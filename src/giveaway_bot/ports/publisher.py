"""Outgoing Telegram messages. Services hand over rendered HTML and neutral button specs
so business logic stays testable without aiogram objects."""

import logging
from dataclasses import dataclass, field
from typing import Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger(__name__)

# Rows of buttons; each button carries either callback data or a URL.
Keyboard = list[list["Button"]]


@dataclass(frozen=True)
class Button:
    text: str
    callback: str | None = None
    url: str | None = None


class Publisher(Protocol):
    async def post_channel(self, html: str) -> int: ...

    async def send(self, tg_id: int, html: str, keyboard: Keyboard | None = None) -> bool: ...


def to_markup(keyboard: Keyboard | None) -> InlineKeyboardMarkup | None:
    if not keyboard:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=b.text, callback_data=b.callback, url=b.url) for b in row]
            for row in keyboard
        ]
    )


class TelegramPublisher:
    def __init__(self, bot: Bot, channel_id: str | int) -> None:
        self.bot = bot
        self.channel_id = channel_id

    async def post_channel(self, html: str) -> int:
        msg = await self.bot.send_message(self.channel_id, html, disable_web_page_preview=True)
        return msg.message_id

    async def send(self, tg_id: int, html: str, keyboard: Keyboard | None = None) -> bool:
        """False when the user can't be reached (never opened the bot, blocked it, ...)."""
        try:
            await self.bot.send_message(
                tg_id, html, reply_markup=to_markup(keyboard), disable_web_page_preview=True
            )
        except TelegramAPIError as e:
            log.warning("send to %s failed: %s", tg_id, e)
            return False
        return True


@dataclass
class FakePublisher:
    channel: list[str] = field(default_factory=list)
    sent: list[tuple[int, str, Keyboard | None]] = field(default_factory=list)
    unreachable: set[int] = field(default_factory=set)

    async def post_channel(self, html: str) -> int:
        self.channel.append(html)
        return len(self.channel)

    async def send(self, tg_id: int, html: str, keyboard: Keyboard | None = None) -> bool:
        if tg_id in self.unreachable:
            return False
        self.sent.append((tg_id, html, keyboard))
        return True

    def to(self, tg_id: int) -> list[str]:
        return [html for to, html, _ in self.sent if to == tg_id]
