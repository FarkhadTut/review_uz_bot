"""Outgoing Telegram messages. Services hand over rendered HTML and neutral button specs
so business logic stays testable without aiogram objects."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
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

    def post_link(self, msg_id: int) -> str | None: ...


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
        """False when the user can't be reached (never opened the bot, blocked it, ...).
        A flood-control answer is waited out once before giving up."""
        for attempt in range(2):
            try:
                await self.bot.send_message(
                    tg_id, html, reply_markup=to_markup(keyboard), disable_web_page_preview=True
                )
            except TelegramRetryAfter as e:
                if attempt:
                    log.warning("send to %s failed: %s", tg_id, e)
                    return False
                await asyncio.sleep(e.retry_after)
            except TelegramAPIError as e:
                log.warning("send to %s failed: %s", tg_id, e)
                return False
            else:
                return True
        return False

    def post_link(self, msg_id: int) -> str | None:
        """Public URL of a channel post (t.me/c/... works for members of a private channel)."""
        channel = str(self.channel_id)
        if channel.startswith("@"):
            return f"https://t.me/{channel[1:]}/{msg_id}"
        if channel.startswith("-100"):
            return f"https://t.me/c/{channel[4:]}/{msg_id}"
        return None


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

    def post_link(self, msg_id: int) -> str | None:
        return f"https://t.me/c/1/{msg_id}"

    def to(self, tg_id: int) -> list[str]:
        return [html for to, html, _ in self.sent if to == tg_id]
