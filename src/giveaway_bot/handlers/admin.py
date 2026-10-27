"""Admin console. Every handler here sits behind IsAdmin and works in private chats only;
irreversible actions (close, publish, redraw) take a second confirming press:
"adm:<action>:<id>" asks, "adm:<action>!:<id>" executes."""

import contextlib
import html
import logging
import re
from datetime import UTC, datetime, timedelta, timezone
from functools import cache

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter, Command
from aiogram.types import CallbackQuery, ForceReply, Message, TelegramObject

from giveaway_bot.db.models import Draw, DrawStatus
from giveaway_bot.i18n import LANGS, t
from giveaway_bot.ports.publisher import Button, to_markup
from giveaway_bot.services.claims import Claims
from giveaway_bot.services.draw_service import (
    DrawBlocked,
    DrawService,
    PoolEmpty,
    RedrawOutcome,
    full_name,
)
from giveaway_bot.services.participation import Participation
from giveaway_bot.services.runner import Runner
from giveaway_bot.services.stats import Stats

log = logging.getLogger(__name__)

_CALLBACK = re.compile(r"adm:([a-z]+!?):(\d{1,15})")


class IsAdmin(BaseFilter):
    async def __call__(self, event: TelegramObject, admin_ids: set[int]) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and user.id in admin_ids


