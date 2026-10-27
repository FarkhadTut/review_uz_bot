"""Prize claim: the winner sends their review.uz login within the claim window;
staff activate the subscription by hand and mark it here.

Status changes go through DrawService._cas, so concurrent presses/messages and the
expiry loop can't both win the same transition."""

import html
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from giveaway_bot.db.models import AuditLog, Claim, Draw, DrawStatus, User, utcnow
from giveaway_bot.domain.codes import normalize_phone
from giveaway_bot.i18n import t
from giveaway_bot.ports.publisher import Button, ContactRequest, Publisher
from giveaway_bot.services.draw_service import DrawService, full_name


class ClaimOutcome(StrEnum):
    ACCEPTED = "accepted"
    INVALID = "invalid"
    TOO_LATE = "too_late"
    NOT_A_WINNER = "not_a_winner"
    ALREADY_RECEIVED = "already_received"


@dataclass(frozen=True)
class ClaimResult:
    outcome: ClaimOutcome
    contact: str | None = None


class Claims:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        publisher: Publisher,
        draws: DrawService,
        *,
        admin_ids: set[int],
        window_hours: int = 48,
        reminder_hours: int = 12,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.sm = sessionmaker
        self.pub = publisher
        self.draws = draws
        self.admin_ids = admin_ids
        self.window = timedelta(hours=window_hours)
        self.window_hours = window_hours
        self.reminder = timedelta(hours=reminder_hours)
        self.now = now

    async def claim(self, draw_id: int) -> Claim | None:
        async with self.sm() as s:
            return await s.get(Claim, draw_id)

    def deadline(self, draw: Draw) -> datetime:
        return draw.published_at + self.window

    async def open_claim_draw(self, tg_id: int) -> Draw | None:
        """The announced draw this user won and hasn't claimed (deadline not checked)."""
        async with self.sm() as s:
            return await s.scalar(
                select(Draw).where(
                    Draw.winner_tg_id == tg_id,
                    Draw.status.in_((DrawStatus.PUBLISHED, DrawStatus.EXPIRED)),
                    Draw.published_at.is_not(None),
                )
            )

    async def pending_claim(self, tg_id: int) -> Draw | None:
        """The draw this user can still claim right now, if any."""
        draw = await self.open_claim_draw(tg_id)
        if draw is None or draw.status != DrawStatus.PUBLISHED or self._late(draw):
            return None
        return draw

    async def already_claimed(self, tg_id: int) -> bool:
        async with self.sm() as s:
            return (
                await s.scalar(
                    select(Draw.id).where(
                        Draw.winner_tg_id == tg_id,
                        Draw.status.in_((DrawStatus.CLAIMED, DrawStatus.ACTIVATED)),
                    )
                )
                is not None
            )

    def _late(self, draw: Draw) -> bool:
        return self.now() > self.deadline(draw)

    async def submit(self, tg_id: int, raw: str) -> ClaimResult:
        """Record the phone from the winner's shared Telegram contact."""
        draw = await self.open_claim_draw(tg_id)
        if draw is None:
            if await self.already_claimed(tg_id):
                return ClaimResult(ClaimOutcome.ALREADY_RECEIVED)
            return ClaimResult(ClaimOutcome.NOT_A_WINNER)
        if draw.status == DrawStatus.EXPIRED or self._late(draw):
            return ClaimResult(ClaimOutcome.TOO_LATE)
        contact = normalize_phone(raw)
        if contact is None:
            return ClaimResult(ClaimOutcome.INVALID)
        try:
            async with self.sm() as s, s.begin():
                if not await self.draws._cas(
                    s, draw.id, (DrawStatus.PUBLISHED,), DrawStatus.CLAIMED
                ):
                    # Claimed by a concurrent message, or expired in between.
                    current = await s.get(Draw, draw.id)
                    if current.status == DrawStatus.EXPIRED:
                        return ClaimResult(ClaimOutcome.TOO_LATE)
                    return ClaimResult(ClaimOutcome.ALREADY_RECEIVED)
                s.add(Claim(draw_id=draw.id, contact=contact, submitted_at=self.now()))
                winner = await s.get(User, tg_id)
        except IntegrityError:
            return ClaimResult(ClaimOutcome.ALREADY_RECEIVED)
        # Staff call the winner, help them register on review.uz and get their login;
        # activation is marked later from /admin.
        await self.draws.alert_admins(
            "admin_claim",
            [[Button("btn_write_winner", url=f"https://t.me/{winner.username}")]]
            if winner.username
            else None,
            draw_no=draw.id,
            winner_full=html.escape(full_name(winner)),
            contact=html.escape(contact),
        )
        return ClaimResult(ClaimOutcome.ACCEPTED, contact)

    async def activate(self, draw_id: int, admin_id: int) -> bool:
        async with self.sm() as s, s.begin():
            if not await self.draws._cas(s, draw_id, (DrawStatus.CLAIMED,), DrawStatus.ACTIVATED):
                return False
            draw = await s.get(Draw, draw_id)
            claim = await s.get(Claim, draw_id)
            claim.activated_at, claim.activated_by = self.now(), admin_id
            winner = await s.get(User, draw.winner_tg_id)
            s.add(
                AuditLog(
                    admin_id=admin_id,
                    action="activate",
                    payload={"draw_id": draw_id},
                    at=self.now(),
                )
            )
        await self.pub.send(winner.tg_id, t(winner.lang, "prize_activated"))
        return True

    async def _reminder_candidates(self, now: datetime) -> list[int]:
        async with self.sm() as s:
            return list(
                await s.scalars(
                    select(Draw.id).where(
                        Draw.status == DrawStatus.PUBLISHED,
                        Draw.result_msg_id.is_not(None),
                        Draw.published_at <= now - (self.window - self.reminder),
                        Draw.published_at > now - self.window,
                        Draw.reminder_sent.is_(False),
                    )
                )
            )

    async def remind_due(self) -> list[int]:
        """Remind an unclaimed winner once, reminder_hours before the deadline (mute doesn't
        apply). The flag is claimed before sending: at most one reminder per draw."""
        now = self.now()
        due = await self._reminder_candidates(now)
        reminded = []
        for draw_id in due:
            async with self.sm() as s, s.begin():
                if not await self.draws._cas(
                    s,
                    draw_id,
                    (DrawStatus.PUBLISHED,),
                    None,
                    Draw.reminder_sent.is_(False),
                    reminder_sent=True,
                ):
                    continue  # claimed or expired in between
                draw = await s.get(Draw, draw_id)
                winner = await s.get(User, draw.winner_tg_id)
            left = (draw.published_at + self.window - now).total_seconds() / 3600
            await self.pub.send(
                winner.tg_id,
                t(winner.lang, "claim_reminder", hours=max(1, math.ceil(left))),
                ContactRequest(t(winner.lang, "btn_share_contact")),
            )
            reminded.append(draw_id)
        return reminded

    async def _notify_missed(self) -> None:
        """Tell each winner whose window expired, once (mute doesn't apply). Covers draws an
        admin already redrew before this pass ran."""
        missed_states = (DrawStatus.EXPIRED, DrawStatus.REDRAWN, DrawStatus.NO_WINNER)
        async with self.sm() as s:
            missed = list(
                await s.scalars(
                    select(Draw.id).where(
                        Draw.status.in_(missed_states),
                        Draw.winner_tg_id.is_not(None),
                        Draw.published_at.is_not(None),
                        Draw.missed_notified.is_(False),
                    )
                )
            )
        for draw_id in missed:
            async with self.sm() as s, s.begin():
                if not await self.draws._cas(
                    s,
                    draw_id,
                    missed_states,
                    None,
                    Draw.missed_notified.is_(False),
                    missed_notified=True,
                ):
                    continue
                draw = await s.get(Draw, draw_id)
                winner = await s.get(User, draw.winner_tg_id)
            await self.pub.send(
                winner.tg_id, t(winner.lang, "prize_missed", hours=self.window_hours)
            )

    async def expire_due(self) -> list[int]:
        """Expire announced-but-unclaimed draws past the window, then alert admins about every
        expired draw not yet alerted (including ones whose alert was lost in a crash).
        Returns the ids alerted in this pass."""
        deadline = self.now() - self.window
        async with self.sm() as s, s.begin():
            await s.execute(
                update(Draw)
                .where(
                    Draw.status == DrawStatus.PUBLISHED,
                    Draw.published_at.is_not(None),
                    Draw.published_at < deadline,
                )
                .values(status=DrawStatus.EXPIRED, expiry_notified=False)
                .execution_options(synchronize_session=False)
            )
        async with self.sm() as s:
            to_alert = list(
                await s.scalars(
                    select(Draw.id).where(
                        Draw.status == DrawStatus.EXPIRED, Draw.expiry_notified.is_(False)
                    )
                )
            )
        for draw_id in to_alert:
            await self.draws.alert_admins(
                "admin_expired",
                [[Button("btn_redraw", f"adm:redraw:{draw_id}")]],
                draw_no=draw_id,
                hours=self.window_hours,
            )
            async with self.sm() as s, s.begin():
                await self.draws._cas(s, draw_id, (DrawStatus.EXPIRED,), None, expiry_notified=True)
        await self._notify_missed()
        return to_alert
