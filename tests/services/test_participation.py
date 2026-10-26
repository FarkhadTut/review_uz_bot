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
