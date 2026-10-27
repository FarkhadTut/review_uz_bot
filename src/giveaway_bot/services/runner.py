"""Background work. Every wait is derived from persisted state, so a restart resumes:
committed and paused draws are resolved, interrupted results posts are finished, claim
windows expire and their alerts go out, retention batches continue."""

import asyncio
import html
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramUnauthorizedError,
)
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
from giveaway_bot.services.notices import Notices
from giveaway_bot.services.stats import Stats

log = logging.getLogger(__name__)

# Telegram answers that won't change by waiting (no rights, bad chat, rejected text):
# retrying them only spams admins and burns commitment rounds.
PERMANENT = (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramUnauthorizedError,
)
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
        notices: Notices | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        expiry_every: float = 60,
        retention_every: float | None = 6 * 3600,
        first_backoff: float = 15,
        max_backoff: float = 600,
    ) -> None:
        self.draws = draws
        self.claims = claims
        self.stats = stats
        self.notices = notices
        self.sleep = sleep
        self.expiry_every = expiry_every
        self.retention_every = retention_every
        self.first_backoff = first_backoff
        self.max_backoff = max_backoff
        self._tasks: set[asyncio.Task] = set()
        self._resolving: dict[int, asyncio.Task] = {}
        self._noticing: dict[int, asyncio.Task] = {}
        self._wakeups: defaultdict[int, asyncio.Event] = defaultdict(asyncio.Event)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def spawn_resolve(self, draw_id: int) -> None:
        """Resolve in the background until done. If a task for this draw is already waiting
        in backoff, wake it for an immediate attempt instead of starting a second one."""
        running = self._resolving.get(draw_id)
        if running is not None and not running.done():
            self._wakeups[draw_id].set()
            return
        self._resolving[draw_id] = self._spawn(self._resolve_with_retry(draw_id))

    async def _backoff(self, draw_id: int, seconds: float) -> None:
        """Sleep for the backoff delay, or less if an admin presses Retry."""
        wake = self._wakeups[draw_id]
        sleeper = asyncio.ensure_future(self.sleep(seconds))
        waiter = asyncio.ensure_future(wake.wait())
        try:
            await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            waiter.cancel()
            wake.clear()

    async def _alert(self, key: str, keyboard=None, **kw) -> None:
        """Alerts must never kill the loop that reports them."""
        try:
            await self.draws.alert_admins(key, keyboard, **kw)
        except Exception:
            log.exception("could not alert admins (%s)", key)

    async def _resolve_with_retry(self, draw_id: int) -> None:
        await self._notices_pass()  # the commitment is usually out by now
        delay = self.first_backoff
        alerted: set[str] = set()  # one alert per kind per task, not one per retry
        while True:
            try:
                await self.draws.resolve(draw_id)
                return
            except PERMANENT as e:
                await self._stop(draw_id, e)
                return
            except TRANSIENT as e:
                log.warning("draw %s: waiting (%s); retry in %ss", draw_id, _brief(e), delay)
                try:
                    draw = await self.draws.get(draw_id)
                except Exception:
                    draw = None
                if isinstance(e, BeaconError) and "beacon" not in alerted and draw:
                    alerted.add("beacon")
                    await self._alert(
                        "admin_beacon_down",
                        draw_no=draw_id,
                        round=draw.beacon_round,
                        error=_brief(e),
                    )
                if (
                    isinstance(e, CommitmentError | TelegramAPIError)
                    and draw
                    and draw.commit_msg_id is None
                    and "commit" not in alerted
                ):
                    alerted.add("commit")
                    await self._alert("admin_commit_pending", draw_no=draw_id)
            except Exception as e:
                await self._stop(draw_id, e)
                return
            await self._backoff(draw_id, delay)
            delay = min(delay * 2, self.max_backoff)

    async def _stop(self, draw_id: int, error: Exception) -> None:
        log.exception("draw %s: resolve stopped", draw_id, exc_info=error)
        await self._alert(
            "admin_resolve_failed",
            [[Button("btn_retry", f"adm:retry:{draw_id}")]],
            draw_no=draw_id,
            error=_brief(error),
        )

    async def _open_draws(self) -> list[int]:
        async with self.draws.sm() as s:
            return list(
                await s.scalars(
                    select(Draw.id).where(
                        Draw.status.in_((DrawStatus.COMMITTED, DrawStatus.PAUSED))
                    )
                )
            )

    async def start(self) -> None:
        for draw_id in await self._open_draws():
            log.info("resuming draw %s", draw_id)
            self.spawn_resolve(draw_id)
        self._spawn(self._every(self.expiry_every, self._housekeeping))
        if self.stats is not None and self.retention_every:
            self._spawn(self._every(self.retention_every, self.stats.run_retention_batch))

    async def _housekeeping(self) -> None:
        # Any open draw without a live resolve task (it died, or was never spawned) resumes.
        for draw_id in await self._open_draws():
            running = self._resolving.get(draw_id)
            if running is None or running.done():
                self.spawn_resolve(draw_id)
        for draw_id in await self.draws.pending_publications():
            try:
                await self.draws.finish_publishing(draw_id)
            except Exception:
                log.exception("draw %s: finishing the results post failed", draw_id)
        await self.claims.expire_due()
        await self.claims.remind_due()
        await self._notices_pass()

    async def _notices_pass(self) -> None:
        """Weekly notices run in their own task per draw, off the draw's path."""
        if self.notices is None:
            return
        try:
            pending = await self.notices.pending()
        except Exception:
            log.exception("could not list pending weekly notices")
            return
        for draw_id in pending:
            running = self._noticing.get(draw_id)
            if running is None or running.done():
                self._noticing[draw_id] = self._spawn(self._send_notice(draw_id))

    async def _send_notice(self, draw_id: int) -> None:
        try:
            await self.notices.send_weekly(draw_id)
        except Exception:
            log.exception("draw %s: weekly notice failed; housekeeping retries", draw_id)

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
