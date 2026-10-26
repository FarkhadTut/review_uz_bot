"""Background work. Every wait is derived from persisted state, so a restart resumes:
committed draws are resolved, claim windows expire, retention batches continue."""

import asyncio
import logging

from sqlalchemy import select

from giveaway_bot.db.models import Draw, DrawStatus
from giveaway_bot.ports.beacon import BeaconError
from giveaway_bot.services.claims import Claims
from giveaway_bot.services.draw_service import DrawService
from giveaway_bot.services.stats import Stats

log = logging.getLogger(__name__)


class Runner:
    def __init__(
        self,
        draws: DrawService,
        claims: Claims,
        stats: Stats,
        *,
        expiry_every: float = 60,
        retention_every: float = 6 * 3600,
    ) -> None:
        self.draws = draws
        self.claims = claims
        self.stats = stats
        self.expiry_every = expiry_every
        self.retention_every = retention_every
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def spawn_resolve(self, draw_id: int) -> None:
        self._spawn(self._resolve_with_retry(draw_id))

    async def _resolve_with_retry(self, draw_id: int) -> None:
        delay = 15
        while True:
            try:
                await self.draws.resolve(draw_id)
                return
            except BeaconError as e:
                # No local fallback by design: the draw waits for the committed round.
                log.error("draw %s: beacon unavailable (%s); retrying in %ss", draw_id, e, delay)
            except Exception:
                log.exception("draw %s: resolve failed; retrying in %ss", draw_id, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 600)

    async def start(self) -> None:
        async with self.draws.sm() as s:
            pending = list(
                await s.scalars(select(Draw.id).where(Draw.status == DrawStatus.COMMITTED))
            )
        for draw_id in pending:
            log.info("resuming committed draw %s", draw_id)
            self.spawn_resolve(draw_id)
        self._spawn(self._every(self.expiry_every, self.claims.expire_due))
        self._spawn(self._every(self.retention_every, self.stats.run_retention_batch))

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
