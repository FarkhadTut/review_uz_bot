from datetime import UTC

import pytest
from sqlalchemy import select

from giveaway_bot.db.models import Draw, DrawEntry, DrawStatus, UserStatus
from giveaway_bot.domain.draw import snapshot_hash, winner_index
from giveaway_bot.ports.beacon import FakeBeacon
from giveaway_bot.ports.membership import FakeMembership, Membership
from giveaway_bot.ports.publisher import FakePublisher
from giveaway_bot.services.draw_service import DrawBlocked, DrawService, PoolEmpty
from giveaway_bot.services.participation import Participation

ADMIN = 900
RAND = "ab" * 32


class FailingOnce(FakePublisher):
    def __init__(self):
        super().__init__()
        self.fail_next_post = False

    async def post_channel(self, html):
        if self.fail_next_post:
            self.fail_next_post = False
            raise RuntimeError("telegram down")
        return await super().post_channel(html)


@pytest.fixture
def members():
    return FakeMembership({uid: Membership.YES for uid in range(1, 7)})


@pytest.fixture
def pub():
    return FailingOnce()


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
        bot_username="review_giveaway_bot",
        now=clock,
        sleep=no_sleep,
    )


async def enter_all(part, ids):
    codes = {}
    for uid in ids:
        await part.register_start(uid, f"User{uid}", f"user{uid}")
        codes[uid] = (await part.enter(uid)).code
    return codes


async def entries(sessionmaker, draw_id):
    async with sessionmaker() as s:
        rows = await s.scalars(
            select(DrawEntry).where(DrawEntry.draw_id == draw_id).order_by(DrawEntry.position)
        )
        return list(rows)


async def reload(sessionmaker, draw_id) -> Draw:
    async with sessionmaker() as s:
        return await s.get(Draw, draw_id)


def expected_position(draw: Draw, randomness: str = RAND) -> int:
    return winner_index(randomness, draw.snapshot_hash, draw.n)


async def test_close_commits_snapshot_of_eligible_entrants_and_posts_it(
    svc, part, sessionmaker, pub, clock
):
    codes = await enter_all(part, [1, 2, 3, 4])
    await part.register_start(5, "NoEntry", None)  # started, never entered
    await part.add_staff(4, added_by=ADMIN)  # became staff after entering

    draw = await svc.close_entries(ADMIN)

    rows = await entries(sessionmaker, draw.id)
    assert [r.entry_code for r in rows] == sorted(codes[u] for u in (1, 2, 3))
    assert draw.n == 3 and draw.status == DrawStatus.COMMITTED
    assert draw.snapshot_hash == snapshot_hash([codes[u] for u in (1, 2, 3)])
    assert draw.beacon_round > svc.beacon.round_at(clock().replace(tzinfo=UTC).timestamp())
    assert draw.commit_msg_id == 1
    assert str(draw.n) in pub.channel[0] and draw.snapshot_hash in pub.channel[0]
    assert str(draw.beacon_round) in pub.channel[0]


async def test_cannot_close_with_unresolved_draw_or_empty_pool(svc, part):
    with pytest.raises(PoolEmpty):
        await svc.close_entries(ADMIN)
    await enter_all(part, [1])
    await svc.close_entries(ADMIN)
    with pytest.raises(DrawBlocked):
        await svc.close_entries(ADMIN)


async def test_resolve_selects_by_published_formula_and_asks_admin_to_approve(
    svc, part, beacon, sessionmaker, pub
):
    await enter_all(part, [1, 2, 3, 4, 5])
    draw = await svc.close_entries(ADMIN)
    beacon.values[draw.beacon_round] = RAND

    await svc.resolve(draw.id)

    draw = await reload(sessionmaker, draw.id)
    rows = await entries(sessionmaker, draw.id)
    pos = expected_position(draw)
    assert draw.status == DrawStatus.DRAWN
    assert (draw.winner_index, draw.winner_position, draw.skipped) == (pos, pos, [])
    assert draw.winner_tg_id == rows[pos].tg_id
    admin_msgs = [kb for to, _, kb in pub.sent if to == ADMIN]
    assert any(
        b.callback == f"adm:approve:{draw.id}" for kb in admin_msgs for row in kb for b in row
    )


