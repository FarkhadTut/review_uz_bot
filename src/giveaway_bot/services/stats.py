"""Growth metrics from the PRD: entrants who were new to the channel at first /start,
and whether they are still subscribed after the retention period."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from giveaway_bot.db.models import MembershipCheck, User, UserStatus, utcnow
from giveaway_bot.ports.membership import Membership, MembershipChecker
from giveaway_bot.services.participation import Participation

RETENTION = "retention30"


@dataclass(frozen=True)
class StatsSnapshot:
    started: int
    entered: int
    new_to_channel: int
    pool: int
    winners: int
    retained: int
    retention_checked: int


class Stats:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        membership: MembershipChecker,
        participation: Participation,
        *,
        admin_ids: set[int],
        retention_days: int = 30,
        checks_per_second: float = 0,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.sm = sessionmaker
        self.membership = membership
        self.participation = participation
        self.admin_ids = admin_ids
        self.retention = timedelta(days=retention_days)
        self.delay = 1 / checks_per_second if checks_per_second else 0
        self.now = now

    async def snapshot(self) -> StatsSnapshot:
        new_to_channel = User.entry_code.is_not(None) & User.member_at_first_start.is_(False)
        async with self.sm() as s:

            async def count(*where) -> int:
                return await s.scalar(select(func.count()).select_from(User).where(*where))

            started = await count()
            entered = await count(User.entry_code.is_not(None))
            new = await count(new_to_channel)
            winners = await count(User.status == UserStatus.WON)
            checks = (
                await s.execute(
                    select(MembershipCheck.subscribed, func.count())
                    .where(MembershipCheck.kind == RETENTION)
                    .group_by(MembershipCheck.subscribed)
                )
            ).all()
        by_result = dict(checks)
        excluded = (
            self.admin_ids | await self.participation.staff_ids()
        ) - self.participation.allowed_ids
        async with self.sm() as s:
            pool = await s.scalar(
                select(func.count())
                .select_from(User)
                .where(
                    User.entry_code.is_not(None),
                    User.status == UserStatus.ACTIVE,
                    User.tg_id.not_in(excluded),
                )
            )
        return StatsSnapshot(
            started=started,
            entered=entered,
            new_to_channel=new,
            pool=pool,
            winners=winners,
            retained=by_result.get(True, 0),
            retention_checked=sum(by_result.values()),
        )

    async def run_retention_batch(self, limit: int = 500) -> int:
        """Check new-to-channel entrants who passed the retention period once. Returns the
        number recorded; API failures are skipped and retried on the next batch."""
        cutoff = self.now() - self.retention
        checked = select(MembershipCheck.tg_id).where(MembershipCheck.kind == RETENTION)
        async with self.sm() as s:
            due = list(
                await s.scalars(
                    select(User.tg_id)
                    .where(
                        User.member_at_first_start.is_(False),
                        User.entered_at.is_not(None),
                        User.entered_at <= cutoff,
                        User.tg_id.not_in(checked),
                    )
                    .limit(limit)
                )
            )
        recorded = 0
        for tg_id in due:
            status = await self.membership.check(tg_id)
            if status is not Membership.UNKNOWN:
                async with self.sm() as s, s.begin():
                    s.add(
                        MembershipCheck(
                            tg_id=tg_id,
                            kind=RETENTION,
                            subscribed=status is Membership.YES,
                            checked_at=self.now(),
                        )
                    )
                recorded += 1
            if self.delay:
                await asyncio.sleep(self.delay)
        return recorded
