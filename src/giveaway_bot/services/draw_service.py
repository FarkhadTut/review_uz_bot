"""Weekly draw: snapshot -> public commitment -> drand randomness -> winner -> results post.

Fairness rests on ordering: the snapshot hash and the beacon round are published before
that round's randomness exists, and nothing after the commitment can be cancelled.

Concurrency: aiogram handles updates concurrently, so every status change is a single
conditional UPDATE (`_cas`) that succeeds for exactly one caller. Creating draws (close,
redraw) is additionally serialized by an in-process lock; the bot runs as one process."""

import asyncio
import html
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from itertools import islice

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from giveaway_bot.db.models import (
    UNRESOLVED,
    AuditLog,
    Draw,
    DrawEntry,
    DrawStatus,
    User,
    UserStatus,
    utcnow,
)
from giveaway_bot.domain.draw import candidate_order, mask_name, snapshot_hash, winner_index
from giveaway_bot.i18n import CHANNEL_LANG, t
from giveaway_bot.ports.beacon import Beacon
from giveaway_bot.ports.membership import Membership, MembershipChecker
from giveaway_bot.ports.publisher import Button, Publisher
from giveaway_bot.services.participation import Participation

log = logging.getLogger(__name__)

# A commitment must be public at least this long before its round is published.
COMMIT_MARGIN_SECONDS = 15
COMMIT_ATTEMPTS = 3
# Longest list (skips, void rounds) printed inline in a channel post.
INLINE_LIST_LIMIT = 25
# Draws whose results are public: /verify may show their randomness and outcome.
PUBLIC_RESULT = (
    DrawStatus.PUBLISHED,
    DrawStatus.CLAIMED,
    DrawStatus.ACTIVATED,
    DrawStatus.EXPIRED,
    DrawStatus.REDRAWN,
    DrawStatus.NO_WINNER,
)


class DrawBlocked(Exception):
    def __init__(self, draw: Draw) -> None:
        super().__init__(f"draw {draw.id} is {draw.status}")
        self.draw = draw


class PoolEmpty(Exception):
    pass


class MembershipUnavailable(Exception):
    """Telegram couldn't confirm a candidate; the draw is PAUSED and must be retried."""


class CommitmentError(Exception):
    """The commitment couldn't be published ahead of its round; retried by the runner."""


class RedrawOutcome(StrEnum):
    CREATED = "created"
    NO_CANDIDATES = "no_candidates"
    NOT_ALLOWED = "not_allowed"  # not expired (too early, or already redrawn)


@dataclass(frozen=True)
class RedrawResult:
    outcome: RedrawOutcome
    draw: Draw | None = None


def _ts(dt: datetime) -> float:
    return dt.replace(tzinfo=UTC).timestamp()


