from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from giveaway_bot.db.models import AuditLog, Staff, User, UserStatus, utcnow
from giveaway_bot.domain.codes import new_entry_code
from giveaway_bot.ports.membership import NOT_SUBSCRIBED, Membership, MembershipChecker


class EnterOutcome(StrEnum):
    ENTERED = "entered"
    ALREADY_ENTERED = "already_entered"
    NOT_SUBSCRIBED = "not_subscribed"
    NOT_ELIGIBLE = "not_eligible"
    ALREADY_WON = "already_won"
    CHECK_FAILED = "check_failed"


@dataclass(frozen=True)
class EnterResult:
    outcome: EnterOutcome
    code: str | None = None


class Participation:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        membership: MembershipChecker,
        *,
        bot_admin_ids: set[int],
        allowed_ids: set[int] = frozenset(),
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.sm = sessionmaker
        self.membership = membership
        self.bot_admin_ids = bot_admin_ids
        # Exempt from every exclusion (ADMINS_CAN_ENTER, for testing only).
        self.allowed_ids = allowed_ids
        self.now = now

    async def get(self, tg_id: int) -> User | None:
        async with self.sm() as s:
            return await s.get(User, tg_id)

    async def register_start(
        self, tg_id: int, first_name: str, username: str | None, source: str | None = None
    ) -> User:
        """Create the user on first /start, recording channel membership at that moment.
        Later calls only refresh the display name.

        The Telegram check runs before any DB session is opened, so a slow API call never
        holds a pooled connection; a concurrent first /start loses the insert harmlessly."""
        existing = await self.get(tg_id)
        if existing is None:
            status = await self.membership.check(tg_id)
            async with self.sm() as s, s.begin():
                await s.execute(
                    sqlite_insert(User)
                    .values(
                        tg_id=tg_id,
                        first_name=first_name,
                        username=username,
                        source=source,
                        first_start_at=self.now(),
                        status=UserStatus.ACTIVE,
                        member_at_first_start=(
                            None if status is Membership.UNKNOWN else status is Membership.YES
                        ),
                    )
                    .on_conflict_do_nothing(index_elements=[User.tg_id])
                )
            return await self.get(tg_id)
        async with self.sm() as s, s.begin():
            user = await s.get(User, tg_id)
            user.first_name, user.username = first_name, username
        return user

    async def set_lang(self, tg_id: int, lang: str) -> None:
        async with self.sm() as s:
            user = await s.get(User, tg_id)
            if user is not None:
                user.lang = lang
                await s.commit()

    async def set_notify(self, tg_id: int, enabled: bool) -> None:
        async with self.sm() as s:
            user = await s.get(User, tg_id)
            if user is not None:
                user.notify = enabled
                await s.commit()

    async def is_excluded(self, tg_id: int) -> bool:
        if tg_id in self.allowed_ids:
            return False
        if tg_id in self.bot_admin_ids or tg_id in await self.staff_ids():
            return True
        return tg_id in await self.membership.channel_admin_ids()

    async def enter(self, tg_id: int) -> EnterResult:
        user = await self.get(tg_id)
        if user is None:
            raise LookupError(f"user {tg_id} has not pressed /start")
        if user.status == UserStatus.WON:
            return EnterResult(EnterOutcome.ALREADY_WON)
        # Exclusion first: someone who entered and later became staff/channel admin must be
        # told they're not eligible, not that they're participating.
        if await self.is_excluded(tg_id):
            return EnterResult(EnterOutcome.NOT_ELIGIBLE)
        if user.entry_code:
            return EnterResult(EnterOutcome.ALREADY_ENTERED, user.entry_code)
        status = await self.membership.check(tg_id)
        if status is Membership.UNKNOWN:
            return EnterResult(EnterOutcome.CHECK_FAILED)
        if status in NOT_SUBSCRIBED:
            return EnterResult(EnterOutcome.NOT_SUBSCRIBED)
        return EnterResult(EnterOutcome.ENTERED, await self._assign_code(tg_id))

    async def _assign_code(self, tg_id: int) -> str:
        for _ in range(5):  # 32^8 codes: a collision is astronomically rare, retry anyway
            async with self.sm() as s:
                user = await s.get(User, tg_id)
                if user.entry_code:  # a concurrent press won the race
                    return user.entry_code
                user.entry_code, user.entered_at = new_entry_code(), self.now()
                try:
                    await s.commit()
                except IntegrityError:
                    await s.rollback()
                    continue
                return user.entry_code
        raise RuntimeError("could not allocate a unique entry code")

    async def staff_ids(self) -> set[int]:
        async with self.sm() as s:
            return set((await s.scalars(select(Staff.tg_id))).all())

    async def add_staff(self, tg_id: int, *, added_by: int) -> None:
        async with self.sm() as s:
            if await s.get(Staff, tg_id) is None:
                s.add(Staff(tg_id=tg_id, added_by=added_by, added_at=self.now()))
                s.add(
                    AuditLog(
                        admin_id=added_by,
                        action="staff_add",
                        payload={"tg_id": tg_id},
                        at=self.now(),
                    )
                )
                await s.commit()

    async def remove_staff(self, tg_id: int, *, removed_by: int = 0) -> bool:
        async with self.sm() as s:
            result = await s.execute(delete(Staff).where(Staff.tg_id == tg_id))
            if result.rowcount:
                s.add(
                    AuditLog(
                        admin_id=removed_by,
                        action="staff_del",
                        payload={"tg_id": tg_id},
                        at=self.now(),
                    )
                )
            await s.commit()
            return result.rowcount > 0
