import pytest

from giveaway_bot.db.models import DrawStatus, UserStatus
from giveaway_bot.ports.beacon import FakeBeacon
from giveaway_bot.ports.membership import FakeMembership, Membership
from giveaway_bot.ports.publisher import FakePublisher
from giveaway_bot.services.claims import ClaimOutcome, Claims
from giveaway_bot.services.draw_service import DrawService
from giveaway_bot.services.participation import Participation

ADMIN = 900
RAND = "cd" * 32


@pytest.fixture
async def world(sessionmaker, clock):
    members = FakeMembership({uid: Membership.YES for uid in range(1, 5)})
    pub, beacon = FakePublisher(), FakeBeacon()
    part = Participation(sessionmaker, members, bot_admin_ids={ADMIN}, now=clock)

    async def no_sleep(_):
        return None

    draws = DrawService(
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
    claims = Claims(sessionmaker, pub, draws, admin_ids={ADMIN}, window_hours=48, now=clock)
    for uid in range(1, 5):
        await part.register_start(uid, f"U{uid}", None)
        await part.enter(uid)
    draw = await draws.close_entries(ADMIN)
    beacon.values[draw.beacon_round] = RAND
    await draws.resolve(draw.id)
    await draws.approve(draw.id, ADMIN)
    draw = await draws.get(draw.id)
    return draws, claims, part, pub, beacon, draw


async def test_winner_claims_with_valid_contact_and_admins_are_told(world):
    draws, claims, _, pub, _, draw = world
    assert (await claims.submit(draw.winner_tg_id, "not a contact")).outcome is ClaimOutcome.INVALID
    result = await claims.submit(draw.winner_tg_id, "+998 90 123 45 67")

    assert result.outcome is ClaimOutcome.ACCEPTED and result.contact == "+998901234567"
    assert (await draws.get(draw.id)).status == DrawStatus.CLAIMED
    admin_msgs = [(html, kb) for to, html, kb in pub.sent if to == ADMIN]
    assert any("+998901234567" in html for html, _ in admin_msgs)
    assert any(
        b.callback == f"adm:activate:{draw.id}"
        for _, kb in admin_msgs
        if kb
        for row in kb
        for b in row
    )


async def test_non_winner_message_is_not_a_claim(world):
    _, claims, _, _, _, draw = world
    other = next(uid for uid in range(1, 5) if uid != draw.winner_tg_id)
    assert (await claims.submit(other, "a@b.uz")).outcome is ClaimOutcome.NOT_A_WINNER


async def test_activation_notifies_winner_once(world):
    draws, claims, _, pub, _, draw = world
    await claims.submit(draw.winner_tg_id, "a@b.uz")
    assert await claims.activate(draw.id, ADMIN) is True
    assert await claims.activate(draw.id, ADMIN) is False
    assert (await draws.get(draw.id)).status == DrawStatus.ACTIVATED
    assert len(pub.to(draw.winner_tg_id)) == 2  # win DM + activation DM


async def test_late_claim_is_refused_and_expiry_unlocks_redraw(world, clock):
    draws, claims, part, pub, beacon, draw = world
    assert await draws.redraw(draw.id, ADMIN) is None  # too early

    clock.advance(hours=48, seconds=1)
    assert (await claims.submit(draw.winner_tg_id, "a@b.uz")).outcome is ClaimOutcome.TOO_LATE
    assert await claims.expire_due() == [draw.id]
    assert await claims.expire_due() == []  # admins alerted once
    assert (await draws.get(draw.id)).status == DrawStatus.EXPIRED

    child = await draws.redraw(draw.id, ADMIN)

    assert child.parent_id == draw.id and child.n == draw.n - 1
    assert child.snapshot_hash != draw.snapshot_hash
    assert child.beacon_round > draw.beacon_round
    assert draw.winner_tg_id not in (await draws._positions(child.id)).values()
    assert (await draws.get(draw.id)).status == DrawStatus.REDRAWN
    assert (await part.get(draw.winner_tg_id)).status == UserStatus.ACTIVE
    assert f"№{draw.id}" in pub.channel[-1] and str(child.beacon_round) in pub.channel[-1]

    beacon.values[child.beacon_round] = RAND
    await draws.resolve(child.id)
    assert (await draws.get(child.id)).status == DrawStatus.DRAWN


async def test_claim_exactly_at_deadline_is_still_accepted(world, clock):
    _, claims, _, _, _, draw = world
    clock.advance(hours=48)
    assert (await claims.submit(draw.winner_tg_id, "a@b.uz")).outcome is ClaimOutcome.ACCEPTED
