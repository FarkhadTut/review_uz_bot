import asyncio

import pytest
from sqlalchemy import update

from giveaway_bot.db.models import Draw, DrawEntry
from giveaway_bot.ports.beacon import FakeBeacon
from giveaway_bot.ports.membership import FakeMembership, Membership
from giveaway_bot.ports.publisher import FakePublisher
from giveaway_bot.services.draw_service import DrawService
from giveaway_bot.services.notices import Notices
from giveaway_bot.services.participation import Participation

ADMIN = 900
USERS = (1, 2, 3, 4)


@pytest.fixture
async def world(sessionmaker, clock):
    members = FakeMembership({uid: Membership.YES for uid in USERS})
    pub = FakePublisher()
    part = Participation(sessionmaker, members, bot_admin_ids={ADMIN}, now=clock)
    draws = DrawService(
        sessionmaker,
        members,
        FakeBeacon(),
        pub,
        part,
        admin_ids={ADMIN},
        bot_username="bot",
        now=clock,
    )
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    notices = Notices(sessionmaker, pub, per_second=10, batch=2, sleep=sleep)
    for uid in USERS:
        await part.register_start(uid, f"U{uid}", None)
        await part.set_lang(uid, "uz_cyrl")
        await part.enter(uid)
    draw = await draws.close_entries(ADMIN)
    return notices, pub, part, draws, draw, sleeps


def notices_to(pub, uid):
    return [h for to, h, kb in pub.sent if to == uid and kb and kb[-1][0].callback == "notify:off"]


async def test_every_entrant_gets_one_notice_with_the_mute_toggle_and_the_post_link(world):
    notices, pub, _, _, draw, sleeps = world
    assert await notices.pending() == [draw.id]

    assert await notices.send_weekly(draw.id) == 4
    assert await notices.send_weekly(draw.id) == 0  # finished: nothing is re-sent
    for uid in USERS:
        [text] = notices_to(pub, uid)
        assert f"№{draw.id}" in text
    kb = next(kb for to, _, kb in pub.sent if to == 1)
    assert kb[0][0].url == pub.post_link(draw.commit_msg_id)
    assert sleeps == [0.1] * 4  # throttled
    assert await notices.pending() == []


async def test_muted_entrants_get_nothing(world):
    notices, pub, part, _, draw, _ = world
    await part.set_notify(2, False)
    assert await notices.send_weekly(draw.id) == 3
    assert notices_to(pub, 2) == []


async def test_a_run_that_crashed_halfway_resumes_without_resending(world, sessionmaker):
    notices, pub, _, _, draw, _ = world
    async with sessionmaker() as s, s.begin():  # entries 0 and 1 were handled before a crash
        await s.execute(
            update(DrawEntry)
            .where(DrawEntry.draw_id == draw.id, DrawEntry.position < 2)
            .values(notified=True)
        )
    assert await notices.send_weekly(draw.id) == 2
    assert sum(len(notices_to(pub, uid)) for uid in USERS) == 2


async def test_concurrent_runs_send_each_notice_once(world):
    notices, pub, _, _, draw, _ = world
    await asyncio.gather(notices.send_weekly(draw.id), notices.send_weekly(draw.id))
    assert all(len(notices_to(pub, uid)) == 1 for uid in USERS)


async def test_unreachable_entrants_do_not_stop_the_run(world):
    notices, pub, _, _, draw, _ = world
    pub.unreachable = {1, 3}
    assert await notices.send_weekly(draw.id) == 2
    assert await notices.pending() == []


async def test_draw_without_a_commitment_post_is_not_announced_yet(world, sessionmaker):
    notices, pub, _, _, draw, _ = world
    async with sessionmaker() as s, s.begin():
        await s.execute(update(Draw).where(Draw.id == draw.id).values(commit_msg_id=None))
    assert await notices.pending() == []
    assert await notices.send_weekly(draw.id) == 0


async def test_redraws_do_not_repeat_the_notice(world, sessionmaker):
    notices, _, _, draws, draw, _ = world
    async with sessionmaker() as s, s.begin():
        child = await draws._insert_draw(s, [("ABC", 1)], admin_id=ADMIN, parent_id=draw.id)
        child.commit_msg_id = 5
    assert child.id not in await notices.pending()
    assert child.notice_done  # marked done itself, not only filtered as a redraw
