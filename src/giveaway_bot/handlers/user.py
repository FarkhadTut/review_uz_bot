import contextlib
import re
import time

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from giveaway_bot.config import Settings
from giveaway_bot.db.models import UserStatus
from giveaway_bot.handlers.menu import lang_for, menu_action, menu_keyboard, set_chat_commands
from giveaway_bot.i18n import LANGS, t
from giveaway_bot.ports.membership import NOT_SUBSCRIBED
from giveaway_bot.ports.publisher import Button, ContactRequest, to_markup
from giveaway_bot.services.claims import ClaimOutcome, Claims
from giveaway_bot.services.draw_service import (
    INLINE_LIST_LIMIT,
    PUBLIC_RESULT,
    DrawService,
    short_list,
)
from giveaway_bot.services.notices import notify_toggle
from giveaway_bot.services.participation import EnterOutcome, Participation

router = Router(name="user")
# Claims echo contacts and /status shows entry codes: never in a group.
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

_LANG_KEYBOARD = [[Button(t(lang, "lang_name"), f"lang:{lang}")] for lang in LANGS]


# What the rules card says about the viewer, keyed by entry state.
_ENTRY_HINTS = {
    "in": "rules_in",
    "unsubscribed": "rules_subscribe_first",
    "left": "rules_resubscribe",
    "won": "already_won",
    "excluded": "not_eligible",
    "unknown": "check_error",
}


async def _entry_state(participation: Participation, tg_id: int) -> tuple[str, str | None]:
    """Enter the viewer if they're subscribed (there is no join button) and say where they
    stand: in / unsubscribed / left (entered, then left the channel) / won / excluded /
    unknown (Telegram couldn't answer)."""
    result = await participation.enter(tg_id)
    match result.outcome:
        case EnterOutcome.ENTERED:
            return "in", result.code
        case EnterOutcome.ALREADY_ENTERED:
            left = await participation.membership.check(tg_id) in NOT_SUBSCRIBED
            return ("left" if left else "in"), result.code
        case EnterOutcome.NOT_SUBSCRIBED:
            return "unsubscribed", None
        case EnterOutcome.ALREADY_WON:
            return "won", None
        case EnterOutcome.NOT_ELIGIBLE:
            return "excluded", None
    return "unknown", None


def _rules_card(lang: str | None, settings: Settings, state: str, code: str | None):
    text = t(lang, "rules", channel_url=settings.channel_url).rstrip()
    text = f"{text}\n\n{t(lang, _ENTRY_HINTS[state], code=code)}"
    channel = [Button(t(lang, "btn_channel"), url=settings.channel_url)]
    if state in ("unsubscribed", "left"):  # subscribing comes first, so it goes on top
        rows = [
            [Button(t(lang, "btn_subscribe"), url=settings.channel_url)],
            [Button(t(lang, "btn_subscribed"), "join")],
        ]
    elif state == "unknown":
        rows = [[Button(t(lang, "btn_check_again"), "join")], channel]
    else:
        rows = [channel]
    return text, to_markup(rows)


def _replace_buttons(markup, matches, button: Button) -> list[list[Button]]:
    """A copy of an inline keyboard with every button whose callback matches swapped out."""
    return [
        [
            button if matches(b.callback_data or "") else Button(b.text, b.callback_data, b.url)
            for b in row
        ]
        for row in markup.inline_keyboard
    ]


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
        t(lang, "win_dm", hours=claims.window_hours),
        reply_markup=_share_contact(lang),
        disable_web_page_preview=True,
    )
    return True


async def _show_rules(
    message: Message, user, lang: str | None, settings: Settings, participation: Participation
) -> None:
    """The rules card. Showing it enters a subscribed viewer; others are asked to subscribe."""
    if user is None:  # a menu label typed before /start was recorded
        await participation.register_start(
            message.from_user.id, message.from_user.first_name or "", message.from_user.username
        )
    state, code = await _entry_state(participation, message.chat.id)
    text, markup = _rules_card(lang, settings, state, code)
    await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


