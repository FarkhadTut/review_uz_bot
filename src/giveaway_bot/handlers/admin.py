"""Admin console. Every handler here sits behind IsAdmin; irreversible actions (close,
publish, redraw) take a second confirming press: "adm:<action>:<id>" asks,
"adm:<action>!:<id>" executes."""

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import CallbackQuery, Message, TelegramObject

from giveaway_bot.db.models import Draw, DrawStatus
from giveaway_bot.i18n import t
from giveaway_bot.ports.publisher import Button, to_markup
from giveaway_bot.services.claims import Claims
from giveaway_bot.services.draw_service import DrawBlocked, DrawService, PoolEmpty
from giveaway_bot.services.participation import Participation
from giveaway_bot.services.runner import Runner
from giveaway_bot.services.stats import Stats


class IsAdmin(BaseFilter):
    async def __call__(self, event: TelegramObject, admin_ids: set[int]) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and user.id in admin_ids


router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


async def _lang(participation: Participation, tg_id: int) -> str | None:
    user = await participation.get(tg_id)
    return user.lang if user else None


def _kb(lang, rows: list[list[tuple[str, str]]]):
    return to_markup([[Button(t(lang, key), data) for key, data in row] for row in rows])


def _draw_actions(draw: Draw) -> list[list[tuple[str, str]]]:
    action = {
        DrawStatus.DRAWN: ("btn_approve", f"adm:approve:{draw.id}"),
        DrawStatus.PAUSED: ("btn_retry", f"adm:retry:{draw.id}"),
        DrawStatus.CLAIMED: ("btn_activate", f"adm:activate:{draw.id}"),
        DrawStatus.EXPIRED: ("btn_redraw", f"adm:redraw:{draw.id}"),
    }.get(draw.status)
    return [[action]] if action else []


async def _menu(lang, draws: DrawService):
    current = await draws.current()
    text = t(
        lang,
        "admin_menu",
        pool=len(await draws.pool()),
        current=(
            t(lang, "admin_current", draw_no=current.id, status=current.status)
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
    _, action, arg = query.data.split(":", 2)
    draw_id = int(arg)
    admin_id = query.from_user.id
    lang = await _lang(participation, admin_id)
    reply = query.message.answer

    match action:
        case "close":
            pool = len(await draws.pool())
            if not pool:
                await reply(t(lang, "admin_pool_empty"))
            else:
                last = await draws.latest_id()
                await reply(
                    t(lang, "admin_confirm_close", draw_no=last + 1, pool=pool),
                    reply_markup=_kb(
                        lang, [[("btn_confirm", "adm:close!:0"), ("btn_cancel", "adm:cancel:0")]]
                    ),
                )
        case "close!":
            try:
                draw = await draws.close_entries(admin_id)
            except DrawBlocked as e:
                await reply(t(lang, "admin_close_blocked", draw_no=e.draw.id, status=e.draw.status))
            except PoolEmpty:
                await reply(t(lang, "admin_pool_empty"))
            else:
                await reply(
                    t(lang, "admin_closed", draw_no=draw.id, n=draw.n, round=draw.beacon_round)
                )
                runner.spawn_resolve(draw.id)
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
                t(lang, "btn_approve") + f" — №{draw_id}?",
                reply_markup=_kb(
                    lang,
                    [[("btn_confirm", f"adm:approve!:{draw_id}"), ("btn_cancel", "adm:cancel:0")]],
                ),
            )
        case "approve!":
            await draws.approve(draw_id, admin_id)  # admins are told the result by the service
        case "retry":
            runner.spawn_resolve(draw_id)
            draw = await draws.get(draw_id)
            await reply(t(lang, "admin_beacon_wait", round=draw.beacon_round))
        case "activate":
            if await claims.activate(draw_id, admin_id):
                await reply(t(lang, "admin_activated", draw_no=draw_id))
        case "redraw":
            await reply(
                t(lang, "btn_redraw") + f" — №{draw_id}?",
                reply_markup=_kb(
                    lang,
                    [[("btn_confirm", f"adm:redraw!:{draw_id}"), ("btn_cancel", "adm:cancel:0")]],
                ),
            )
        case "redraw!":
            child = await draws.redraw(draw_id, admin_id)
            if child is None:
                await reply(t(lang, "admin_redraw_too_early", hours=claims.window_hours))
            else:
                await reply(
                    t(lang, "admin_closed", draw_no=child.id, n=child.n, round=child.beacon_round)
                )
                runner.spawn_resolve(child.id)
        case "stats":
            await reply(await _stats_text(lang, stats, draws))
        case "staff":
            await reply(await _staff_text(lang, participation))
        case "cancel":
            await query.message.delete()
    await query.answer()


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
        pool=len(await draws.pool()),
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
    if not arg.lstrip("-").isdigit():
        await message.answer(t(lang, "admin_bad_id"))
        return
    tg_id = int(arg)
    if command.command == "staff_add":
        await participation.add_staff(tg_id, added_by=message.from_user.id)
        await message.answer(t(lang, "admin_staff_added", tg_id=tg_id))
    else:
        await participation.remove_staff(tg_id, removed_by=message.from_user.id)
        await message.answer(t(lang, "admin_staff_removed", tg_id=tg_id))
