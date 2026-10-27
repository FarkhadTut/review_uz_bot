import asyncio

import pytest

from giveaway_bot.db.models import Draw, DrawStatus
from giveaway_bot.i18n import t
from giveaway_bot.ports.beacon import BeaconError, FakeBeacon
from giveaway_bot.ports.membership import FakeMembership, Membership
from giveaway_bot.ports.publisher import FakePublisher
from giveaway_bot.services.claims import Claims
from giveaway_bot.services.draw_service import DrawService
from giveaway_bot.services.participation import Participation
from giveaway_bot.services.runner import Runner

ADMIN = 900
RAND = "77" * 32


class FlakyBeacon(FakeBeacon):
    def __init__(self, failures: int):
        super().__init__()
        self.failures = failures

    async def randomness(self, round_no):
        if self.failures:
            self.failures -= 1
            raise BeaconError("relays down")
        return await super().randomness(round_no)


class RecordingSleep:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds):
        self.calls.append(seconds)
        await asyncio.sleep(0)


def build(sessionmaker, clock, beacon, members=None, sleep=None):
    members = members or FakeMembership({1: Membership.YES, 2: Membership.YES})
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
    runner = Runner(
        draws,
        claims,
        None,
        sleep=sleep or RecordingSleep(),
        expiry_every=0.01,
        retention_every=None,
    )
    return part, draws, runner, pub


async def committed(part, draws):
    for uid in (1, 2):
        await part.register_start(uid, "U", None)
        await part.enter(uid)
    return await draws.close_entries(ADMIN)


async def eventually(condition) -> bool:
    for _ in range(200):
        if await condition():
            return True
        await asyncio.sleep(0.005)
    return False


async def wait_for(draws, draw_id, status):
    for _ in range(200):
        if (await draws.get(draw_id)).status == status:
            return True
        await asyncio.sleep(0.005)
    return False


async def test_restart_resumes_a_committed_draw_and_expires_claims(sessionmaker, clock):
    beacon = FakeBeacon()
    part, draws, _, _ = build(sessionmaker, clock, beacon)
    draw = await committed(part, draws)  # process "dies" before the round is out

    beacon.values[draw.beacon_round] = RAND
    _, draws2, runner, _ = build(sessionmaker, clock, beacon)  # fresh process
    await runner.start()
    assert await wait_for(draws2, draw.id, DrawStatus.DRAWN)

    await draws2.approve(draw.id, ADMIN)
    clock.advance(hours=49)
    assert await wait_for(draws2, draw.id, DrawStatus.EXPIRED)
    await runner.stop()


async def test_beacon_outage_alerts_admins_once_and_retries_with_backoff(sessionmaker, clock):
    beacon = FlakyBeacon(failures=2)
    sleep = RecordingSleep()
    part, draws, runner, pub = build(sessionmaker, clock, beacon, sleep=sleep)
    draw = await committed(part, draws)
    beacon.values[draw.beacon_round] = RAND

    runner.spawn_resolve(draw.id)
    assert await wait_for(draws, draw.id, DrawStatus.DRAWN)

    assert sleep.calls[:2] == [15, 30]
    down = [h for to, h, _ in pub.sent if to == ADMIN and "drand" in h]
    assert len(down) == 1 and str(draw.beacon_round) in down[0]
    await runner.stop()


async def test_paused_draw_is_retried_automatically(sessionmaker, clock):
    members = FakeMembership({1: Membership.UNKNOWN, 2: Membership.UNKNOWN})
    beacon = FakeBeacon()
    part, draws, runner, _ = build(sessionmaker, clock, beacon, members=members)
    members.members.update({1: Membership.YES, 2: Membership.YES})  # for registration
    draw = await committed(part, draws)
    beacon.values[draw.beacon_round] = RAND
    members.members.update({1: Membership.UNKNOWN, 2: Membership.UNKNOWN})

    runner.spawn_resolve(draw.id)
    assert await wait_for(draws, draw.id, DrawStatus.PAUSED)
    members.members.update({1: Membership.YES, 2: Membership.YES})  # Telegram is back
    assert await wait_for(draws, draw.id, DrawStatus.DRAWN)
    await runner.stop()


async def test_unexpected_error_alerts_admins_and_stops_retrying(sessionmaker, clock):
    beacon = FakeBeacon()
    sleep = RecordingSleep()
    part, draws, runner, pub = build(sessionmaker, clock, beacon, sleep=sleep)
    draw = await committed(part, draws)

    async def broken(_):
        raise KeyError("bug")

    draws.resolve = broken
    runner.spawn_resolve(draw.id)

    async def alerted():
        return any("KeyError" in h for to, h, _ in pub.sent if to == ADMIN)

    assert await eventually(alerted)
    await runner.stop()
    assert sleep.calls == []  # no retry loop


async def test_spawning_twice_runs_one_resolve(sessionmaker, clock):
    beacon = FakeBeacon()
    part, draws, runner, _ = build(sessionmaker, clock, beacon)
    draw = await committed(part, draws)
    calls = []
    gate = asyncio.Event()

    async def slow(draw_id):
        calls.append(draw_id)
        await gate.wait()

    draws.resolve = slow
    runner.spawn_resolve(draw.id)
    runner.spawn_resolve(draw.id)
    await asyncio.sleep(0)
    gate.set()
    await asyncio.sleep(0)
    await runner.stop()
    assert calls == [draw.id]