class DrawService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        membership: MembershipChecker,
        beacon: Beacon,
        publisher: Publisher,
        participation: Participation,
        *,
        admin_ids: set[int],
        bot_username: str,
        claim_window_hours: int = 48,
        lead_seconds: int = 60,
        now: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.sm = sessionmaker
        self.membership = membership
        self.beacon = beacon
        self.pub = publisher
        self.participation = participation
        self.admin_ids = admin_ids
        self.bot_username = bot_username
        self.claim_window_hours = claim_window_hours
        self.lead_seconds = lead_seconds
        self.now = now
        self.sleep = sleep
        self._lifecycle = asyncio.Lock()
        self._commit_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._resolve_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    # --- queries -------------------------------------------------------------------------

    async def current(self) -> Draw | None:
        """The unresolved draw, if any (at most one exists)."""
        async with self.sm() as s:
            return await s.scalar(
                select(Draw).where(Draw.status.in_(UNRESOLVED)).order_by(Draw.id.desc()).limit(1)
            )

    async def get(self, draw_id: int) -> Draw | None:
        async with self.sm() as s:
            return await s.get(Draw, draw_id)

    async def latest_id(self) -> int:
        async with self.sm() as s:
            return await s.scalar(select(func.max(Draw.id))) or 0

    async def latest_public(self) -> Draw | None:
        async with self.sm() as s:
            return await s.scalar(
                select(Draw).where(Draw.status.in_(PUBLIC_RESULT)).order_by(Draw.id.desc()).limit(1)
            )

    async def entry_codes(self, draw_id: int) -> list[str]:
        async with self.sm() as s:
            rows = await s.scalars(
                select(DrawEntry.entry_code)
                .where(DrawEntry.draw_id == draw_id)
                .order_by(DrawEntry.position)
            )
            return list(rows)

    async def _excluded(self, *, fresh: bool = False) -> set[int]:
        return (
            self.admin_ids
            | await self.participation.staff_ids()
            | await self.membership.channel_admin_ids(fresh=fresh)
        ) - self.participation.allowed_ids

    def _eligible(self, excluded: set[int]):
        return (
            User.entry_code.is_not(None),
            User.status == UserStatus.ACTIVE,
            User.tg_id.not_in(excluded),
        )

    async def pool_count(self) -> int:
        """Entrants eligible right now: entered, never won, not staff/admin."""
        excluded = await self._excluded()
        async with self.sm() as s:
            return await s.scalar(
                select(func.count()).select_from(User).where(*self._eligible(excluded))
            )

    async def _pool_entries(self) -> list[tuple[str, int]]:
        excluded = await self._excluded(fresh=True)
        async with self.sm() as s:
            rows = await s.execute(
                select(User.entry_code, User.tg_id).where(*self._eligible(excluded))
            )
            return [(code, tg_id) for code, tg_id in rows]

    async def _entry(self, draw_id: int, position: int) -> DrawEntry:
        async with self.sm() as s:
            return await s.get(DrawEntry, (draw_id, position))

    async def _positions(self, draw_id: int) -> dict[int, int]:
        async with self.sm() as s:
            rows = await s.execute(
                select(DrawEntry.position, DrawEntry.tg_id).where(DrawEntry.draw_id == draw_id)
            )
            return {position: tg_id for position, tg_id in rows}

    # --- state changes -----------------------------------------------------------------------

    async def _cas(
        self,
        s: AsyncSession,
        draw_id: int,
        from_: Iterable[DrawStatus],
        to: DrawStatus | None = None,
        *conditions,
        **values,
    ) -> bool:
        """Apply values (and the new status) only if the draw is still in one of from_."""
        if to is not None:
            values["status"] = to
        result = await s.execute(
            update(Draw)
            .where(Draw.id == draw_id, Draw.status.in_(list(from_)), *conditions)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    async def audit(self, admin_id: int, action: str, **payload) -> None:
        async with self.sm() as s, s.begin():
            s.add(AuditLog(admin_id=admin_id, action=action, payload=payload, at=self.now()))

    # --- close + commit ------------------------------------------------------------------

    async def close_entries(self, admin_id: int) -> Draw:
        """Freeze the snapshot and publish the commitment. If the post fails the draw still
        exists (COMMITTED, no post yet) and resolve() publishes it before anything else."""
        async with self._lifecycle:
            if (blocking := await self.current()) is not None:
                raise DrawBlocked(blocking)
            entries = await self._pool_entries()
            if not entries:
                raise PoolEmpty
            async with self.sm() as s, s.begin():
                draw = await self._insert_draw(s, entries, admin_id=admin_id, parent_id=None)
        await self._try_commit(draw.id)
        return await self.get(draw.id)

    async def _insert_draw(
        self,
        s: AsyncSession,
        entries: list[tuple[str, int]],
        *,
        admin_id: int,
        parent_id: int | None,
    ) -> Draw:
        entries = sorted(entries)
        draw = Draw(
            parent_id=parent_id,
            status=DrawStatus.COMMITTED,
            closed_at=self.now(),
            closed_by=admin_id,
            n=len(entries),
            snapshot_hash=snapshot_hash([code for code, _ in entries]),
            beacon_round=self._future_round(),
            void_rounds=[],
            skipped=[],
            # Entrants of a redraw were already told about the parent draw.
            notice_done=parent_id is not None,
        )
        s.add(draw)
        await s.flush()
        await s.execute(
            insert(DrawEntry),
            [
                {"draw_id": draw.id, "position": i, "entry_code": code, "tg_id": tg_id}
                for i, (code, tg_id) in enumerate(entries)
            ],
        )
        s.add(
            AuditLog(
                admin_id=admin_id,
                action="redraw" if parent_id is not None else "close",
                payload={"draw_id": draw.id, "n": draw.n, "parent_id": parent_id},
                at=self.now(),
            )
        )
        return draw

    def _future_round(self) -> int:
        """The first round that starts at least lead_seconds from now."""
        return self.beacon.round_at(_ts(self.now()) + self.lead_seconds) + 1

    async def _try_commit(self, draw_id: int) -> None:
        try:
            await self.ensure_committed(draw_id)
        except Exception:
            log.exception("draw %s: commitment post failed; resolve will retry", draw_id)

    async def ensure_committed(self, draw_id: int) -> None:
        """Publish the commitment post if it is missing.

        The round chosen at close is kept while it is still comfortably ahead. If it isn't
        (an earlier post failed, or this post landed after the round was out), the round is
        voided and a later one committed, and the new post names the void rounds."""
        async with self._commit_locks[draw_id]:
            for _ in range(COMMIT_ATTEMPTS):
                draw = await self.get(draw_id)
                if draw.commit_msg_id is not None:
                    return
                if self.beacon.round_time(draw.beacon_round) - _ts(self.now()) < (
                    COMMIT_MARGIN_SECONDS
                ):
                    await self._replace_round(draw_id)
                    draw = await self.get(draw_id)
                msg_id = await self.pub.post_channel(self._commitment_text(draw))
                if self.beacon.round_time(draw.beacon_round) <= _ts(self.now()):
                    log.warning(
                        "draw %s: commitment landed after round %s", draw_id, draw.beacon_round
                    )
                    await self._replace_round(draw_id)
                    continue
                async with self.sm() as s, s.begin():
                    await self._cas(s, draw_id, UNRESOLVED, None, commit_msg_id=msg_id)
                return
            raise CommitmentError(f"draw {draw_id}: no timely commitment")

    async def _replace_round(self, draw_id: int) -> None:
        async with self.sm() as s, s.begin():
            draw = await s.get(Draw, draw_id)
            if draw.randomness is not None:  # never after randomness was fetched
                raise CommitmentError(f"draw {draw_id}: randomness already fetched")
            draw.void_rounds = [*draw.void_rounds, draw.beacon_round]
            draw.beacon_round = self._future_round()

    def _commitment_text(self, draw: Draw) -> str:
        text = t(
            CHANNEL_LANG,
            "channel_commit",
            draw_no=draw.id,
            n=draw.n,
            snapshot_hash=draw.snapshot_hash,
            round=draw.beacon_round,
            chain="quicknet",
        )
        if draw.void_rounds:
            void = t(
                CHANNEL_LANG,
                "channel_void_note",
                draw_no=draw.id,
                rounds=_rounds(draw.void_rounds),
            )
            text = f"{void}\n\n{text}"
        if draw.parent_id is not None:
            note = t(
                CHANNEL_LANG,
                "channel_redraw_note",
                parent_no=draw.parent_id,
                hours=self.claim_window_hours,
            )
            text = f"{note}\n\n{text}"
        return text

    # --- resolve ---------------------------------------------------------------------------

    async def resolve(self, draw_id: int) -> None:
        """Wait for the committed round, then pick the winner. Safe to call repeatedly.

        Raises MembershipUnavailable when paused, BeaconError / CommitmentError when the
        draw must wait; the runner retries all three."""
        async with self._resolve_locks[draw_id]:
            await self._resolve(draw_id)

    async def _resolve(self, draw_id: int) -> None:
        draw = await self.get(draw_id)
        if draw is None or draw.status not in (DrawStatus.COMMITTED, DrawStatus.PAUSED):
            return
        if draw.commit_msg_id is None:
            await self.ensure_committed(draw_id)
            draw = await self.get(draw_id)

        if draw.randomness is None:
            wait = self.beacon.round_time(draw.beacon_round) - _ts(self.now())
            if wait > 0:
                await self.sleep(wait)
            randomness = await self.beacon.randomness(draw.beacon_round)
            index = winner_index(randomness, draw.snapshot_hash, draw.n)
            async with self.sm() as s, s.begin():
                await self._cas(
                    s,
                    draw_id,
                    (DrawStatus.COMMITTED, DrawStatus.PAUSED),
                    None,
                    Draw.randomness.is_(None),
                    randomness=randomness,
                    winner_index=index,
                )
            draw = await self.get(draw_id)

        skipped = list(draw.skipped)
        open_ = (DrawStatus.COMMITTED, DrawStatus.PAUSED)
        # Continue after the last recorded skip, so a pause never re-tests earlier positions.
        start = (draw.winner_index + len(skipped)) % draw.n
        for position in islice(candidate_order(start, draw.n), draw.n - len(skipped)):
            entry = await self._entry(draw_id, position)
            status = await self.membership.check(entry.tg_id)
            if status is Membership.UNKNOWN:
                async with self.sm() as s, s.begin():
                    newly_paused = await self._cas(
                        s, draw_id, (DrawStatus.COMMITTED,), DrawStatus.PAUSED
                    )
                if newly_paused:
                    # The candidate is deliberately not named: knowing who is about to be
                    # checked would let an insider remove them before the retry.
                    await self.alert_admins(
                        "admin_paused",
                        [[Button("btn_retry", f"adm:retry:{draw_id}")]],
                        draw_no=draw_id,
                    )
                raise MembershipUnavailable(f"draw {draw_id} position {position}")
            if status is not Membership.YES:
                reason = "banned" if status is Membership.BANNED else "not_subscribed"
                skipped.append({"index": position, "code": entry.entry_code, "reason": reason})
                async with self.sm() as s, s.begin():
                    await self._cas(s, draw_id, open_, None, skipped=skipped)
                continue
            async with self.sm() as s, s.begin():
                won = await self._cas(
                    s,
                    draw_id,
                    open_,
                    DrawStatus.DRAWN,
                    winner_tg_id=entry.tg_id,
                    winner_position=position,
                    skipped=skipped,
                )
            if won:
                winner = await self.participation.get(entry.tg_id)
                await self.alert_admins(
                    "admin_drawn",
                    [[Button("btn_approve", f"adm:approve:{draw_id}")]],
                    draw_no=draw_id,
                    winner_full=html.escape(full_name(winner)),
                    tg_id=entry.tg_id,
                    position=position,
                    skipped=format_skips(skipped),
                )
            return
        async with self.sm() as s, s.begin():
            ended = await self._cas(s, draw_id, open_, DrawStatus.NO_WINNER, skipped=skipped)
        if ended:
            await self.finish_publishing(draw_id)

    # --- publish ---------------------------------------------------------------------------

    async def approve(self, draw_id: int, admin_id: int) -> bool | None:
        """Post the results and notify the winner. Returns whether the winner's DM was
        delivered, or None when the draw was not awaiting approval (e.g. a repeat press).
        Raises if the channel post fails; the draw is then back to DRAWN for another try."""
        async with self.sm() as s, s.begin():
            if not await self._cas(
                s,
                draw_id,
                (DrawStatus.DRAWN,),
                DrawStatus.PUBLISHED,
                published_at=None,
                result_msg_id=None,
            ):
                return None
            draw = await s.get(Draw, draw_id)
            (await s.get(User, draw.winner_tg_id)).status = UserStatus.WON
            s.add(
                AuditLog(
                    admin_id=admin_id, action="approve", payload={"draw_id": draw_id}, at=self.now()
                )
            )
        return await self.finish_publishing(draw_id)

    async def pending_publications(self) -> list[int]:
        """Draws whose results/no-winner post was interrupted (e.g. by a crash)."""
        async with self.sm() as s:
            rows = await s.scalars(
                select(Draw.id).where(
                    Draw.status.in_((DrawStatus.PUBLISHED, DrawStatus.NO_WINNER)),
                    Draw.result_msg_id.is_(None),
                    Draw.randomness.is_not(None),
                )
            )
            return list(rows)

    async def finish_publishing(self, draw_id: int) -> bool | None:
        """Post the results of a PUBLISHED (or NO_WINNER) draw that has no post yet.

        After a crash the post may already exist; posting it again is the lesser evil
        compared to a claim window running on a result nobody saw."""
        async with self._commit_locks[draw_id]:
            draw = await self.get(draw_id)
            if draw.result_msg_id is not None:
                return None
            if draw.status == DrawStatus.NO_WINNER:
                msg_id = await self.pub.post_channel(self._no_winner_text(draw))
                async with self.sm() as s, s.begin():
                    await self._cas(
                        s,
                        draw_id,
                        (DrawStatus.NO_WINNER,),
                        None,
                        result_msg_id=msg_id,
                        published_at=self.now(),
                    )
                await self.alert_admins("admin_no_winner", None, draw_no=draw_id)
                return None
            if draw.status != DrawStatus.PUBLISHED:
                return None
            winner = await self.participation.get(draw.winner_tg_id)
            try:
                msg_id = await self.pub.post_channel(self._results_text(draw, winner))
            except Exception:
                # Nothing was announced: undo, so the claim window doesn't run on an unseen
                # result and the admin can press Publish again.
                async with self.sm() as s, s.begin():
                    if await self._cas(
                        s,
                        draw_id,
                        (DrawStatus.PUBLISHED,),
                        DrawStatus.DRAWN,
                        Draw.result_msg_id.is_(None),
                    ):
                        (await s.get(User, draw.winner_tg_id)).status = UserStatus.ACTIVE
                raise
            async with self.sm() as s, s.begin():
                await self._cas(
                    s,
                    draw_id,
                    (DrawStatus.PUBLISHED,),
                    None,
                    result_msg_id=msg_id,
                    published_at=self.now(),
                )
        delivered = await self.pub.send(
            winner.tg_id, t(winner.lang, "win_dm", hours=self.claim_window_hours)
        )
        await self.alert_admins(
            "admin_published",
            None,
            draw_no=draw_id,
            dm_status_key="admin_dm_ok" if delivered else "admin_dm_failed",
        )
        return delivered

    def _skipped_line(self, draw: Draw) -> str:
        """Skips and void rounds for a public post, capped so the post stays well under
        Telegram's 4096-character limit; /verify N always has the full data."""
        line = ""
        if draw.skipped:
            shown = draw.skipped[:INLINE_LIST_LIMIT]
            items = ", ".join(
                f"{x['index']} (<code>{x.get('code', '?')}</code> — "
                f"{t(CHANNEL_LANG, 'channel_reason_' + x['reason'])})"
                for x in shown
            )
            if len(draw.skipped) > len(shown):
                items += t(CHANNEL_LANG, "channel_more", count=len(draw.skipped) - len(shown))
            line += t(CHANNEL_LANG, "channel_skipped_line", items=items)
        if draw.void_rounds:
            line += t(CHANNEL_LANG, "channel_void_line", rounds=_rounds(draw.void_rounds))
        return line

    def _results_text(self, draw: Draw, winner: User) -> str:
        return t(
            CHANNEL_LANG,
            "channel_results",
            draw_no=draw.id,
            winner=html.escape(
                mask_name(winner.first_name, winner.username, fallback=f"#{winner.entry_code[:4]}")
            ),
            n=draw.n,
            snapshot_hash=draw.snapshot_hash,
            round=draw.beacon_round,
            randomness=draw.randomness,
            index=draw.winner_index,
            skipped_line=self._skipped_line(draw),
            bot_link=f"@{self.bot_username}",
            hours=self.claim_window_hours,
        )

    def _no_winner_text(self, draw: Draw) -> str:
        return t(
            CHANNEL_LANG,
            "channel_no_winner",
            draw_no=draw.id,
            n=draw.n,
            snapshot_hash=draw.snapshot_hash,
            round=draw.beacon_round,
            randomness=draw.randomness,
            index=draw.winner_index,
            skipped_line=self._skipped_line(draw),
        )

    # --- redraw ----------------------------------------------------------------------------

    async def redraw(self, draw_id: int, admin_id: int) -> RedrawResult:
        """After an expired claim: same snapshot minus the no-show, new round, new commitment.
        The parent's transition and the child's creation are one transaction."""
        async with self._lifecycle, self.sm() as s, s.begin():
            if not await self._cas(s, draw_id, (DrawStatus.EXPIRED,), DrawStatus.REDRAWN):
                return RedrawResult(RedrawOutcome.NOT_ALLOWED)
            parent = await s.get(Draw, draw_id)
            # They never received a prize, so they stay eligible for future weeks.
            (await s.get(User, parent.winner_tg_id)).status = UserStatus.ACTIVE
            rows = await s.execute(
                select(DrawEntry.entry_code, DrawEntry.tg_id).where(
                    DrawEntry.draw_id == draw_id, DrawEntry.tg_id != parent.winner_tg_id
                )
            )
            remaining = [(code, tg_id) for code, tg_id in rows]
            if remaining:
                child = await self._insert_draw(s, remaining, admin_id=admin_id, parent_id=draw_id)
            else:
                parent.status = DrawStatus.NO_WINNER
                s.add(
                    AuditLog(
                        admin_id=admin_id,
                        action="redraw_empty",
                        payload={"draw_id": draw_id},
                        at=self.now(),
                    )
                )
        if not remaining:
            try:
                await self.pub.post_channel(
                    t(CHANNEL_LANG, "channel_redraw_empty", parent_no=draw_id)
                )
            except Exception as e:
                log.exception("draw %s: redraw-empty post failed", draw_id)
                await self.alert_admins(
                    "admin_publish_failed",
                    None,
                    draw_no=draw_id,
                    error=html.escape(f"{type(e).__name__}: {e}")[:200],
                )
            return RedrawResult(RedrawOutcome.NO_CANDIDATES)
        await self._try_commit(child.id)
        return RedrawResult(RedrawOutcome.CREATED, await self.get(child.id))

    # --- admin notifications --------------------------------------------------------------

    async def alert_admins(self, key: str, keyboard, **kw) -> None:
        """keyboard buttons carry i18n keys as text; they're translated per admin."""
        async with self.sm() as s:
            langs = {
                u.tg_id: u.lang
                for u in await s.scalars(select(User).where(User.tg_id.in_(self.admin_ids)))
            }
        for admin_id in self.admin_ids:
            lang = langs.get(admin_id)
            params = dict(kw)
            if "dm_status_key" in params:
                params["dm_status"] = t(lang, params.pop("dm_status_key"))
            kb = (
                [[Button(t(lang, b.text), b.callback, b.url) for b in row] for row in keyboard]
                if keyboard
                else None
            )
            await self.pub.send(admin_id, t(lang, key, **params), kb)


def full_name(user: User) -> str:
    return f"{user.first_name} @{user.username}" if user.username else user.first_name


def format_skips(skipped: list[dict]) -> str:
    return ", ".join(str(x["index"]) for x in skipped) or "—"


def _rounds(rounds: list[int]) -> str:
    if len(rounds) <= INLINE_LIST_LIMIT:
        return ", ".join(map(str, rounds))
    return f"{rounds[0]}…{rounds[-1]} ({len(rounds)})"
