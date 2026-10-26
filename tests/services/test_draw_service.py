from datetime import UTC

import pytest
from sqlalchemy import select

from giveaway_bot.db.models import Draw, DrawEntry, DrawStatus, UserStatus
from giveaway_bot.domain.draw import snapshot_hash, winner_index
from giveaway_bot.i18n import t
from giveaway_bot.ports.beacon import FakeBeacon
from giveaway_bot.ports.membership import FakeMembership, Membership
from giveaway_bot.ports.publisher import FakePublisher
from giveaway_bot.services.draw_service import (
    DrawBlocked,
    DrawService,
    MembershipUnavailable,
    PoolEmpty,
)
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
    assert svc.beacon.round_time(draw.beacon_round) >= clock().replace(tzinfo=UTC).timestamp() + 60
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
    assert draw.skipped == [
        {"index": pos, "code": rows[pos].entry_code, "reason": "not_subscribed"}
    ]
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

    with pytest.raises(MembershipUnavailable):  # the runner retries this automatically
        await svc.resolve(draw.id)
    paused = await reload(sessionmaker, draw.id)
    assert paused.status == DrawStatus.PAUSED and paused.winner_tg_id is None
    # Admins are told the draw paused, but not who the candidate is.
    paused_alerts = [html for to, html, kb in pub.sent if to == ADMIN and kb]
    assert paused_alerts[-1] == t(None, "admin_paused", draw_no=draw.id)
    assert any(
        f"adm:retry:{draw.id}" == b.callback for _, _, kb in pub.sent if kb for r in kb for b in r
    )

    members.members[first] = Membership.YES  # re-subscribing after being skipped doesn't help
    members.members[second] = Membership.YES
    await svc.resolve(draw.id)

    done = await reload(sessionmaker, draw.id)
    assert done.status == DrawStatus.DRAWN
    assert done.winner_tg_id == second
    assert [x["index"] for x in done.skipped] == [pos]


async def test_everyone_unsubscribed_means_no_winner(svc, part, beacon, members, sessionmaker, pub):
    await enter_all(part, [1, 2])
    draw = await svc.close_entries(ADMIN)
    beacon.values[draw.beacon_round] = RAND
    members.members.update({1: Membership.NO, 2: Membership.NO})

    await svc.resolve(draw.id)

    draw = await reload(sessionmaker, draw.id)
    assert draw.status == DrawStatus.NO_WINNER
    assert len(draw.skipped) == 2
    assert await svc.current() is None  # a new draw can be closed
    # A committed draw always ends with a public post, even without a winner.
    assert draw.result_msg_id is not None
    assert all(x["code"] in pub.channel[-1] for x in draw.skipped)


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


async def test_failed_commitment_post_keeps_the_round_while_it_is_still_ahead(
    svc, part, pub, sessionmaker, clock
):
    await enter_all(part, [1, 2])
    pub.fail_next_post = True
    draw = await svc.close_entries(ADMIN)  # doesn't raise: the draw exists, post is pending
    assert draw.status == DrawStatus.COMMITTED and draw.commit_msg_id is None

    clock.advance(seconds=5)
    await svc.ensure_committed(draw.id)

    retried = await reload(sessionmaker, draw.id)
    assert retried.commit_msg_id is not None
    assert retried.beacon_round == draw.beacon_round and retried.void_rounds == []


async def test_commitment_retried_too_late_voids_the_old_round_publicly(
    svc, part, pub, sessionmaker, clock
):
    await enter_all(part, [1, 2])
    pub.fail_next_post = True
    draw = await svc.close_entries(ADMIN)
    old_round = draw.beacon_round

    clock.advance(minutes=10)
    await svc.ensure_committed(draw.id)

    draw = await reload(sessionmaker, draw.id)
    assert draw.beacon_round > old_round and draw.void_rounds == [old_round]
    assert str(old_round) in pub.channel[-1] and str(draw.beacon_round) in pub.channel[-1]


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


@pytest.mark.parametrize("offset", [0, 1, 2])
async def test_committed_round_starts_at_least_the_lead_after_close_at_any_time(
    sessionmaker, members, beacon, part, clock, offset
):
    from datetime import timedelta

    clock.now += timedelta(seconds=offset)  # not aligned to the 3 s beacon period
    svc = DrawService(
        sessionmaker,
        members,
        beacon,
        FakePublisher(),
        part,
        admin_ids={ADMIN},
        bot_username="bot",
        now=clock,
    )
    await enter_all(part, [1, 2])
    draw = await svc.close_entries(ADMIN)
    lead = beacon.round_time(draw.beacon_round) - clock().replace(tzinfo=UTC).timestamp()
    assert lead >= svc.lead_seconds


async def test_past_winner_cannot_enter_again_and_is_left_out_of_the_next_draw(
    svc, part, beacon, sessionmaker, clock
):
    from giveaway_bot.services.claims import Claims
    from giveaway_bot.services.participation import EnterOutcome

    await enter_all(part, [1, 2, 3])
    draw = await svc.close_entries(ADMIN)
    beacon.values[draw.beacon_round] = RAND
    await svc.resolve(draw.id)
    await svc.approve(draw.id, ADMIN)
    winner = (await reload(sessionmaker, draw.id)).winner_tg_id
    claims = Claims(sessionmaker, svc.pub, svc, admin_ids={ADMIN}, now=clock)
    await claims.submit(winner, "a@b.uz")
    await claims.activate(draw.id, ADMIN)

    assert (await part.enter(winner)).outcome is EnterOutcome.ALREADY_WON
    await enter_all(part, [4])
    second = await svc.close_entries(ADMIN)
    rows = await entries(sessionmaker, second.id)
    assert winner not in {r.tg_id for r in rows} and second.n == 3
