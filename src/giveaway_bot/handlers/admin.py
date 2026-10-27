"""Admin console. Every handler here sits behind IsAdmin and works in private chats only;
irreversible actions (close, publish, redraw) take a second confirming press:
"adm:<action>:<id>" asks, "adm:<action>!:<id>" executes."""

import contextlib
import html
import logging
import re

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import CallbackQuery, Message, TelegramObject

from giveaway_bot.db.models import Draw, DrawStatus
from giveaway_bot.i18n import t
from giveaway_bot.ports.publisher import Button, to_markup
from giveaway_bot.services.claims import Claims
from giveaway_bot.services.draw_service import (
    DrawBlocked,
    DrawService,
    PoolEmpty,
    RedrawOutcome,
)
from giveaway_bot.services.participation import Participation
from giveaway_bot.services.runner import Runner
from giveaway_bot.services.stats import Stats

log = logging.getLogger(__name__)

_CALLBACK = re.compile(r"adm:([a-z]+!?):(\d{1,9})")


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
    return text, _kb(lang, rows)


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
        case "current":
            draw = await draws.current()
            if draw is None:
                await reply(t(lang, "admin_current_none"))
            else:
                await reply(
                    t(lang, "admin_current", draw_no=draw.id, status=draw.status),
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
            await reply(await _staff_text(lang, participation))
        case "cancel":
            with contextlib.suppress(TelegramAPIError):  # older than 48h or already gone
                await query.message.delete()


async def _stats_text(lang, stats: Stats, draws: DrawService) -> str:
    s = await stats.snapshot()
    retention = (
        f"{100 * s.retained / s.retention_checked:.0f}% ({s.retained}/{s.retention_checked})"
        if s.retention_checked
        else "—"
    )
    return t(
        lang,
        "admin_stats",
        started=s.started,
        entered=s.entered,
        new_to_channel=s.new_to_channel,
        pool=await draws.pool_count(),
        winners=s.winners,
        retention=retention,
    )


async def _staff_text(lang, participation: Participation) -> str:
    ids = sorted(await participation.staff_ids())
    listing = "\n".join(str(i) for i in ids) or t(lang, "admin_staff_empty")
    return t(lang, "admin_staff", list=listing)


@router.message(Command("stats"))
async def stats_cmd(
    message: Message, participation: Participation, stats: Stats, draws: DrawService
) -> None:
    lang = await _lang(participation, message.from_user.id)
    await message.answer(await _stats_text(lang, stats, draws))


@router.message(Command("staff_add", "staff_del"))
async def staff_edit(
    message: Message, command: CommandObject, participation: Participation
) -> None:
    lang = await _lang(participation, message.from_user.id)
    arg = (command.args or "").strip()
    if not re.fullmatch(r"-?\d{1,15}", arg):
        await message.answer(t(lang, "admin_bad_id"))
        return
    tg_id = int(arg)
    if command.command == "staff_add":
        await participation.add_staff(tg_id, added_by=message.from_user.id)
        await message.answer(t(lang, "admin_staff_added", tg_id=tg_id))
    else:
        await participation.remove_staff(tg_id, removed_by=message.from_user.id)
        await message.answer(t(lang, "admin_staff_removed", tg_id=tg_id))
