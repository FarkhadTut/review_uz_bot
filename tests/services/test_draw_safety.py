"""Double presses, crash windows and failure paths found in review 85f55cd."""

import asyncio
from datetime import UTC

import pytest
from sqlalchemy import func, select

from giveaway_bot.db.models import Draw, DrawStatus, User, UserStatus
from giveaway_bot.domain.draw import winner_index
from giveaway_bot.i18n import CHANNEL_LANG, t
from giveaway_bot.ports.beacon import FakeBeacon
from giveaway_bot.ports.membership import FakeMembership, Membership
from giveaway_bot.ports.publisher import FakePublisher
from giveaway_bot.services.claims import Claims
from giveaway_bot.services.draw_service import (
    CommitmentError,
    DrawBlocked,
    DrawService,
    RedrawOutcome,
)
from giveaway_bot.services.participation import Participation

ADMIN = 900
RAND = "5a" * 32


class SlowPublisher(FakePublisher):
    """Yields to the event loop on every send, like a real network call."""

    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.post_delay = None
        self.fail_posts = 0

    async def post_channel(self, html):
        await asyncio.sleep(0)
        if self.fail_posts:
            self.fail_posts -= 1
            raise RuntimeError("telegram down")
        if self.post_delay:
            self.clock.advance(**self.post_delay)
            self.post_delay = None
        return await super().post_channel(html)

    async def send(self, tg_id, html, keyboard=None):
        await asyncio.sleep(0)
        return await super().send(tg_id, html, keyboard)


@pytest.fixture
def members():
    return FakeMembership({uid: Membership.YES for uid in range(1, 7)})


@pytest.fixture
def pub(clock):
    return SlowPublisher(clock)


@pytest.fixture
def beacon():
    return FakeBeacon()


@pytest.fixture
def part(sessionmaker, members, clock):
    return Participation(sessionmaker, members, bot_admin_ids={ADMIN}, now=clock)


@pytest.fixture
def svc(sessionmaker, members, beacon, pub, part, clock):
    async def no_sleep(_):
        return None

    return DrawService(
        sessionmaker,
        members,
        beacon,
        pub,
        part,
        admin_ids={ADMIN},
        bot_username="bot",
        now=clock,
        sleep=no_sleep,
    )


@pytest.fixture
def claims(sessionmaker, pub, svc, clock):
    return Claims(sessionmaker, pub, svc, admin_ids={ADMIN}, now=clock)


async def enter(part, ids):
    for uid in ids:
        await part.register_start(uid, f"U{uid}", None)
        await part.enter(uid)


async def drawn(svc, part, beacon, ids=(1, 2, 3)):
    await enter(part, ids)
    draw = await svc.close_entries(ADMIN)
    beacon.values[draw.beacon_round] = RAND
    await svc.resolve(draw.id)
    return await svc.get(draw.id)


async def expired(svc, part, beacon, claims, clock, ids=(1, 2, 3)):
    draw = await drawn(svc, part, beacon, ids)
    await svc.approve(draw.id, ADMIN)
    clock.advance(hours=49)
    await claims.expire_due()
    return await svc.get(draw.id)


async def test_double_approve_posts_once(svc, part, beacon, pub):
    draw = await drawn(svc, part, beacon)
    results = await asyncio.gather(svc.approve(draw.id, ADMIN), svc.approve(draw.id, ADMIN))
    assert sorted(results, key=str) == [None, True]
    assert len(pub.channel) == 2  # commitment + one results post
    assert len(pub.to(draw.winner_tg_id)) == 1


async def test_double_close_creates_one_draw(svc, part):
    await enter(part, (1, 2))
    results = await asyncio.gather(
        svc.close_entries(ADMIN), svc.close_entries(ADMIN), return_exceptions=True
    )
    assert sum(isinstance(r, DrawBlocked) for r in results) == 1
    assert sum(isinstance(r, Draw) for r in results) == 1


async def test_double_redraw_creates_one_child(svc, part, beacon, claims, clock):
    draw = await expired(svc, part, beacon, claims, clock)
    results = await asyncio.gather(svc.redraw(draw.id, ADMIN), svc.redraw(draw.id, ADMIN))
    assert sorted(r.outcome for r in results) == sorted(
        [RedrawOutcome.CREATED, RedrawOutcome.NOT_ALLOWED]
    )
    async with svc.sm() as s:
        children = await s.scalar(
            select(func.count()).select_from(Draw).where(Draw.parent_id == draw.id)
        )
    assert children == 1


async def test_redraw_with_failed_commitment_still_leaves_a_resolvable_child(
    svc, part, beacon, claims, clock, pub
):
    draw = await expired(svc, part, beacon, claims, clock)
    pub.fail_posts = 1
    result = await svc.redraw(draw.id, ADMIN)

    child = result.draw
    assert result.outcome is RedrawOutcome.CREATED
    assert (await svc.get(draw.id)).status == DrawStatus.REDRAWN
    assert child.status == DrawStatus.COMMITTED and child.commit_msg_id is None
    with pytest.raises(DrawBlocked):  # the child counts as the open draw
        await svc.close_entries(ADMIN)

    beacon.values[child.beacon_round] = RAND
    await svc.resolve(child.id)  # resolve publishes the missing commitment first
    assert (await svc.get(child.id)).status == DrawStatus.DRAWN


