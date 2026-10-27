import html
import re
import time

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from giveaway_bot.config import Settings
from giveaway_bot.db.models import UserStatus
from giveaway_bot.handlers.menu import menu_action, menu_keyboard, set_chat_commands
from giveaway_bot.i18n import LANGS, t
from giveaway_bot.ports.publisher import Button, to_markup
from giveaway_bot.services.claims import ClaimOutcome, Claims
from giveaway_bot.services.draw_service import PUBLIC_RESULT, DrawService
from giveaway_bot.services.notices import notify_toggle
from giveaway_bot.services.participation import EnterOutcome, Participation

router = Router(name="user")
# Claims echo contacts and /status shows entry codes: never in a group.
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

_LANG_KEYBOARD = [[Button(t(lang, "lang_name"), f"lang:{lang}")] for lang in LANGS]


def _rules_markup(lang: str | None, settings: Settings):
    return to_markup(
        [
            [Button(t(lang, "btn_join"), "join")],
            [Button(t(lang, "btn_channel"), url=settings.channel_url)],
        ]
    )


async def _send_menu(message: Message, lang: str | None) -> None:
    """The persistent bottom menu travels on its own short message: a message carries either
    a reply keyboard or inline buttons, never both."""
    await message.answer(t(lang, "welcome"), reply_markup=menu_keyboard(lang))


class VerifyLimiter:
    """/verify sends the whole entry-code list. Upload it once per draw and reuse
    Telegram's file_id, and let each user ask at most once per cooldown."""

    def __init__(self, cooldown: float = 30, clock=time.monotonic) -> None:
        self.cooldown = cooldown
        self.clock = clock
        self.file_ids: dict[int, str] = {}
        self._last: dict[int, float] = {}

    def wait_left(self, tg_id: int) -> int:
        """Seconds the user still has to wait (0 = allowed, and the call is recorded)."""
        now = self.clock()
        last = self._last.get(tg_id)
        if last is not None and now - last < self.cooldown:
            return int(self.cooldown - (now - last)) + 1
        self._last[tg_id] = now
        return 0


async def _remind_claim(message: Message, tg_id: int, lang: str | None, claims: Claims) -> bool:
    """A winner who missed the DM gets the claim instructions whenever they open the bot."""
    if await claims.pending_claim(tg_id) is None:
        return False
    await message.answer(
        t(lang, "win_dm", hours=claims.window_hours), disable_web_page_preview=True
    )
    return True


async def _show_rules(message: Message, lang: str | None, settings: Settings) -> None:
    await message.answer(
        t(lang, "rules", channel_url=settings.channel_url),
        reply_markup=_rules_markup(lang, settings),
        disable_web_page_preview=True,
    )


@router.message(CommandStart())
async def start(
    message: Message,
    command: CommandObject,
    participation: Participation,
    settings: Settings,
    claims: Claims,
) -> None:
    source = (command.args or "")[:64] or None
    user = await participation.register_start(
        message.from_user.id,
        message.from_user.first_name or "",
        message.from_user.username,
        source=source,
    )
    if user.lang is None:
        await message.answer(t(None, "lang_prompt"), reply_markup=to_markup(_LANG_KEYBOARD))
        return
    await _send_menu(message, user.lang)
    if await _remind_claim(message, user.tg_id, user.lang, claims):
        return
    await _show_rules(message, user.lang, settings)


@router.message(Command("language"))
async def language(message: Message) -> None:
    await message.answer(t(None, "lang_prompt"), reply_markup=to_markup(_LANG_KEYBOARD))


@router.callback_query(F.data.startswith("lang:"))
async def set_language(
    query: CallbackQuery,
    bot: Bot,
    participation: Participation,
    settings: Settings,
    admin_ids: set[int],
) -> None:
    lang = query.data.split(":", 1)[1]
    if lang not in LANGS:
        await query.answer()
        return
    await participation.set_lang(query.from_user.id, lang)
    await query.message.edit_text(t(lang, "lang_set"))
    await _send_menu(query.message, lang)
    await set_chat_commands(bot, query.from_user.id, lang, admin=query.from_user.id in admin_ids)
    await _show_rules(query.message, lang, settings)
    await query.answer()


