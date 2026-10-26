from giveaway_bot.ports.membership import FakeMembership, Membership
from giveaway_bot.services.participation import Participation
from giveaway_bot.services.stats import Stats

ADMIN = 900


async def test_growth_and_30_day_retention_of_new_to_channel_entrants(sessionmaker, clock):
    members = FakeMembership()
    part = Participation(sessionmaker, members, bot_admin_ids={ADMIN}, now=clock)
    stats = Stats(sessionmaker, members, part, admin_ids={ADMIN}, retention_days=30, now=clock)

    # 1 was already in the channel; 2 and 3 joined because of the contest; 4 never entered
    members.members[1] = Membership.YES
    for uid in (1, 2, 3, 4):
        await part.register_start(uid, f"U{uid}", None)
    for uid in (2, 3):
        members.members[uid] = Membership.YES
    for uid in (1, 2, 3):
        await part.enter(uid)

    clock.advance(days=29)
    assert await stats.run_retention_batch() == 0  # nobody is 30 days in yet

    clock.advance(days=1)
    members.members[3] = Membership.NO  # left the channel
    assert await stats.run_retention_batch() == 2
    assert await stats.run_retention_batch() == 0  # each entrant is checked once

    s = await stats.snapshot()
    assert (s.started, s.entered, s.new_to_channel, s.pool, s.winners) == (4, 3, 2, 3, 0)
    assert (s.retained, s.retention_checked) == (1, 2)


async def test_unknown_membership_is_not_recorded_and_retried_later(sessionmaker, clock):
    members = FakeMembership()
    part = Participation(sessionmaker, members, bot_admin_ids=set(), now=clock)
    stats = Stats(sessionmaker, members, part, admin_ids=set(), retention_days=30, now=clock)
    await part.register_start(1, "U", None)
    members.members[1] = Membership.YES
    await part.enter(1)
    clock.advance(days=31)

    members.members[1] = Membership.UNKNOWN
    assert await stats.run_retention_batch() == 0
    members.members[1] = Membership.YES
    assert await stats.run_retention_batch() == 1
