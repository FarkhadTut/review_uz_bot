"""Channel membership via the Bot API. The bot must be an administrator of the channel:
getChatMember is only guaranteed to work for other users in that case."""

import asyncio
import logging
import time
from enum import StrEnum
from typing import Protocol

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter

log = logging.getLogger(__name__)


class Membership(StrEnum):
    YES = "yes"
    NO = "no"
    # The API failed. Callers must never treat this as NO: an outage must not cost
    # someone their entry or their prize.
    UNKNOWN = "unknown"


class MembershipChecker(Protocol):
    async def check(self, tg_id: int) -> Membership: ...

    async def channel_admin_ids(self) -> set[int]: ...


_SUBSCRIBED = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}


class TelegramMembership:
    def __init__(
        self, bot: Bot, channel_id: str | int, *, attempts: int = 3, admins_ttl: float = 300
    ) -> None:
        self.bot = bot
        self.channel_id = channel_id
        self.attempts = attempts
        self.admins_ttl = admins_ttl
        self._admins: tuple[float, set[int]] | None = None

    async def check(self, tg_id: int) -> Membership:
        for attempt in range(1, self.attempts + 1):
            try:
                member = await self.bot.get_chat_member(self.channel_id, tg_id)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                continue
            except TelegramBadRequest as e:
                # "user not found" / "PARTICIPANT_ID_INVALID": never joined, so not a member.
                if (
                    "not found" in e.message.lower()
                    or "participant_id_invalid" in e.message.lower()
                ):
                    return Membership.NO
                log.warning("getChatMember(%s) bad request: %s", tg_id, e.message)
            except TelegramNetworkError as e:
                log.warning("getChatMember(%s) network error (try %s): %s", tg_id, attempt, e)
            else:
                if member.status in _SUBSCRIBED:
                    return Membership.YES
                if member.status == ChatMemberStatus.RESTRICTED:
                    return Membership.YES if getattr(member, "is_member", False) else Membership.NO
                return Membership.NO
            await asyncio.sleep(attempt)
        return Membership.UNKNOWN

    async def channel_admin_ids(self) -> set[int]:
        # Called on every entry; the admin list changes rarely, so cache it briefly.
        if self._admins is None or time.monotonic() - self._admins[0] > self.admins_ttl:
            admins = await self.bot.get_chat_administrators(self.channel_id)
            self._admins = (time.monotonic(), {a.user.id for a in admins})
        return set(self._admins[1])

    async def bot_is_admin(self) -> bool:
        me = await self.bot.get_me()
        member = await self.bot.get_chat_member(self.channel_id, me.id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)


class FakeMembership:
    def __init__(
        self, members: dict[int, Membership] | None = None, admins: set[int] | None = None
    ):
        self.members = members or {}
        self.admins = admins or set()
        self.calls: list[int] = []

    async def check(self, tg_id: int) -> Membership:
        self.calls.append(tg_id)
        return self.members.get(tg_id, Membership.NO)

    async def channel_admin_ids(self) -> set[int]:
        return set(self.admins)