@router.callback_query(F.data == "join")
async def join(query: CallbackQuery, participation: Participation, settings: Settings) -> None:
    user = await participation.get(query.from_user.id)
    if user is None:  # pressed a button from an old message before /start was recorded
        user = await participation.register_start(
            query.from_user.id, query.from_user.first_name or "", query.from_user.username
        )
    lang = user.lang
    result = await participation.enter(query.from_user.id)
    match result.outcome:
        case EnterOutcome.ENTERED:
            text, keyboard = t(lang, "joined", code=result.code), None
        case EnterOutcome.ALREADY_ENTERED:
            text, keyboard = t(lang, "already_joined", code=result.code), None
        case EnterOutcome.NOT_SUBSCRIBED:
            text = t(lang, "not_subscribed")
            keyboard = [
                [Button(t(lang, "btn_subscribe"), url=settings.channel_url)],
                [Button(t(lang, "btn_check_again"), "join")],
            ]
        case EnterOutcome.NOT_ELIGIBLE:
            text, keyboard = t(lang, "not_eligible"), None
        case EnterOutcome.ALREADY_WON:
            text, keyboard = t(lang, "already_won"), None
        case EnterOutcome.CHECK_FAILED:
            text, keyboard = t(lang, "check_error"), [[Button(t(lang, "btn_check_again"), "join")]]
    await query.message.answer(text, reply_markup=to_markup(keyboard))
    await query.answer()


def _status_card(user) -> tuple[str, object]:
    state = t(user.lang, "notify_state_on" if user.notify else "notify_state_off")
    text = t(
        user.lang,
        "status_active",
        since=user.entered_at.strftime("%Y-%m-%d"),
        code=user.entry_code,
        notify_state=state,
    )
    toggle = notify_toggle(user.lang, user.notify)
    return text, to_markup([[Button(toggle.text, f"{toggle.callback}:status")]])


@router.callback_query(F.data.regexp(r"notify:(on|off)(:status)?"))
async def toggle_notify(query: CallbackQuery, participation: Participation) -> None:
    """Mute/unmute weekly notices from the button under a notice or /status."""
    _, value, *where = query.data.split(":")
    enabled = value == "on"
    await participation.set_notify(query.from_user.id, enabled)
    user = await participation.get(query.from_user.id)
    lang = user.lang if user else None
    if where and user is not None and user.entry_code:
        text, markup = _status_card(user)
        await query.message.edit_text(text, reply_markup=markup)
    elif query.message is not None and query.message.reply_markup is not None:
        toggle = notify_toggle(lang, enabled)
        rows = [
            [
                Button(toggle.text, toggle.callback)
                if (b.callback_data or "").startswith("notify:")
                else Button(b.text, b.callback_data, b.url)
                for b in row
            ]
            for row in query.message.reply_markup.inline_keyboard
        ]
        await query.message.edit_reply_markup(reply_markup=to_markup(rows))
    await query.answer(t(lang, "notify_unmuted" if enabled else "notify_muted"))


@router.message(Command("status"))
async def status(message: Message, participation: Participation, claims: Claims) -> None:
    user = await participation.get(message.from_user.id)
    lang = user.lang if user else None
    if user is not None and await _remind_claim(message, user.tg_id, lang, claims):
        return
    excluded = (
        user is not None
        and user.status != UserStatus.WON
        and await participation.is_excluded(user.tg_id)
    )
    if excluded:
        await message.answer(t(lang, "not_eligible"))
        return
    if user is not None and user.status == UserStatus.WON:
        await message.answer(t(lang, "status_won"))
    elif user is not None and user.entry_code:
        text, markup = _status_card(user)
        await message.answer(text, reply_markup=markup)
    else:
        await message.answer(t(lang, "status_not_entered"))


@router.message(Command("help"))
async def help_(message: Message, participation: Participation) -> None:
    user = await participation.get(message.from_user.id)
    await message.answer(t(user.lang if user else None, "help"))