router = Router(name="admin")
router.message.filter(IsAdmin(), F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(IsAdmin(), F.message.chat.type == ChatType.PRIVATE)


async def _lang(participation: Participation, tg_id: int) -> str | None:
    user = await participation.get(tg_id)
    return user.lang if user else None


def _kb(lang, rows: list[list[tuple[str, str]]]):
    return to_markup([[Button(t(lang, key), data) for key, data in row] for row in rows])


def _draw_actions(draw: Draw) -> list[list[tuple[str, str]]]:
    action = {
        DrawStatus.COMMITTED: ("btn_retry", f"adm:retry:{draw.id}"),
        DrawStatus.PAUSED: ("btn_retry", f"adm:retry:{draw.id}"),
        DrawStatus.DRAWN: ("btn_approve", f"adm:approve:{draw.id}"),
        DrawStatus.CLAIMED: ("btn_activate", f"adm:activate:{draw.id}"),
        DrawStatus.EXPIRED: ("btn_redraw", f"adm:redraw:{draw.id}"),
    }.get(draw.status)
    return [[action]] if action else []


def _state(lang, status: str) -> str:
    """A draw status in words for admins."""
    return t(lang, f"state_{status}")


TASHKENT = timezone(timedelta(hours=5))


def _when(dt: datetime | None) -> str:
    """A stored (naive UTC) time as Tashkent local time."""
    if dt is None:
        return "—"
    return dt.replace(tzinfo=UTC).astimezone(TASHKENT).strftime("%d.%m.%Y %H:%M")


def _left(lang, delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    return t(lang, "admin_left", h=minutes // 60, m=minutes % 60)


async def _draw_card(lang, draw: Draw, participation: Participation, claims: Claims) -> str:
    """Everything an admin needs about one draw: state, winner, deadline, contact."""
    blocks: list[str] = []
    if draw.parent_id is not None:
        blocks.append(t(lang, "admin_card_redraw", parent_no=draw.parent_id))
    winner = await participation.get(draw.winner_tg_id) if draw.winner_tg_id else None
    if winner is not None:
        name = html.escape(winner.first_name or "—")
        if winner.username:
            name += f" @{html.escape(winner.username)}"
        blocks.append(
            t(lang, "admin_card_winner", name=name, tg_id=winner.tg_id, code=winner.entry_code)
        )
    claim = await claims.claim(draw.id)
    match draw.status:
        case DrawStatus.COMMITTED | DrawStatus.PAUSED:
            blocks.append(t(lang, "admin_card_waiting"))
        case DrawStatus.DRAWN:
            blocks.append(t(lang, "admin_card_approve"))
        case DrawStatus.PUBLISHED:
            deadline = claims.deadline(draw)
            blocks.append(
                t(lang, "admin_card_published", at=_when(draw.published_at))
                + "\n"
                + t(
                    lang,
                    "admin_card_deadline",
                    at=_when(deadline),
                    left=_left(lang, deadline - claims.now()),
                )
            )
        case DrawStatus.EXPIRED:
            blocks.append(t(lang, "admin_card_expired"))
        case DrawStatus.NO_WINNER:
            blocks.append(t(lang, "admin_card_no_winner"))
    if claim is not None:
        contact = t(
            lang,
            "admin_card_contact",
            contact=html.escape(claim.contact),
            at=_when(claim.submitted_at),
        )
        if claim.activated_at is not None:
            contact += "\n" + t(lang, "admin_card_activated", at=_when(claim.activated_at))
        elif draw.status == DrawStatus.CLAIMED:
            contact += "\n" + t(lang, "admin_card_activate_hint")
        blocks.append(contact)
    return t(
        lang,
        "admin_card",
        draw_no=draw.id,
        status=_state(lang, draw.status),
        n=draw.n,
        closed_at=_when(draw.closed_at),
        details="\n\n".join(blocks),
    ).rstrip()


def _confirm(lang, action: str, draw_id: int):
    return _kb(
        lang, [[("btn_confirm", f"adm:{action}!:{draw_id}"), ("btn_cancel", "adm:cancel:0")]]
    )


async def _menu(lang, draws: DrawService):
    current = await draws.current()
    text = t(
        lang,
        "admin_menu",
        pool=await draws.pool_count(),
        current=(
            t(lang, "admin_current", draw_no=current.id, status=_state(lang, current.status))
            if current
            else t(lang, "admin_current_none")
        ),
    )
    rows = [
        [("btn_close", "adm:close:0")],
        [("btn_current", "adm:current:0"), ("btn_stats", "adm:stats:0")],
        [("btn_staff", "adm:staff:0")],
    ]
    awaiting = [
        [Button(t(lang, "btn_awaiting", draw_no=d.id), f"adm:card:{d.id}")]
        for d in await draws.awaiting_activation()
    ]
    return text, to_markup(awaiting + [[Button(t(lang, k), d) for k, d in row] for row in rows])


@router.message(Command("admin"))
async def admin_menu(message: Message, participation: Participation, draws: DrawService) -> None:
    lang = await _lang(participation, message.from_user.id)
    text, kb = await _menu(lang, draws)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("adm:"))
async def admin_action(
    query: CallbackQuery,
    participation: Participation,
    draws: DrawService,
    claims: Claims,
    stats: Stats,
    runner: Runner,
) -> None:
    try:
        match = _CALLBACK.fullmatch(query.data or "")
        if match is None or query.message is None:
            return
        await _dispatch(
            match.group(1), int(match.group(2)), query, participation, draws, claims, stats, runner
        )
    finally:
        await query.answer()


async def _dispatch(
    action: str,
    draw_id: int,
    query: CallbackQuery,
    participation: Participation,
    draws: DrawService,
    claims: Claims,
    stats: Stats,
    runner: Runner,
) -> None:
    admin_id = query.from_user.id
    lang = await _lang(participation, admin_id)
    reply = query.message.answer

    async def already_done() -> None:
        draw = await draws.get(draw_id)
        status = _state(lang, draw.status) if draw else "—"
        await reply(t(lang, "admin_already_done", status=status))

    match action:
        case "close":
            pool = await draws.pool_count()
            if not pool:
                await reply(t(lang, "admin_pool_empty"))
            else:
                await reply(
                    t(lang, "admin_confirm_close", draw_no=await draws.latest_id() + 1, pool=pool),
                    reply_markup=_confirm(lang, "close", 0),
                )
        case "close!":
            try:
                draw = await draws.close_entries(admin_id)
            except DrawBlocked as e:
                await reply(t(lang, "admin_close_blocked", draw_no=e.draw.id, status=e.draw.status))
                return
            except PoolEmpty:
                await reply(t(lang, "admin_pool_empty"))
                return
            except TelegramAPIError as e:
                # The fresh channel-admin list is required for the snapshot; nothing was created.
                log.warning("close: channel admins unavailable: %s", e)
                await reply(t(lang, "check_error"))
                return
            # Always schedule: if the commitment post failed, resolve publishes it first.
            runner.spawn_resolve(draw.id)
            await reply(t(lang, "admin_closed", draw_no=draw.id, n=draw.n, round=draw.beacon_round))
            if draw.commit_msg_id is None:
                await reply(t(lang, "admin_commit_pending", draw_no=draw.id))
        case "current" | "card":
            draw = await (draws.latest() if action == "current" else draws.get(draw_id))
            if draw is None:
                await reply(t(lang, "admin_current_none"))
            else:
                await reply(
                    await _draw_card(lang, draw, participation, claims),
                    reply_markup=_kb(lang, _draw_actions(draw)),
                )
        case "approve":
            await reply(
                f"{t(lang, 'btn_approve')} — №{draw_id}?",
                reply_markup=_confirm(lang, "approve", draw_id),
            )
        case "approve!":
            try:
                result = await draws.approve(draw_id, admin_id)
            except TelegramAPIError as e:
                log.warning("draw %s: results post failed: %s", draw_id, e)
                await reply(
                    t(
                        lang,
                        "admin_publish_failed",
                        draw_no=draw_id,
                        error=html.escape(str(e))[:200],
                    )
                )
                return
            if result is None:
                await already_done()
            # On success every admin gets "admin_published" from the service.
        case "retry":
            draw = await draws.get(draw_id)
            if draw is None or draw.status not in (DrawStatus.COMMITTED, DrawStatus.PAUSED):
                await already_done()
                return
            await draws.audit(admin_id, "retry", draw_id=draw_id)
            runner.spawn_resolve(draw_id)
            await reply(t(lang, "admin_beacon_wait", round=draw.beacon_round))
        case "activate":
            if await claims.activate(draw_id, admin_id):
                await reply(t(lang, "admin_activated", draw_no=draw_id))
            else:
                await already_done()
        case "redraw":
            await reply(
                f"{t(lang, 'btn_redraw')} — №{draw_id}?",
                reply_markup=_confirm(lang, "redraw", draw_id),
            )
        case "redraw!":
            result = await draws.redraw(draw_id, admin_id)
            match result.outcome:
                case RedrawOutcome.CREATED:
                    child = result.draw
                    runner.spawn_resolve(child.id)
                    await reply(
                        t(
                            lang,
                            "admin_closed",
                            draw_no=child.id,
                            n=child.n,
                            round=child.beacon_round,
                        )
                    )
                case RedrawOutcome.NO_CANDIDATES:
                    await reply(t(lang, "admin_redraw_empty", draw_no=draw_id))
                case RedrawOutcome.NOT_ALLOWED:
                    draw = await draws.get(draw_id)
                    if draw is not None and draw.status == DrawStatus.PUBLISHED:
                        await reply(t(lang, "admin_redraw_too_early", hours=claims.window_hours))
                    else:
                        await already_done()
        case "stats":
            await reply(await _stats_text(lang, stats, draws))
        case "staff":
            text, kb = await _staff_screen(lang, participation)
            await reply(text, reply_markup=kb)
        case "staffadd":
            await reply(t(lang, "admin_staff_ask"), reply_markup=_ASK_ID)
        case "staffdel":
            await participation.remove_staff(draw_id, removed_by=admin_id)  # draw_id: the tg_id
            text, kb = await _staff_screen(lang, participation)
            with contextlib.suppress(TelegramAPIError):  # unchanged or too old to edit
                await query.message.edit_text(text, reply_markup=kb)
        case "cancel":
            with contextlib.suppress(TelegramAPIError):  # older than 48h or already gone
                await query.message.delete()


def _pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "—"


async def _stats_text(lang, stats: Stats, draws: DrawService) -> str:
    s = await stats.snapshot()
    retention = (
        f"{_pct(s.retained, s.retention_checked)} ({s.retained}/{s.retention_checked})"
        if s.retention_checked
        else t(lang, "admin_stats_no_data")
    )
    return t(
        lang,
        "admin_stats",
        started=s.started,
        entered=s.entered,
        join_rate=_pct(s.entered, s.started),
        new_to_channel=s.new_to_channel,
        new_share=_pct(s.new_to_channel, s.entered),
        pool=await draws.pool_count(),
        winners=s.winners,
        days=stats.retention.days,
        retention=retention,
        at=_when(stats.now()),
    )


async def _staff_screen(lang, participation: Participation):
    """The staff list with a remove button per person and an add button."""
    lines, rows = [], []
    for tg_id in sorted(await participation.staff_ids()):
        user = await participation.get(tg_id)
        name = full_name(user) if user else ""
        lines.append(
            f"• {html.escape(name)} <code>{tg_id}</code>" if name else f"• <code>{tg_id}</code>"
        )
        rows.append([Button(t(lang, "btn_staff_del", who=name or tg_id), f"adm:staffdel:{tg_id}")])
    rows.append([Button(t(lang, "btn_staff_add"), "adm:staffadd:0")])
    text = t(lang, "admin_staff", list="\n".join(lines) or t(lang, "admin_staff_empty"))
    return text, to_markup(rows)


@cache
def _staff_prompts() -> frozenset[str]:
    """Texts of the bot's "send the staff member's ID" prompts. Answering one with a reply
    (ForceReply) adds staff, so no conversation state has to survive a restart."""
    return frozenset(t(lang, key) for lang in LANGS for key in ("admin_staff_ask", "admin_bad_id"))


def _replies_to_staff_prompt(message: Message) -> bool:
    replied = message.reply_to_message
    return replied is not None and replied.text in _staff_prompts()


_ASK_ID = ForceReply(input_field_placeholder="123456789")


@router.message(_replies_to_staff_prompt)
async def staff_add_reply(message: Message, participation: Participation) -> None:
    """The admin's answer to "send the staff member's Telegram ID"."""
    lang = await _lang(participation, message.from_user.id)
    arg = (message.text or "").strip()
    if not re.fullmatch(r"\d{1,15}", arg):
        await message.answer(t(lang, "admin_bad_id"), reply_markup=_ASK_ID)
        return
    await participation.add_staff(int(arg), added_by=message.from_user.id)
    await message.answer(t(lang, "admin_staff_added", tg_id=arg))
    text, kb = await _staff_screen(lang, participation)
    await message.answer(text, reply_markup=kb)
