import pytest
from sqlalchemy import select

from giveaway_bot.db.models import AuditLog
from giveaway_bot.i18n import CHANNEL_LANG, t
from giveaway_bot.ports.publisher import FakePublisher
from giveaway_bot.services.announcements import MAX_TEXT, Announcements, participate_keyboard

ADMIN = 900


@pytest.fixture
def pub():
    return FakePublisher()


@pytest.fixture
def ann(sessionmaker, pub, clock):
    return Announcements(sessionmaker, pub, bot_username="bot", now=clock)


async def test_default_text_until_an_admin_saves_one_and_the_saved_one_persists(
    ann, sessionmaker, pub, clock
):
    assert await ann.text() == t(CHANNEL_LANG, "channel_announcement")
    await ann.set_text("<b>New</b> text", admin_id=ADMIN)
    fresh = Announcements(sessionmaker, pub, bot_username="bot", now=clock)  # after a restart
    assert await fresh.text() == "<b>New</b> text"


@pytest.mark.parametrize("bad", ["", "   \n", "x" * (MAX_TEXT + 1)])
async def test_empty_or_too_long_text_is_refused_and_the_old_one_kept(ann, bad):
    await ann.set_text("kept", admin_id=ADMIN)
    with pytest.raises(ValueError):
        await ann.set_text(bad, admin_id=ADMIN)
    assert await ann.text() == "kept"


async def test_publish_posts_the_text_with_the_participate_button_and_pin_pins_it(
    ann, pub, sessionmaker
):
    await ann.set_text("Join us", admin_id=ADMIN)
    msg_id = await ann.publish(admin_id=ADMIN)
    assert pub.channel == ["Join us"] and pub.channel_kb == [participate_keyboard("bot")]

    await ann.pin(msg_id, admin_id=ADMIN)
    assert pub.pinned == [msg_id]
    async with sessionmaker() as s:
        actions = [a.action for a in await s.scalars(select(AuditLog).order_by(AuditLog.id))]
    assert actions == ["announce_text", "announce_publish", "announce_pin"]