@router.message(Command("verify"))
async def verify(
    message: Message,
    command: CommandObject,
    participation: Participation,
    draws: DrawService,
    verify_limiter: VerifyLimiter,
) -> None:
    await _verify(message, command.args, participation, draws, verify_limiter)


async def _verify(
    message: Message,
    args: str | None,
    participation: Participation,
    draws: DrawService,
    verify_limiter: VerifyLimiter,
) -> None:
    user = await participation.get(message.from_user.id)
    lang = user.lang if user else None
    if wait := verify_limiter.wait_left(message.from_user.id):
        await message.answer(t(lang, "verify_cooldown", seconds=wait))
        return
    arg = (args or "").strip().lstrip("№#")
    if arg and not re.fullmatch(r"[0-9]{1,9}", arg):
        await message.answer(t(lang, "verify_unknown"))
        return
    draw = await draws.get(int(arg)) if arg else await draws.latest_public()
    if draw is None:
        await message.answer(t(lang, "verify_unknown" if arg else "verify_none"))
        return
    # Nothing beyond the commitment is shown until the result is announced in the channel.
    if (
        draw.status not in PUBLIC_RESULT
        or draw.randomness is None
        or draw.result_msg_id is None  # announced in the channel, not just decided
    ):
        await message.answer(t(lang, "verify_pending"))
        return
    if draw.winner_position is not None:
        skipped = ", ".join(str(x["index"]) for x in draw.skipped) or "—"
        result_line = t(lang, "verify_result_line", position=draw.winner_position, skipped=skipped)
    else:
        result_line = ""
    await message.answer(
        t(
            lang,
            "verify_text",
            draw_no=draw.id,
            snapshot_hash=draw.snapshot_hash,
            round=draw.beacon_round,
            chain=draws.beacon.chain_hash,
            n=draw.n,
            index=draw.winner_index,
            result_line=result_line,
        ),
        disable_web_page_preview=True,
    )
    cached = verify_limiter.file_ids.get(draw.id)
    if cached is not None:
        await message.answer_document(cached)
        return
    codes = await draws.entry_codes(draw.id)
    sent = await message.answer_document(
        BufferedInputFile("\n".join(codes).encode("utf-8"), filename=f"draw-{draw.id}.txt")
    )
    if sent.document is not None:
        verify_limiter.file_ids[draw.id] = sent.document.file_id


@router.message(F.text.func(menu_action).as_("menu_key"))
async def menu_button(
    message: Message,
    menu_key: str,
    participation: Participation,
    settings: Settings,
    claims: Claims,
    draws: DrawService,
    verify_limiter: VerifyLimiter,
) -> None:
    """Bottom-menu presses, in any language. Registered before free_text so a winner
    pressing a menu button is never mistaken for a claim."""
    match menu_key:
        case "menu_contest":
            user = await participation.get(message.from_user.id)
            await _show_rules(message, user.lang if user else None, settings)
        case "menu_status":
            await status(message, participation, claims)
        case "menu_verify":
            await _verify(message, None, participation, draws, verify_limiter)
        case "menu_language":
            await language(message)


@router.message(F.text & ~F.text.startswith("/"))
async def free_text(message: Message, participation: Participation, claims: Claims) -> None:
    """Free text is only meaningful from a winner sending their review.uz login."""
    user = await participation.get(message.from_user.id)
    lang = user.lang if user else None
    result = await claims.submit(message.from_user.id, message.text)
    match result.outcome:
        case ClaimOutcome.ACCEPTED:
            await message.answer(t(lang, "claim_accepted", contact=html.escape(result.contact)))
        case ClaimOutcome.INVALID:
            await message.answer(t(lang, "claim_invalid"))
        case ClaimOutcome.TOO_LATE:
            await message.answer(t(lang, "claim_too_late", hours=claims.window_hours))
        case ClaimOutcome.ALREADY_RECEIVED:
            await message.answer(t(lang, "claim_already"))
        case ClaimOutcome.NOT_A_WINNER:
            await message.answer(t(lang, "help"))
