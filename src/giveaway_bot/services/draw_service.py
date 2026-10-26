"""Weekly draw: snapshot -> public commitment -> drand randomness -> winner -> results post.

Fairness rests on ordering: the snapshot hash and the beacon round are published before
that round's randomness exists, and nothing after the commitment can be cancelled."""

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

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


class DrawBlocked(Exception):
    def __init__(self, draw: Draw) -> None:
        super().__init__(f"draw {draw.id} is {draw.status}")
        self.draw = draw


class PoolEmpty(Exception):
    pass


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
        self._resolve_lock = asyncio.Lock()

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

    async def latest_with_randomness(self) -> Draw | None:
        async with self.sm() as s:
            return await s.scalar(
                select(Draw).where(Draw.randomness.is_not(None)).order_by(Draw.id.desc()).limit(1)
            )

    async def entry_codes(self, draw_id: int) -> list[str]:
        async with self.sm() as s:
            rows = await s.scalars(
                select(DrawEntry.entry_code)
                .where(DrawEntry.draw_id == draw_id)
                .order_by(DrawEntry.position)
            )
            return list(rows)

    async def pool(self) -> list[User]:
        """Entrants eligible right now: entered, never won, not staff/admin."""
        excluded = (
            self.admin_ids
            | await self.participation.staff_ids()
            | await self.membership.channel_admin_ids()
        )
        async with self.sm() as s:
            users = await s.scalars(
                select(User).where(User.entry_code.is_not(None), User.status == UserStatus.ACTIVE)
            )
            return [u for u in users if u.tg_id not in excluded]

    # --- close + commit ------------------------------------------------------------------

    async def close_entries(self, admin_id: int) -> Draw:
        if (blocking := await self.current()) is not None:
            raise DrawBlocked(blocking)
        pool = await self.pool()
        if not pool:
            raise PoolEmpty
        draw = await self._create(
            [(u.entry_code, u.tg_id) for u in pool], admin_id=admin_id, parent_id=None
        )
        await self.ensure_committed(draw.id)
        return await self.get(draw.id)

    async def _create(
        self, entries: list[tuple[str, int]], *, admin_id: int, parent_id: int | None
    ) -> Draw:
        entries = sorted(entries)
        async with self.sm() as s, s.begin():
            # Re-check inside the transaction: two admins pressing Close at once.
            if parent_id is None and await s.scalar(
                select(func.count()).select_from(Draw).where(Draw.status.in_(UNRESOLVED))
            ):
                raise DrawBlocked(await self.current())
            draw = Draw(
                parent_id=parent_id,
                status=DrawStatus.COMMITTED,
                closed_at=self.now(),
                closed_by=admin_id,
                n=len(entries),
                snapshot_hash=snapshot_hash([code for code, _ in entries]),
                beacon_round=self._future_round(),
                skipped=[],
            )
            s.add(draw)
            await s.flush()
            s.add_all(
                DrawEntry(draw_id=draw.id, position=i, entry_code=code, tg_id=tg_id)
                for i, (code, tg_id) in enumerate(entries)
            )
            s.add(
                AuditLog(
                    admin_id=admin_id,
                    action="redraw" if parent_id else "close",
                    payload={"draw_id": draw.id, "n": draw.n, "parent_id": parent_id},
                    at=self.now(),
                )
            )
        return draw

    def _future_round(self) -> int:
        return self.beacon.round_at(_ts(self.now()) + self.lead_seconds)

    async def ensure_committed(self, draw_id: int) -> None:
        """Publish the commitment post if it is missing.

        If an earlier attempt failed, the old round may already be out, so a fresh future
        round is chosen first. Its randomness was never fetched, so this can't be steered."""
        draw = await self.get(draw_id)
        if draw.commit_msg_id is not None:
            return
        async with self.sm() as s, s.begin():
            d = await s.get(Draw, draw_id)
            d.beacon_round = self._future_round()
            round_no = d.beacon_round
        text = t(
            CHANNEL_LANG,
            "channel_commit",
            draw_no=draw.id,
            n=draw.n,
            snapshot_hash=draw.snapshot_hash,
            round=round_no,
            chain="quicknet",
        )
        if draw.parent_id is not None:
            note = t(
                CHANNEL_LANG,
                "channel_redraw_note",
                parent_no=draw.parent_id,
                hours=self.claim_window_hours,
            )
            text = f"{note}\n\n{text}"
        msg_id = await self.pub.post_channel(text)
        async with self.sm() as s, s.begin():
            (await s.get(Draw, draw_id)).commit_msg_id = msg_id

    # --- resolve ---------------------------------------------------------------------------

    async def resolve(self, draw_id: int) -> None:
        """Wait for the committed round, then pick the winner. Safe to call repeatedly."""
        async with self._resolve_lock:
            await self._resolve(draw_id)

    async def _resolve(self, draw_id: int) -> None:
        draw = await self.get(draw_id)
        if draw.status not in (DrawStatus.COMMITTED, DrawStatus.PAUSED):
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
                d = await s.get(Draw, draw_id)
                d.randomness, d.winner_index = randomness, index
            draw = await self.get(draw_id)

        positions = await self._positions(draw_id)
        skipped = list(draw.skipped)
        # Continue after the last recorded skip, so a pause never re-tests earlier positions.
        start = (draw.winner_index + len(skipped)) % draw.n
        for position in list(candidate_order(start, draw.n))[: draw.n - len(skipped)]:
            tg_id = positions[position]
            status = await self.membership.check(tg_id)
            if status is Membership.UNKNOWN:
                await self._set(draw_id, status=DrawStatus.PAUSED, skipped=skipped)
                await self.alert_admins(
                    "admin_paused",
                    [[Button("btn_retry", f"adm:retry:{draw_id}")]],
                    draw_no=draw_id,
                    tg_id=tg_id,
                )
                return
            if status is Membership.NO:
                skipped.append({"index": position, "reason": "not_subscribed"})
                continue
            await self._set(
                draw_id,
                status=DrawStatus.DRAWN,
                skipped=skipped,
                winner_tg_id=tg_id,
                winner_position=position,
            )
            winner = await self.participation.get(tg_id)
            await self.alert_admins(
                "admin_drawn",
                [[Button("btn_approve", f"adm:approve:{draw_id}")]],
                draw_no=draw_id,
                winner_full=html.escape(_full_name(winner)),
                tg_id=tg_id,
                position=position,
                skipped=_skips(skipped),
            )
            return
        await self._set(draw_id, status=DrawStatus.NO_WINNER, skipped=skipped)
        await self.alert_admins("admin_no_winner", None, draw_no=draw_id)

    async def _positions(self, draw_id: int) -> dict[int, int]:
        async with self.sm() as s:
            rows = await s.execute(
                select(DrawEntry.position, DrawEntry.tg_id).where(DrawEntry.draw_id == draw_id)
            )
            return {position: tg_id for position, tg_id in rows}

    async def _set(self, draw_id: int, **fields) -> None:
        async with self.sm() as s, s.begin():
            draw = await s.get(Draw, draw_id)
            for key, value in fields.items():
                setattr(draw, key, value)

    # --- publish ---------------------------------------------------------------------------

    async def approve(self, draw_id: int, admin_id: int) -> bool | None:
        """Post the results and notify the winner. Returns whether the winner's DM was
        delivered, or None when the draw was not awaiting approval (repeat press)."""
        async with self.sm() as s, s.begin():
            draw = await s.get(Draw, draw_id)
            if draw is None or draw.status != DrawStatus.DRAWN:
                return None
            # Claim the transition first so a double press can't post twice.
            draw.status = DrawStatus.PUBLISHED
            draw.published_at = self.now()
            winner = await s.get(User, draw.winner_tg_id)
            winner.status = UserStatus.WON
            s.add(
                AuditLog(
                    admin_id=admin_id, action="approve", payload={"draw_id": draw_id}, at=self.now()
                )
            )
        skipped_line = (
            t(
                CHANNEL_LANG,
                "channel_skipped_line",
                indexes=", ".join(str(x["index"]) for x in draw.skipped),
            )
            if draw.skipped
            else ""
        )
        text = t(
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
            skipped_line=skipped_line,
            bot_link=f"@{self.bot_username}",
            hours=self.claim_window_hours,
        )
        try:
            msg_id = await self.pub.post_channel(text)
        except Exception:
            # Nothing was announced: undo, so the claim window doesn't run on an unseen result
            # and the admin can press Publish again.
            async with self.sm() as s, s.begin():
                d = await s.get(Draw, draw_id)
                d.status, d.published_at = DrawStatus.DRAWN, None
                (await s.get(User, d.winner_tg_id)).status = UserStatus.ACTIVE
            raise
        await self._set(draw_id, result_msg_id=msg_id)
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

    # --- redraw ----------------------------------------------------------------------------

    async def redraw(self, draw_id: int, admin_id: int) -> Draw | None:
        """After an expired claim: same snapshot minus the no-show, new round, new commitment."""
        async with self.sm() as s, s.begin():
            parent = await s.get(Draw, draw_id)
            if parent is None or parent.status != DrawStatus.EXPIRED:
                return None
            parent.status = DrawStatus.REDRAWN
            no_show = await s.get(User, parent.winner_tg_id)
            # They never received a prize, so they stay eligible for future weeks.
            no_show.status = UserStatus.ACTIVE
            rows = await s.execute(
                select(DrawEntry.entry_code, DrawEntry.tg_id).where(
                    DrawEntry.draw_id == draw_id, DrawEntry.tg_id != parent.winner_tg_id
                )
            )
            remaining = [(code, tg_id) for code, tg_id in rows]
        if not remaining:
            await self._set(draw_id, status=DrawStatus.NO_WINNER)
            return None
        child = await self._create(remaining, admin_id=admin_id, parent_id=draw_id)
        await self.ensure_committed(child.id)
        return await self.get(child.id)

    # --- admin notifications --------------------------------------------------------------

    async def alert_admins(self, key: str, keyboard, **kw) -> None:
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


def _full_name(user: User) -> str:
    return f"{user.first_name} @{user.username}" if user.username else user.first_name


def _skips(skipped: list[dict]) -> str:
    return ", ".join(str(x["index"]) for x in skipped) or "—"
