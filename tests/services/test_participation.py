import pytest

from giveaway_bot.ports.membership import FakeMembership, Membership
from giveaway_bot.services.participation import EnterOutcome, Participation

ALICE, BOB, STAFF, CH_ADMIN, BOT_ADMIN = 1, 2, 3, 4, 5


@pytest.fixture
def members():
    return FakeMembership({ALICE: Membership.YES, STAFF: Membership.YES}, admins={CH_ADMIN})


@pytest.fixture
def svc(sessionmaker, members, clock):
    return Participation(sessionmaker, members, bot_admin_ids={BOT_ADMIN}, now=clock)


async def test_first_start_records_whether_user_was_already_in_channel(svc, members):
    await svc.register_start(ALICE, "Alice", "alice", source="post_12")
    await svc.register_start(BOB, "Bob", None, source=None)
    members.members[BOB] = Membership.YES
    await svc.register_start(BOB, "Bob", "bobby", source="other")  # second /start changes nothing

    alice, bob = await svc.get(ALICE), await svc.get(BOB)
    assert (alice.member_at_first_start, alice.source) == (True, "post_12")
    assert (bob.member_at_first_start, bob.source, bob.username) == (False, None, "bobby")


async def test_subscribed_user_enters_once_and_keeps_the_same_code(svc):
    await svc.register_start(ALICE, "Alice", None)
    first = await svc.enter(ALICE)
    again = await svc.enter(ALICE)

    assert first.outcome is EnterOutcome.ENTERED
    assert again.outcome is EnterOutcome.ALREADY_ENTERED
    assert first.code == again.code and len(first.code) == 8


async def test_unsubscribed_user_cannot_enter_until_they_subscribe(svc, members):
    await svc.register_start(BOB, "Bob", None)
    assert (await svc.enter(BOB)).outcome is EnterOutcome.NOT_SUBSCRIBED
    assert (await svc.get(BOB)).entry_code is None

    members.members[BOB] = Membership.YES
    assert (await svc.enter(BOB)).outcome is EnterOutcome.ENTERED


async def test_api_failure_is_reported_not_treated_as_unsubscribed(svc, members):
    members.members[BOB] = Membership.UNKNOWN
    await svc.register_start(BOB, "Bob", None)
    assert (await svc.enter(BOB)).outcome is EnterOutcome.CHECK_FAILED
    assert (await svc.get(BOB)).member_at_first_start is None


@pytest.mark.parametrize("who", [STAFF, CH_ADMIN, BOT_ADMIN])
async def test_staff_channel_admins_and_bot_admins_are_not_eligible(svc, members, who):
    members.members[who] = Membership.YES
    await svc.add_staff(STAFF, added_by=BOT_ADMIN)
    await svc.register_start(who, "X", None)
    assert (await svc.enter(who)).outcome is EnterOutcome.NOT_ELIGIBLE


async def test_staff_list_can_be_edited(svc):
    await svc.add_staff(STAFF, added_by=BOT_ADMIN)
    assert await svc.staff_ids() == {STAFF}
    assert await svc.remove_staff(STAFF) is True
    assert await svc.staff_ids() == set()
    assert await svc.remove_staff(STAFF) is False


@pytest.mark.parametrize("make_excluded", ["staff", "channel_admin"])
async def test_entrant_who_becomes_staff_or_channel_admin_is_told_not_eligible(
    svc, members, make_excluded
):
    await svc.register_start(ALICE, "Alice", None)
    assert (await svc.enter(ALICE)).outcome is EnterOutcome.ENTERED
    if make_excluded == "staff":
        await svc.add_staff(ALICE, added_by=BOT_ADMIN)
    else:
        members.admins.add(ALICE)

    assert (await svc.enter(ALICE)).outcome is EnterOutcome.NOT_ELIGIBLE


async def test_concurrent_first_start_creates_one_user_without_errors(svc):
    import asyncio

    users = await asyncio.gather(
        svc.register_start(ALICE, "Alice", None), svc.register_start(ALICE, "Alice", "al")
    )
    assert {u.tg_id for u in users} == {ALICE}


async def test_membership_is_checked_outside_any_open_db_session(sessionmaker, clock):
    """A slow getChatMember must not hold a pooled connection."""
    open_sessions = {"n": 0}

    class Tracked:
        def __call__(self):
            inner = sessionmaker()

            class Ctx:
                async def __aenter__(self):
                    open_sessions["n"] += 1
                    return await inner.__aenter__()

                async def __aexit__(self, *exc):
                    open_sessions["n"] -= 1
                    return await inner.__aexit__(*exc)

            return Ctx()

    class CheckingMembership(FakeMembership):
        async def check(self, tg_id):
            assert open_sessions["n"] == 0, "Telegram call made while a DB session was open"
            return Membership.YES

    part = Participation(Tracked(), CheckingMembership(), bot_admin_ids=set(), now=clock)
    await part.register_start(ALICE, "Alice", None)
    assert (await part.get(ALICE)).member_at_first_start is True


async def test_admins_allowed_for_testing_can_enter_despite_being_admins(
    sessionmaker, members, clock
):
    members.members[BOT_ADMIN] = members.members[CH_ADMIN] = Membership.YES
    svc = Participation(
        sessionmaker,
        members,
        bot_admin_ids={BOT_ADMIN},
        allowed_ids={BOT_ADMIN, CH_ADMIN},
        now=clock,
    )
    for uid in (BOT_ADMIN, CH_ADMIN):
        await svc.register_start(uid, "Admin", None)
        assert (await svc.enter(uid)).outcome is EnterOutcome.ENTERED
        assert not await svc.is_excluded(uid)
