"""Prize claim: the winner sends their review.uz login within the claim window;
staff activate the subscription by hand and mark it here."""

import html
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from giveaway_bot.db.models import AuditLog, Claim, Draw, DrawStatus, User, utcnow
from giveaway_bot.domain.codes import normalize_contact
from giveaway_bot.i18n import t
from giveaway_bot.ports.publisher import Button, Publisher
from giveaway_bot.services.draw_service import DrawService


class ClaimOutcome(StrEnum):
    ACCEPTED = "accepted"
    INVALID = "invalid"
    TOO_LATE = "too_late"
    NOT_A_WINNER = "not_a_winner"


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
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.sm = sessionmaker
        self.pub = publisher
        self.draws = draws
        self.admin_ids = admin_ids
        self.window = timedelta(hours=window_hours)
        self.window_hours = window_hours
        self.now = now

    async def open_claim_draw(self, tg_id: int) -> Draw | None:
        """The published draw this user won and hasn't claimed yet (deadline not checked)."""
        async with self.sm() as s:
            return await s.scalar(
                select(Draw).where(
                    Draw.winner_tg_id == tg_id,
                    Draw.status.in_((DrawStatus.PUBLISHED, DrawStatus.EXPIRED)),
                )
            )

    async def submit(self, tg_id: int, raw: str) -> ClaimResult:
        draw = await self.open_claim_draw(tg_id)
        if draw is None:
            return ClaimResult(ClaimOutcome.NOT_A_WINNER)
        if draw.status == DrawStatus.EXPIRED or self.now() > draw.published_at + self.window:
            return ClaimResult(ClaimOutcome.TOO_LATE)
        contact = normalize_contact(raw)
        if contact is None:
            return ClaimResult(ClaimOutcome.INVALID)
        async with self.sm() as s, s.begin():
            d = await s.get(Draw, draw.id)
            if d.status != DrawStatus.PUBLISHED:  # expired between the two reads
                return ClaimResult(ClaimOutcome.TOO_LATE)
            d.status = DrawStatus.CLAIMED
            s.add(Claim(draw_id=draw.id, contact=contact, submitted_at=self.now()))
            winner = await s.get(User, tg_id)
        await self.draws.alert_admins(
            "admin_claim",
            [[Button("btn_activate", f"adm:activate:{draw.id}")]],
            draw_no=draw.id,
            winner_full=html.escape(
                f"{winner.first_name} @{winner.username}" if winner.username else winner.first_name
            ),
            contact=html.escape(contact),
        )
        return ClaimResult(ClaimOutcome.ACCEPTED, contact)

    async def activate(self, draw_id: int, admin_id: int) -> bool:
        async with self.sm() as s, s.begin():
            draw = await s.get(Draw, draw_id)
            if draw is None or draw.status != DrawStatus.CLAIMED:
                return False
            draw.status = DrawStatus.ACTIVATED
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

    async def expire_due(self) -> list[int]:
        """Mark published-but-unclaimed draws past the window as expired; alert admins once."""
        deadline = self.now() - self.window
        async with self.sm() as s, s.begin():
            due = list(
                await s.scalars(
                    select(Draw).where(
                        Draw.status == DrawStatus.PUBLISHED, Draw.published_at < deadline
                    )
                )
            )
            for draw in due:
                draw.status = DrawStatus.EXPIRED
        for draw in due:
            await self.draws.alert_admins(
                "admin_expired",
                [[Button("btn_redraw", f"adm:redraw:{draw.id}")]],
                draw_no=draw.id,
                hours=self.window_hours,
            )
        return [d.id for d in due]