async def test_interrupted_results_post_and_paused_draws_resume_on_start(sessionmaker, clock):
    beacon = FakeBeacon()
    part, draws, runner, pub = build(sessionmaker, clock, beacon)
    draw = await committed(part, draws)
    beacon.values[draw.beacon_round] = RAND
    await draws.resolve(draw.id)
    async with sessionmaker() as s, s.begin():
        (await s.get(Draw, draw.id)).status = DrawStatus.PUBLISHED  # crashed mid-publish

    await runner.start()
    published = t(None, "admin_published", draw_no=draw.id, dm_status=t(None, "admin_dm_ok"))

    async def announced():
        return published in [h for to, h, _ in pub.sent if to == ADMIN]

    assert await eventually(announced)
    await runner.stop()
    assert (await draws.get(draw.id)).result_msg_id is not None


@pytest.mark.parametrize("bad_id", [999])
async def test_resolving_a_missing_draw_is_a_quiet_no_op(sessionmaker, clock, bad_id):
    beacon = FakeBeacon()
    _, draws, runner, pub = build(sessionmaker, clock, beacon)
    runner.spawn_resolve(bad_id)
    for _ in range(20):
        await asyncio.sleep(0)
    await runner.stop()
    assert pub.sent == []


class Forever:
    """A backoff sleep that never ends on its own: only a wake-up can continue the loop."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, seconds):
        self.calls += 1
        await asyncio.Event().wait()


async def test_admin_retry_wakes_a_draw_sleeping_in_backoff(sessionmaker, clock):
    beacon = FlakyBeacon(failures=1)
    sleep = Forever()
    part, draws, runner, _ = build(sessionmaker, clock, beacon, sleep=sleep)
    draw = await committed(part, draws)
    beacon.values[draw.beacon_round] = RAND

    runner.spawn_resolve(draw.id)
    assert await eventually(lambda: asyncio.sleep(0, result=sleep.calls == 1))
    assert (await draws.get(draw.id)).status == DrawStatus.COMMITTED  # stuck in backoff

    runner.spawn_resolve(draw.id)  # the admin's Retry
    assert await wait_for(draws, draw.id, DrawStatus.DRAWN)
    await runner.stop()


async def test_commitment_pending_is_alerted_once_per_outage(sessionmaker, clock):
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import SendMessage

    beacon = FakeBeacon()
    sleep = RecordingSleep()
    part, draws, runner, pub = build(sessionmaker, clock, beacon, sleep=sleep)
    failures = {"left": 3}
    real_post = pub.post_channel

    async def flaky_post(html, keyboard=None):
        if failures["left"]:
            failures["left"] -= 1
            raise TelegramNetworkError(SendMessage(chat_id=1, text="x"), "timeout")
        return await real_post(html, keyboard)

    pub.post_channel = flaky_post
    draw = await committed(part, draws)  # first post fails
    beacon.values[draw.beacon_round] = RAND
    runner.spawn_resolve(draw.id)
    assert await wait_for(draws, draw.id, DrawStatus.DRAWN)
    await runner.stop()

    pending = t(None, "admin_commit_pending", draw_no=draw.id)
    assert [h for to, h, _ in pub.sent if to == ADMIN].count(pending) == 1


async def test_permanent_telegram_error_stops_and_alerts_instead_of_looping(sessionmaker, clock):
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage

    beacon = FakeBeacon()
    sleep = RecordingSleep()
    part, draws, runner, pub = build(sessionmaker, clock, beacon, sleep=sleep)

    async def forbidden(html, keyboard=None):
        raise TelegramForbiddenError(SendMessage(chat_id=1, text="x"), "not enough rights")

    pub.post_channel = forbidden
    draw = await committed(part, draws)
    runner.spawn_resolve(draw.id)

    async def alerted():
        return any("TelegramForbiddenError" in h for to, h, _ in pub.sent if to == ADMIN)

    assert await eventually(alerted)
    await runner.stop()
    assert sleep.calls == []


async def test_housekeeping_respawns_an_orphaned_committed_draw(sessionmaker, clock):
    beacon = FakeBeacon()
    part, draws, runner, _ = build(sessionmaker, clock, beacon)
    draw = await committed(part, draws)
    beacon.values[draw.beacon_round] = RAND
    # No spawn_resolve at all (e.g. its task died): housekeeping must pick it up.
    await runner._housekeeping()
    assert await wait_for(draws, draw.id, DrawStatus.DRAWN)
    await runner.stop()


async def test_weekly_notice_runs_in_the_background_without_blocking_the_draw(sessionmaker, clock):
    from giveaway_bot.services.notices import Notices

    beacon = FakeBeacon()
    part, draws, runner, pub = build(sessionmaker, clock, beacon)
    gate = asyncio.Event()

    async def slow_sleep(_):
        await gate.wait()  # delivery stalls (e.g. flood control) until released

    runner.notices = Notices(sessionmaker, pub, sleep=slow_sleep)
    draw = await committed(part, draws)
    beacon.values[draw.beacon_round] = RAND

    runner.spawn_resolve(draw.id)
    assert await wait_for(draws, draw.id, DrawStatus.DRAWN)  # not held up by the notices
    gate.set()
    assert await eventually(lambda: _done(runner.notices))
    noticed = {to for to, _, kb in pub.sent if kb and kb[-1][0].callback == "notify:off"}
    assert noticed == {1, 2}
    await runner._housekeeping()  # finished: nothing is sent again
    await asyncio.sleep(0.01)
    assert len([1 for _, _, kb in pub.sent if kb and kb[-1][0].callback == "notify:off"]) == 2
    await runner.stop()


async def _done(notices):
    return await notices.pending() == []