async def test_redraw_with_nobody_left_is_announced(svc, part, beacon, claims, clock, pub):
    draw = await expired(svc, part, beacon, claims, clock, ids=(1,))
    posts_before = len(pub.channel)
    result = await svc.redraw(draw.id, ADMIN)
    assert result.outcome is RedrawOutcome.NO_CANDIDATES
    assert (await svc.get(draw.id)).status == DrawStatus.NO_WINNER
    assert len(pub.channel) == posts_before + 1
    assert pub.channel[-1] == t(CHANNEL_LANG, "channel_redraw_empty", parent_no=draw.id)
    assert (await part.get(draw.winner_tg_id)).status == UserStatus.ACTIVE


async def test_redraw_before_expiry_is_not_allowed(svc, part, beacon):
    draw = await drawn(svc, part, beacon)
    await svc.approve(draw.id, ADMIN)
    assert (await svc.redraw(draw.id, ADMIN)).outcome is RedrawOutcome.NOT_ALLOWED


async def test_commitment_that_lands_after_its_round_is_voided_and_recommitted(
    svc, part, pub, clock
):
    await enter(part, (1, 2))
    pub.post_delay = {"minutes": 2}  # Telegram took longer than the lead time
    draw = await svc.close_entries(ADMIN)

    assert len(draw.void_rounds) == 1
    assert svc.beacon.round_time(draw.beacon_round) > clock().replace(tzinfo=UTC).timestamp()
    assert str(draw.void_rounds[0]) in pub.channel[-1]


async def test_banned_candidate_is_skipped_with_a_distinct_public_reason(
    svc, part, beacon, members, pub
):
    await enter(part, (1, 2, 3))
    draw = await svc.close_entries(ADMIN)
    beacon.values[draw.beacon_round] = RAND
    codes = await svc.entry_codes(draw.id)
    pos = winner_index(RAND, draw.snapshot_hash, draw.n)
    members.members[(await svc._positions(draw.id))[pos]] = Membership.BANNED

    await svc.resolve(draw.id)
    await svc.approve(draw.id, ADMIN)

    d = await svc.get(draw.id)
    assert d.skipped == [{"index": pos, "code": codes[pos], "reason": "banned"}]
    assert codes[pos] in pub.channel[-1]


async def test_results_post_interrupted_by_a_crash_is_finished_on_restart(
    svc, part, beacon, pub, sessionmaker, clock
):
    draw = await drawn(svc, part, beacon)
    # Crash after the PUBLISHED transition, before the post was recorded.
    async with sessionmaker() as s, s.begin():
        d = await s.get(Draw, draw.id)
        d.status, d.published_at = DrawStatus.PUBLISHED, None
        (await s.get(User, d.winner_tg_id)).status = UserStatus.WON

    assert await svc.pending_publications() == [draw.id]
    clock.advance(minutes=5)
    await svc.finish_publishing(draw.id)

    d = await svc.get(draw.id)
    assert d.result_msg_id is not None and d.published_at == clock()
    assert len(pub.to(d.winner_tg_id)) == 1
    assert await svc.pending_publications() == []


async def test_failed_redraw_empty_post_is_reported_to_admins(
    svc, part, beacon, claims, clock, pub
):
    draw = await expired(svc, part, beacon, claims, clock, ids=(1,))
    pub.fail_posts = 1
    await svc.redraw(draw.id, ADMIN)
    assert any("telegram down" in h for to, h, _ in pub.sent if to == ADMIN)


async def test_commitment_that_keeps_landing_late_gives_up_without_a_commitment(
    svc, part, pub, clock
):
    await enter(part, (1, 2))
    pub.fail_posts = 1
    draw = await svc.close_entries(ADMIN)  # first post fails, draw stays pending

    async def always_late(html):
        clock.advance(minutes=5)
        return 1

    pub.post_channel = always_late
    with pytest.raises(CommitmentError):
        await svc.ensure_committed(draw.id)
    d = await svc.get(draw.id)
    assert d.commit_msg_id is None and len(d.void_rounds) == 3


async def test_a_round_is_never_replaced_once_its_randomness_was_fetched(
    svc, part, pub, clock, sessionmaker
):
    await enter(part, (1, 2))
    pub.fail_posts = 1
    draw = await svc.close_entries(ADMIN)
    async with sessionmaker() as s, s.begin():
        (await s.get(Draw, draw.id)).randomness = RAND  # e.g. state edited behind our back
    clock.advance(minutes=10)

    with pytest.raises(CommitmentError):
        await svc.ensure_committed(draw.id)
    d = await svc.get(draw.id)
    assert d.beacon_round == draw.beacon_round and d.void_rounds == []


async def test_long_skip_and_void_lists_keep_the_post_within_telegram_limits(
    svc, part, beacon, members, pub, sessionmaker
):
    ids = list(range(1, 81))
    for uid in ids:
        members.members[uid] = Membership.YES
    await enter(part, ids)
    draw = await svc.close_entries(ADMIN)
    beacon.values[draw.beacon_round] = RAND
    for uid in ids:
        members.members[uid] = Membership.NO
    async with sessionmaker() as s, s.begin():
        (await s.get(Draw, draw.id)).void_rounds = list(range(1000, 1200))
    await svc.resolve(draw.id)  # everyone skipped -> public no-winner post

    assert len(pub.channel[-1]) < 4096
    assert "/verify" in pub.channel[-1]
