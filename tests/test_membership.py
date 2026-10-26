from types import SimpleNamespace

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import GetChatMember

from giveaway_bot.ports.membership import Membership, TelegramMembership

METHOD = GetChatMember(chat_id="@c", user_id=1)


class StubBot:
    """Returns/raises the queued outcomes of getChatMember in order."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def get_chat_member(self, chat_id, user_id):
        self.calls += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def member(status, **kw):
    return SimpleNamespace(status=status, **kw)


async def no_sleep(_):
    return None


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr("giveaway_bot.ports.membership.asyncio.sleep", no_sleep)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (member(ChatMemberStatus.MEMBER), Membership.YES),
        (member(ChatMemberStatus.ADMINISTRATOR), Membership.YES),
        (member(ChatMemberStatus.CREATOR), Membership.YES),
        (member(ChatMemberStatus.RESTRICTED, is_member=True), Membership.YES),
        (member(ChatMemberStatus.RESTRICTED, is_member=False), Membership.NO),
        (member(ChatMemberStatus.LEFT), Membership.NO),
        (member(ChatMemberStatus.KICKED), Membership.BANNED),
        (TelegramBadRequest(METHOD, "Bad Request: user not found"), Membership.NO),
        (TelegramBadRequest(METHOD, "Bad Request: PARTICIPANT_ID_INVALID"), Membership.NO),
    ],
)
async def test_status_mapping(outcome, expected):
    assert await TelegramMembership(StubBot(outcome), "@c").check(1) is expected


@pytest.mark.parametrize(
    "error",
    [
        # A wrong CHANNEL_ID or lost access must not turn every participant into "unsubscribed".
        TelegramBadRequest(METHOD, "Bad Request: chat not found"),
        TelegramForbiddenError(METHOD, "Forbidden: bot is not a member of the channel chat"),
        TelegramServerError(METHOD, "Internal Server Error"),
        TelegramNetworkError(METHOD, "timeout"),
    ],
)
async def test_api_errors_are_unknown_after_retries(error):
    bot = StubBot(error)
    assert await TelegramMembership(bot, "@c", attempts=3).check(1) is Membership.UNKNOWN
    assert bot.calls == 3


async def test_rate_limit_inside_a_user_request_is_unknown_instead_of_a_long_sleep():
    bot = StubBot(TelegramRetryAfter(METHOD, "Too Many Requests", retry_after=30))
    m = TelegramMembership(bot, "@c", max_inline_wait=1)
    assert await m.check(1) is Membership.UNKNOWN


async def test_short_rate_limit_is_waited_out():
    bot = StubBot(
        TelegramRetryAfter(METHOD, "Too Many Requests", retry_after=1),
        member(ChatMemberStatus.MEMBER),
    )
    assert await TelegramMembership(bot, "@c").check(1) is Membership.YES
