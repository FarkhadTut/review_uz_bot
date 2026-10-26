"""Background work. Every wait is derived from persisted state, so a restart resumes:
committed and paused draws are resolved, interrupted results posts are finished, claim
windows expire and their alerts go out, retention batches continue."""

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable

from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from giveaway_bot.db.models import Draw, DrawStatus
from giveaway_bot.ports.beacon import BeaconError
from giveaway_bot.ports.publisher import Button
from giveaway_bot.services.claims import Claims
from giveaway_bot.services.draw_service import (
    CommitmentError,
    DrawService,
    MembershipUnavailable,
)
from giveaway_bot.services.stats import Stats

log = logging.getLogger(__name__)

# Conditions that resolve by waiting. Anything else is a bug: alert and stop retrying.
TRANSIENT = (
    BeaconError,
    MembershipUnavailable,
    CommitmentError,
    TelegramAPIError,
    OperationalError,
)


def _brief(error: Exception) -> str:
    return html.escape(f"{type(error).__name__}: {error}")[:200]


class Runner:
    def __init__(
        self,
        draws: DrawService,
        claims: Claims,
        stats: Stats | None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        expiry_every: float = 60,
        retention_every: float | None = 6 * 3600,
        first_backoff: float = 15,
        max_backoff: float = 600,
    ) -> None:
        self.draws = draws
        self.claims = claims
        self.stats = stats
        self.sleep = sleep
        self.expiry_every = expiry_every
        self.retention_every = retention_every
        self.first_backoff = first_backoff
        self.max_backoff = max_backoff
        self._tasks: set[asyncio.Task] = set()
        self._resolving: dict[int, asyncio.Task] = {}

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def spawn_resolve(self, draw_id: int) -> None:
        """Resolve in the background until done; a second call while running is a no-op."""
        running = self._resolving.get(draw_id)
        if running is not None and not running.done():
            return
        self._resolving[draw_id] = self._spawn(self._resolve_with_retry(draw_id))

    async def _resolve_with_retry(self, draw_id: int) -> None:
        delay = self.first_backoff
        beacon_alerted = False
        while True:
            try:
                await self.draws.resolve(draw_id)
                return
            except TRANSIENT as e:
                log.warning("draw %s: waiting (%s); retry in %ss", draw_id, _brief(e), delay)
                if isinstance(e, BeaconError) and not beacon_alerted:
                    draw = await self.draws.get(draw_id)
                    await self.draws.alert_admins(
                        "admin_beacon_down",
                        None,
                        draw_no=draw_id,
                        round=draw.beacon_round,
                        error=_brief(e),
                    )
                    beacon_alerted = True
                if isinstance(e, CommitmentError | TelegramAPIError):
                    draw = await self.draws.get(draw_id)
                    if draw is not None and draw.commit_msg_id is None:
                        await self.draws.alert_admins("admin_commit_pending", None, draw_no=draw_id)
            except Exception as e:
                log.exception("draw %s: resolve failed", draw_id)
                await self.draws.alert_admins(
                    "admin_resolve_failed",
                    [[Button("btn_retry", f"adm:retry:{draw_id}")]],
                    draw_no=draw_id,
                    error=_brief(e),
                )
                return
            await self.sleep(delay)
            delay = min(delay * 2, self.max_backoff)

    async def start(self) -> None:
        async with self.draws.sm() as s:
            pending = list(
                await s.scalars(
                    select(Draw.id).where(
                        Draw.status.in_((DrawStatus.COMMITTED, DrawStatus.PAUSED))
                    )
                )
            )
        for draw_id in pending:
            log.info("resuming draw %s", draw_id)
            self.spawn_resolve(draw_id)
        self._spawn(self._every(self.expiry_every, self._housekeeping))
        if self.stats is not None and self.retention_every:
            self._spawn(self._every(self.retention_every, self.stats.run_retention_batch))

    async def _housekeeping(self) -> None:
        for draw_id in await self.draws.pending_publications():
            try:
                await self.draws.finish_publishing(draw_id)
            except Exception:
                log.exception("draw %s: finishing the results post failed", draw_id)
        await self.claims.expire_due()

    async def _every(self, seconds: float, job) -> None:
        while True:
            try:
                await job()
            except Exception:
                log.exception("periodic job %s failed", getattr(job, "__name__", job))
            await asyncio.sleep(seconds)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
