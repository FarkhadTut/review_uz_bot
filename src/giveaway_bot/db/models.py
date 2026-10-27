from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import JSON, BigInteger, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class UserStatus(StrEnum):
    ACTIVE = "active"
    WON = "won"


class DrawStatus(StrEnum):
    COMMITTED = "committed"  # snapshot + beacon round published, randomness not yet known
    PAUSED = "paused"  # membership check returned UNKNOWN; retried automatically
    DRAWN = "drawn"  # winner selected, waiting for admin approval of the results post
    # Results being posted (result_msg_id NULL) or posted, claim window running from
    # published_at. PUBLISHED with no result_msg_id after a crash is finished on restart.
    PUBLISHED = "published"
    CLAIMED = "claimed"  # winner sent contact
    ACTIVATED = "activated"  # staff activated the subscription
    EXPIRED = "expired"  # claim window passed, redraw available
    REDRAWN = "redrawn"  # superseded by a child draw
    NO_WINNER = "no_winner"  # every candidate in the snapshot was ineligible


# Draws in these states block closing a new one.
UNRESOLVED = (
    DrawStatus.COMMITTED,
    DrawStatus.PAUSED,
    DrawStatus.DRAWN,
    DrawStatus.PUBLISHED,
    # CLAIMED is left out: once the winner's contact is in, staff finish the prize by phone
    # (registration, login, activation can take days) and next week's draw may start.
    DrawStatus.EXPIRED,
)


class User(Base):
    __tablename__ = "users"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    first_name: Mapped[str] = mapped_column(String(128), default="")
    username: Mapped[str | None] = mapped_column(String(64))
    lang: Mapped[str | None] = mapped_column(String(8))
    source: Mapped[str | None] = mapped_column(String(64))
    first_start_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Channel membership at first /start: the basis of the "new-to-channel entrant" metric.
    member_at_first_start: Mapped[bool | None]
    entry_code: Mapped[str | None] = mapped_column(String(16), unique=True)
    entered_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(16), default=UserStatus.ACTIVE)
    # Weekly "you're in this draw" notices; winner messages ignore it.
    notify: Mapped[bool] = mapped_column(default=True, server_default="1")


class Staff(Base):
    __tablename__ = "staff"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    added_by: Mapped[int] = mapped_column(BigInteger)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)


class Draw(Base):
    __tablename__ = "draws"

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("draws.id"))
    status: Mapped[str] = mapped_column(String(16))
    closed_at: Mapped[datetime] = mapped_column(default=utcnow)
    closed_by: Mapped[int] = mapped_column(BigInteger)
    n: Mapped[int]
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    beacon_round: Mapped[int]
    # Rounds announced earlier for this draw and replaced because their commitment post
    # failed or landed too late; listed publicly in the replacing post.
    void_rounds: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    randomness: Mapped[str | None] = mapped_column(String(128))
    winner_index: Mapped[int | None]
    # [{"index": int, "code": str, "reason": "not_subscribed" | "banned"}],
    # in the order they were tried
    skipped: Mapped[list] = mapped_column(JSON, default=list)
    winner_tg_id: Mapped[int | None] = mapped_column(BigInteger)
    winner_position: Mapped[int | None]
    commit_msg_id: Mapped[int | None]
    result_msg_id: Mapped[int | None]
    published_at: Mapped[datetime | None]
    # EXPIRED draws whose admin alert hasn't gone out yet are re-alerted by the runner.
    expiry_notified: Mapped[bool] = mapped_column(default=False)
    # Winner notices, each claimed before sending so it goes out at most once.
    reminder_sent: Mapped[bool] = mapped_column(default=False, server_default="0")
    missed_notified: Mapped[bool] = mapped_column(default=False, server_default="0")
    # Weekly notice to the snapshot finished (or not applicable: redraws).
    notice_done: Mapped[bool] = mapped_column(default=False, server_default="0")


class DrawEntry(Base):
    __tablename__ = "draw_entries"
    __table_args__ = (UniqueConstraint("draw_id", "entry_code"),)

    draw_id: Mapped[int] = mapped_column(ForeignKey("draws.id"), primary_key=True)
    position: Mapped[int] = mapped_column(primary_key=True)
    entry_code: Mapped[str] = mapped_column(String(16))
    tg_id: Mapped[int] = mapped_column(BigInteger)
    # Weekly notice handled for this entry (sent, muted or unreachable).
    notified: Mapped[bool] = mapped_column(default=False, server_default="0")


class Claim(Base):
    __tablename__ = "claims"

    draw_id: Mapped[int] = mapped_column(ForeignKey("draws.id"), primary_key=True)
    contact: Mapped[str] = mapped_column(String(128))
    submitted_at: Mapped[datetime] = mapped_column(default=utcnow)
    activated_at: Mapped[datetime | None]
    activated_by: Mapped[int | None] = mapped_column(BigInteger)


class MembershipCheck(Base):
    __tablename__ = "membership_checks"
    __table_args__ = (UniqueConstraint("tg_id", "kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16))
    subscribed: Mapped[bool]
    checked_at: Mapped[datetime] = mapped_column(default=utcnow)


class Setting(Base):
    """Admin-editable values, e.g. the announcement text (HTML)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_by: Mapped[int] = mapped_column(BigInteger)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(default=utcnow)
