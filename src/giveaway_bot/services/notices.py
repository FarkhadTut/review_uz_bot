"""The weekly "you're in this draw" notice to every entrant of a newly committed draw.

Each entry is claimed (notified = true) before its message goes out, so a crash or a
second run never sends twice; at worst one message is lost. Delivery is throttled and runs
in its own task, never on the draw's path."""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from giveaway_bot.db.models import Draw, DrawEntry, User
from giveaway_bot.i18n import t
from giveaway_bot.ports.publisher import Button, Keyboard, Publisher

log = logging.getLogger(__name__)


def notify_toggle(lang: str | None, enabled: bool) -> Button:
    """The mute/unmute button; its label offers the opposite of the current setting."""
    if enabled:
        return Button(t(lang, "btn_notify_off"), "notify:off")
    return Button(t(lang, "btn_notify_on"), "notify:on")


def notice_keyboard(lang: str | None, enabled: bool, link: str | None) -> Keyboard:
    rows = [[notify_toggle(lang, enabled)]]
    if link:
        rows.insert(0, [Button(t(lang, "btn_view_post"), url=link)])
    return rows


class Notices:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        publisher: Publisher,
        *,
        per_second: float = 20.0,
        batch: int = 200,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.sm = sessionmaker
        self.pub = publisher
        self.interval = 1 / per_second
        self.batch = batch
        self.sleep = sleep
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def pending(self) -> list[int]:
        """Committed first-run draws whose notice hasn't finished."""
        async with self.sm() as s:
            return list(
                await s.scalars(
                    select(Draw.id).where(
                        Draw.commit_msg_id.is_not(None),
                        Draw.parent_id.is_(None),
                        Draw.notice_done.is_(False),
                    )
                )
            )

    async def send_weekly(self, draw_id: int) -> int:
        """Notify every not-yet-handled entrant of the draw; returns messages delivered."""
        async with self._locks[draw_id]:
            async with self.sm() as s:
                draw = await s.get(Draw, draw_id)
            if draw is None or draw.notice_done or draw.commit_msg_id is None:
                return 0
            link = self.pub.post_link(draw.commit_msg_id)
            delivered = 0
            while rows := await self._next_batch(draw_id):
                for position, tg_id, code, notify, lang in rows:
                    if not await self._claim(draw_id, position) or not notify:
                        continue
                    if await self.pub.send(
                        tg_id,
                        t(lang, "weekly_notice", draw_no=draw_id, code=code),
                        notice_keyboard(lang, True, link),
                    ):
                        delivered += 1
                    await self.sleep(self.interval)
            async with self.sm() as s, s.begin():
                await s.execute(update(Draw).where(Draw.id == draw_id).values(notice_done=True))
            log.info("draw %s: weekly notice delivered to %s entrants", draw_id, delivered)
            return delivered

    async def _next_batch(self, draw_id: int) -> list[tuple]:
        async with self.sm() as s:
            result = await s.execute(
                select(
                    DrawEntry.position,
                    DrawEntry.tg_id,
                    DrawEntry.entry_code,
                    User.notify,
                    User.lang,
                )
                .join(User, User.tg_id == DrawEntry.tg_id)
                .where(DrawEntry.draw_id == draw_id, DrawEntry.notified.is_(False))
                .order_by(DrawEntry.position)
                .limit(self.batch)
            )
            return [tuple(row) for row in result]

    async def _claim(self, draw_id: int, position: int) -> bool:
        async with self.sm() as s, s.begin():
            result = await s.execute(
                update(DrawEntry)
                .where(
                    DrawEntry.draw_id == draw_id,
                    DrawEntry.position == position,
                    DrawEntry.notified.is_(False),
                )
                .values(notified=True)
            )
            return result.rowcount == 1