@router.message(CommandStart())
async def start(
    message: Message,
    command: CommandObject,
    bot: Bot,
    participation: Participation,
    settings: Settings,
    claims: Claims,
    admin_ids: set[int],
) -> None:
    source = (command.args or "")[:64] or None
    user = await participation.register_start(
        message.from_user.id,
        message.from_user.first_name or "",
        message.from_user.username,
        source=source,
    )
    if user.lang is None:  # no language question: one Start tap must be enough to enter
        user.lang = lang_for(message.from_user.language_code)
        await participation.set_lang(user.tg_id, user.lang)
        await set_chat_commands(bot, user.tg_id, user.lang, admin=user.tg_id in admin_ids)
    await _send_menu(message, user.lang)
    if await _remind_claim(message, user.tg_id, user.lang, claims):
        return
    await _show_rules(message, user, user.lang, settings, participation)


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
    user = await participation.get(query.from_user.id)
    await _show_rules(query.message, user, lang, settings, participation)
    await query.answer()


@router.callback_query(F.data == "join")
async def join(query: CallbackQuery, participation: Participation, settings: Settings) -> None:
    """«I subscribed» / «Check again»: enter if subscribed now and update the card."""
    user = await participation.get(query.from_user.id)
    if user is None:  # pressed a button from an old message before /start was recorded
        user = await participation.register_start(
            query.from_user.id, query.from_user.first_name or "", query.from_user.username
        )
    lang = user.lang
    state, code = await _entry_state(participation, query.from_user.id)
    if state in ("unsubscribed", "left", "unknown"):
        key = "check_error" if state == "unknown" else "not_subscribed"
        await query.answer(t(lang, key), show_alert=True)
        return
    text, markup = _rules_card(lang, settings, state, code)
    with contextlib.suppress(TelegramBadRequest):  # unchanged, or too old to edit
        await query.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    await query.answer(t(lang, "joined_toast") if state == "in" else None)


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
        rows = _replace_buttons(
            query.message.reply_markup, lambda data: data.startswith("notify:"), toggle
        )
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
    # Skips and void rounds live here, not in the channel post.
    shown = draw.skipped[:INLINE_LIST_LIMIT]
    skipped = ", ".join(f"{x['index']} (<code>{x.get('code', '?')}</code>)" for x in shown) or "—"
    if len(draw.skipped) > len(shown):
        skipped += f" … +{len(draw.skipped) - len(shown)}"
    lines = []
    if draw.winner_position is not None:
        lines.append(t(lang, "verify_result_line", position=draw.winner_position, skipped=skipped))
    elif draw.skipped:
        lines.append(t(lang, "verify_skipped_line", skipped=skipped))
    if draw.void_rounds:
        lines.append(t(lang, "verify_void_line", rounds=short_list(draw.void_rounds)))
    result_line = "\n".join(lines)
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
            await _show_rules(message, user, user.lang if user else None, settings, participation)
        case "menu_status":
            await status(message, participation, claims)
        case "menu_language":
            await language(message)


def _share_contact(lang: str | None):
    return to_markup(ContactRequest(t(lang, "btn_share_contact")))


@router.message(F.contact)
async def shared_contact(message: Message, participation: Participation, claims: Claims) -> None:
    """A winner claims the prize by sharing their own Telegram contact."""
    user = await participation.get(message.from_user.id)
    lang = user.lang if user else None
    if message.contact.user_id != message.from_user.id:
        await message.answer(t(lang, "claim_not_own"), reply_markup=_share_contact(lang))
        return
    result = await claims.submit(message.from_user.id, message.contact.phone_number)
    key = {
        ClaimOutcome.ACCEPTED: "claim_accepted",
        ClaimOutcome.ALREADY_RECEIVED: "claim_already",
        ClaimOutcome.TOO_LATE: "claim_too_late",
        ClaimOutcome.INVALID: "claim_not_own",
        ClaimOutcome.NOT_A_WINNER: "help",
    }[result.outcome]
    await message.answer(t(lang, key, hours=claims.window_hours), reply_markup=menu_keyboard(lang))


@router.message(F.text & ~F.text.startswith("/"))
async def free_text(message: Message, participation: Participation, claims: Claims) -> None:
    """Typed text is never a claim: a winner is pointed to the share-contact button."""
    user = await participation.get(message.from_user.id)
    lang = user.lang if user else None
    tg_id = message.from_user.id
    if await claims.pending_claim(tg_id) is not None:
        await message.answer(t(lang, "claim_use_button"), reply_markup=_share_contact(lang))
    elif await claims.open_claim_draw(tg_id) is not None:
        await message.answer(t(lang, "claim_too_late", hours=claims.window_hours))
    elif await claims.already_claimed(tg_id):
        await message.answer(t(lang, "claim_already"))
    else:
        await message.answer(t(lang, "help"))
