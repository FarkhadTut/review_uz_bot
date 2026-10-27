"""Bot profile and menus: the persistent reply keyboard, per-language command menus and the
bot description. Telegram failures here are cosmetic, so they are logged and never fatal."""

import logging
from collections.abc import Awaitable
from functools import cache

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from giveaway_bot.i18n import CHANNEL_LANG, LANGS, t

log = logging.getLogger(__name__)

MENU_KEYS = ("menu_contest", "menu_status", "menu_verify", "menu_language")
USER_COMMANDS = ("start", "status", "verify", "language", "help")
ADMIN_COMMANDS = ("admin", "stats", "staff_add", "staff_del")
# Telegram client language -> catalog. The "uz" code can't tell Cyrillic from Latin, so
# Uzbek clients get the default (Cyrillic) until they pick a language in the bot.
CLIENT_LANGS = {None: CHANNEL_LANG, "ru": "ru"}


def menu_keyboard(lang: str | None) -> ReplyKeyboardMarkup:
    contest, status, verify, language = (KeyboardButton(text=t(lang, k)) for k in MENU_KEYS)
    return ReplyKeyboardMarkup(
        keyboard=[[contest, status], [verify, language]],
        resize_keyboard=True,
        is_persistent=True,
    )


@cache
def _labels() -> dict[str, str]:
    return {t(lang, key): key for lang in LANGS for key in MENU_KEYS}


def menu_action(text: str | None) -> str | None:
    """The menu key a reply-keyboard label stands for, in any language."""
    return _labels().get((text or "").strip())


def commands(lang: str | None, *, admin: bool = False) -> list[BotCommand]:
    names = USER_COMMANDS + (ADMIN_COMMANDS if admin else ())
    return [BotCommand(command=name, description=t(lang, f"cmd_{name}")) for name in names]


async def _safe(call: Awaitable, what: str) -> None:
    try:
        await call
    except TelegramAPIError as e:
        log.warning("%s failed: %s", what, e)


async def set_chat_commands(bot: Bot, chat_id: int, lang: str | None, *, admin: bool) -> None:
    """The ☰ menu for one chat: its chosen language, plus admin commands for admins."""
    await _safe(
        bot.set_my_commands(
            commands(lang, admin=admin), scope=BotCommandScopeChat(chat_id=chat_id)
        ),
        f"commands for {chat_id}",
    )


async def setup_profile(bot: Bot, admin_langs: dict[int, str | None]) -> None:
    """Default menus and descriptions per client language, and admin menus."""
    for code, lang in CLIENT_LANGS.items():
        await _safe(
            bot.set_my_commands(commands(lang), scope=BotCommandScopeDefault(), language_code=code),
            f"default commands ({code})",
        )
        await _safe(
            bot.set_my_description(t(lang, "bot_description"), language_code=code),
            f"description ({code})",
        )
        await _safe(
            bot.set_my_short_description(t(lang, "bot_short_description"), language_code=code),
            f"short description ({code})",
        )
    for admin_id, lang in admin_langs.items():
        await set_chat_commands(bot, admin_id, lang, admin=True)
