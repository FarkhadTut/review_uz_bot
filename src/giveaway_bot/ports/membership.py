"""Channel membership via the Bot API. The bot must be an administrator of the channel:
getChatMember is only guaranteed to work for other users in that case."""

import asyncio
import logging
import time
from enum import StrEnum
from typing import Protocol

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter

log = logging.getLogger(__name__)


class Membership(StrEnum):
    YES = "yes"
    NO = "no"  # never joined, or left on their own
    BANNED = "banned"  # removed by a channel admin; also "not subscribed", but recorded apart
    # The API failed. Callers must never treat this as NO: an outage must not cost
    # someone their entry or their prize.
    UNKNOWN = "unknown"


NOT_SUBSCRIBED = (Membership.NO, Membership.BANNED)


class MembershipChecker(Protocol):
    async def check(self, tg_id: int) -> Membership: ...

    async def channel_admin_ids(self, *, fresh: bool = False) -> set[int]: ...


_SUBSCRIBED = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
# Exact Bot API texts for "this user has no membership record". Anything else, notably
# "chat not found" (wrong CHANNEL_ID, bot lost access), is an outage, not an answer.
_NEVER_JOINED = ("user not found", "participant_id_invalid")


class TelegramMembership:
    def __init__(
        self,
        bot: Bot,
        channel_id: str | int,
        *,
        attempts: int = 3,
        admins_ttl: float = 300,
        max_inline_wait: float = 2,
    ) -> None:
        self.bot = bot
        self.channel_id = channel_id
        self.attempts = attempts
        self.admins_ttl = admins_ttl
        # Rate-limit waits longer than this end the check as UNKNOWN instead of stalling.
        self.max_inline_wait = max_inline_wait
        self._admins: tuple[float, set[int]] | None = None
        self._admins_lock = asyncio.Lock()

    async def check(self, tg_id: int) -> Membership:
        for attempt in range(1, self.attempts + 1):
            try:
                member = await self.bot.get_chat_member(self.channel_id, tg_id)
            except TelegramRetryAfter as e:
                if e.retry_after > self.max_inline_wait:
                    log.warning("getChatMember(%s) rate limited for %ss", tg_id, e.retry_after)
                    return Membership.UNKNOWN
                await asyncio.sleep(e.retry_after)
                continue
            except TelegramBadRequest as e:
                if any(text in e.message.lower() for text in _NEVER_JOINED):
                    return Membership.NO
                log.warning("getChatMember(%s) bad request: %s", tg_id, e.message)
            except TelegramAPIError as e:
                log.warning("getChatMember(%s) failed (try %s): %s", tg_id, attempt, e)
            else:
                if member.status in _SUBSCRIBED:
                    return Membership.YES
                if member.status == ChatMemberStatus.RESTRICTED:
                    return Membership.YES if getattr(member, "is_member", False) else Membership.NO
                if member.status == ChatMemberStatus.KICKED:
                    return Membership.BANNED
                return Membership.NO
            await asyncio.sleep(attempt)
        return Membership.UNKNOWN

    async def channel_admin_ids(self, *, fresh: bool = False) -> set[int]:
        """Cached briefly (called on every entry); fresh=True for the snapshot at close.
        On API failure the last known list is used rather than failing the caller."""
        async with self._admins_lock:
            expired = self._admins is None or time.monotonic() - self._admins[0] > self.admins_ttl
            if fresh or expired:
                try:
                    admins = await self.bot.get_chat_administrators(self.channel_id)
                except TelegramAPIError as e:
                    if self._admins is None:
                        raise
                    log.warning("getChatAdministrators failed, using cached list: %s", e)
                else:
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

    async def channel_admin_ids(self, *, fresh: bool = False) -> set[int]:
        return set(self.admins)
