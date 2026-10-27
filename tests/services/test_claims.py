import pytest

from giveaway_bot.db.models import DrawStatus, UserStatus
from giveaway_bot.ports.beacon import FakeBeacon
from giveaway_bot.ports.membership import FakeMembership, Membership
from giveaway_bot.ports.publisher import FakePublisher
from giveaway_bot.services.claims import ClaimOutcome, Claims
from giveaway_bot.services.draw_service import DrawService, RedrawOutcome
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
    assert (await draws.redraw(draw.id, ADMIN)).outcome is RedrawOutcome.NOT_ALLOWED  # too early

    clock.advance(hours=48, seconds=1)
    assert (await claims.submit(draw.winner_tg_id, "a@b.uz")).outcome is ClaimOutcome.TOO_LATE
    assert await claims.expire_due() == [draw.id]
    assert await claims.expire_due() == []  # admins alerted once
    assert (await draws.get(draw.id)).status == DrawStatus.EXPIRED

    child = (await draws.redraw(draw.id, ADMIN)).draw

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


async def test_double_submit_and_double_activate_happen_once(world):
    import asyncio

    draws, claims, _, pub, _, draw = world
    results = await asyncio.gather(
        claims.submit(draw.winner_tg_id, "a@b.uz"), claims.submit(draw.winner_tg_id, "c@d.uz")
    )
    assert sorted(r.outcome for r in results) == sorted(
        [ClaimOutcome.ACCEPTED, ClaimOutcome.ALREADY_RECEIVED]
    )
    accepted = next(r.contact for r in results if r.outcome is ClaimOutcome.ACCEPTED)
    claim_alerts = [
        h for to, h, kb in pub.sent if to == ADMIN and kb and "adm:activate" in kb[0][0].callback
    ]
    assert len(claim_alerts) == 1 and accepted in claim_alerts[0]
    activations = await asyncio.gather(
        claims.activate(draw.id, ADMIN), claims.activate(draw.id, ADMIN)
    )
    assert sorted(activations) == [False, True]
    assert len(pub.to(draw.winner_tg_id)) == 2  # win DM + one activation DM


async def test_expiry_alert_lost_in_a_crash_is_sent_on_the_next_pass(world, clock, sessionmaker):
    from giveaway_bot.db.models import Draw

    draws, claims, _, pub, _, draw = world
    clock.advance(hours=49)
    # Crash between the EXPIRED transition and the admin alert.
    async with sessionmaker() as s, s.begin():
        (await s.get(Draw, draw.id)).status = DrawStatus.EXPIRED

    assert await claims.expire_due() == [draw.id]
    assert (await draws.get(draw.id)).expiry_notified is True
    assert await claims.expire_due() == []


async def test_pending_claim_is_offered_again_until_the_deadline(world, clock):
    _, claims, _, _, _, draw = world
    assert (await claims.pending_claim(draw.winner_tg_id)).id == draw.id
    assert await claims.pending_claim(draw.winner_tg_id + 1000) is None
    clock.advance(hours=49)
    assert await claims.pending_claim(draw.winner_tg_id) is None


def winner_msgs(pub, draw, key_fragment):
    return [h for to, h, _ in pub.sent if to == draw.winner_tg_id and key_fragment in h]


async def test_unclaimed_winner_is_reminded_once_12_hours_before_the_deadline(world, clock):
    from giveaway_bot.i18n import t

    _, claims, _, pub, _, draw = world
    reminder = t(None, "claim_reminder", hours=12)
    clock.advance(hours=35, minutes=59)
    assert await claims.remind_due() == []
    clock.advance(minutes=1)
    assert await claims.remind_due() == [draw.id]
    assert await claims.remind_due() == []
    assert [h for to, h, _ in pub.sent if to == draw.winner_tg_id].count(reminder) == 1


async def test_winner_who_already_claimed_is_not_reminded(world, clock):
    _, claims, _, pub, _, draw = world
    await claims.submit(draw.winner_tg_id, "a@b.uz")
    clock.advance(hours=40)
    assert await claims.remind_due() == []


async def test_reminder_never_goes_out_after_the_deadline(world, clock):
    _, claims, _, _, _, draw = world
    clock.advance(hours=49)  # bot was down through the reminder time
    assert await claims.remind_due() == []


async def test_winner_is_told_once_when_the_prize_is_missed_even_if_muted(world, clock):
    from giveaway_bot.i18n import t

    _, claims, part, pub, _, draw = world
    await part.set_notify(draw.winner_tg_id, False)
    missed = t(None, "prize_missed", hours=48)
    clock.advance(hours=49)
    await claims.expire_due()
    await claims.expire_due()
    assert [h for to, h, _ in pub.sent if to == draw.winner_tg_id].count(missed) == 1


async def test_missed_notice_still_goes_out_if_an_admin_redrew_first(world, clock, sessionmaker):
    from sqlalchemy import update

    from giveaway_bot.db.models import Draw
    from giveaway_bot.i18n import t

    draws, claims, _, pub, _, draw = world
    clock.advance(hours=49)
    async with sessionmaker() as s, s.begin():  # expired + redrawn before the notice went out
        await s.execute(update(Draw).where(Draw.id == draw.id).values(status="expired"))
    await draws.redraw(draw.id, ADMIN)
    await claims.expire_due()
    assert t(None, "prize_missed", hours=48) in [
        h for to, h, _ in pub.sent if to == draw.winner_tg_id
    ]


async def test_reminder_is_claimed_so_overlapping_passes_send_it_once(world, clock):
    from giveaway_bot.i18n import t

    _, claims, _, pub, _, draw = world
    clock.advance(hours=36)
    real = claims._reminder_candidates

    async def seen_twice(now):  # two passes that both selected the draw before either sent
        return (await real(now)) * 2

    claims._reminder_candidates = seen_twice
    assert await claims.remind_due() == [draw.id]
    sent = [h for to, h, _ in pub.sent if to == draw.winner_tg_id]
    assert sent.count(t(None, "claim_reminder", hours=12)) == 1