async def test_unsubscribed_candidate_is_skipped_to_the_next_position(
    svc, part, beacon, members, sessionmaker
):
    await enter_all(part, [1, 2, 3, 4, 5])
    draw = await svc.close_entries(ADMIN)
    beacon.values[draw.beacon_round] = RAND
    rows = await entries(sessionmaker, draw.id)
    pos = expected_position(draw)
    members.members[rows[pos].tg_id] = Membership.NO

    await svc.resolve(draw.id)

    draw = await reload(sessionmaker, draw.id)
    assert draw.skipped == [{"index": pos, "reason": "not_subscribed"}]
    assert draw.winner_position == (pos + 1) % 5
    assert draw.winner_tg_id == rows[(pos + 1) % 5].tg_id


async def test_api_failure_pauses_and_retry_continues_without_rechecking_skips(
    svc, part, beacon, members, sessionmaker, pub
):
    await enter_all(part, [1, 2, 3, 4, 5])
    draw = await svc.close_entries(ADMIN)
    beacon.values[draw.beacon_round] = RAND
    rows = await entries(sessionmaker, draw.id)
    pos = expected_position(draw)
    first, second = rows[pos].tg_id, rows[(pos + 1) % 5].tg_id
    members.members[first] = Membership.NO
    members.members[second] = Membership.UNKNOWN

    await svc.resolve(draw.id)
    paused = await reload(sessionmaker, draw.id)
    assert paused.status == DrawStatus.PAUSED and paused.winner_tg_id is None
    assert any(
        f"adm:retry:{draw.id}" == b.callback for _, _, kb in pub.sent if kb for r in kb for b in r
    )

    members.members[first] = Membership.YES  # re-subscribing after being skipped doesn't help
    members.members[second] = Membership.YES
    await svc.resolve(draw.id)

    done = await reload(sessionmaker, draw.id)
    assert done.status == DrawStatus.DRAWN
    assert done.winner_tg_id == second
    assert done.skipped == [{"index": pos, "reason": "not_subscribed"}]


async def test_everyone_unsubscribed_means_no_winner(svc, part, beacon, members, sessionmaker):
    await enter_all(part, [1, 2])
    draw = await svc.close_entries(ADMIN)
    beacon.values[draw.beacon_round] = RAND
    members.members.update({1: Membership.NO, 2: Membership.NO})

    await svc.resolve(draw.id)

    draw = await reload(sessionmaker, draw.id)
    assert draw.status == DrawStatus.NO_WINNER
    assert len(draw.skipped) == 2
    assert await svc.current() is None  # a new draw can be closed


async def test_approve_posts_proof_dms_winner_and_excludes_them_from_next_draw(
    svc, part, beacon, sessionmaker, pub, clock
):
    await enter_all(part, [1, 2, 3])
    draw = await svc.close_entries(ADMIN)
    beacon.values[draw.beacon_round] = RAND
    await svc.resolve(draw.id)
    draw = await reload(sessionmaker, draw.id)

    await svc.approve(draw.id, ADMIN)
    await svc.approve(draw.id, ADMIN)  # double press is a no-op

    published = await reload(sessionmaker, draw.id)
    assert published.status == DrawStatus.PUBLISHED and published.published_at == clock()
    assert len(pub.channel) == 2
    results = pub.channel[1]
    for value in (draw.snapshot_hash, RAND, str(draw.beacon_round), "User", "@us***"):
        assert value in results
    assert len(pub.to(draw.winner_tg_id)) == 1
    assert (await part.get(draw.winner_tg_id)).status == UserStatus.WON


async def test_commitment_post_failure_picks_a_fresh_round_on_resume(
    svc, part, pub, sessionmaker, clock
):
    await enter_all(part, [1, 2])
    pub.fail_next_post = True
    with pytest.raises(RuntimeError):
        await svc.close_entries(ADMIN)
    draw = await svc.current()
    assert draw.status == DrawStatus.COMMITTED and draw.commit_msg_id is None
    old_round = draw.beacon_round

    clock.advance(minutes=10)
    await svc.ensure_committed(draw.id)

    draw = await reload(sessionmaker, draw.id)
    assert draw.commit_msg_id is not None
    assert draw.beacon_round > old_round
    assert str(draw.beacon_round) in pub.channel[-1]


async def test_failed_results_post_rolls_back_so_admin_can_retry(svc, part, beacon, pub):
    await enter_all(part, [1, 2])
    draw = await svc.close_entries(ADMIN)
    beacon.values[draw.beacon_round] = RAND
    await svc.resolve(draw.id)

    pub.fail_next_post = True
    with pytest.raises(RuntimeError):
        await svc.approve(draw.id, ADMIN)

    failed = await svc.get(draw.id)
    assert failed.status == DrawStatus.DRAWN and failed.published_at is None
    assert (await part.get(failed.winner_tg_id)).status == UserStatus.ACTIVE
    assert await svc.approve(draw.id, ADMIN) is True
