import asyncio

from giveaway_bot.db.models import DrawStatus
from giveaway_bot.ports.beacon import FakeBeacon
from giveaway_bot.ports.membership import FakeMembership, Membership
from giveaway_bot.ports.publisher import FakePublisher
from giveaway_bot.services.claims import Claims
from giveaway_bot.services.draw_service import DrawService
from giveaway_bot.services.participation import Participation
from giveaway_bot.services.runner import Runner
from giveaway_bot.services.stats import Stats

ADMIN = 900


def build(sessionmaker, clock, beacon):
    members = FakeMembership({1: Membership.YES, 2: Membership.YES})
    pub = FakePublisher()
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
    claims = Claims(sessionmaker, pub, draws, admin_ids={ADMIN}, now=clock)
    stats = Stats(sessionmaker, members, part, admin_ids={ADMIN}, now=clock)
    return part, draws, Runner(draws, claims, stats, expiry_every=0.01, retention_every=0.01)


async def test_restart_resumes_a_committed_draw_and_expires_claims(sessionmaker, clock):
    beacon = FakeBeacon()
    part, draws, _ = build(sessionmaker, clock, beacon)
    for uid in (1, 2):
        await part.register_start(uid, "U", None)
        await part.enter(uid)
    draw = await draws.close_entries(ADMIN)  # process "dies" before the round is out

    beacon.values[draw.beacon_round] = "77" * 32
    _, draws2, runner = build(sessionmaker, clock, beacon)  # fresh process
    await runner.start()
    for _ in range(100):
        if (await draws2.get(draw.id)).status == DrawStatus.DRAWN:
            break
        await asyncio.sleep(0.01)
    assert (await draws2.get(draw.id)).status == DrawStatus.DRAWN

    await draws2.approve(draw.id, ADMIN)
    clock.advance(hours=49)
    for _ in range(100):
        if (await draws2.get(draw.id)).status == DrawStatus.EXPIRED:
            break
        await asyncio.sleep(0.01)
    await runner.stop()
    assert (await draws2.get(draw.id)).status == DrawStatus.EXPIRED
