"""Updates go through the real aiogram Dispatcher and routers; only the Bot API session is
replaced, recording every outgoing request."""

from datetime import datetime
from itertools import count

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendDocument, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from giveaway_bot.config import Settings
from giveaway_bot.handlers import admin, user
from giveaway_bot.ports.beacon import FakeBeacon
from giveaway_bot.ports.membership import FakeMembership, Membership
from giveaway_bot.ports.publisher import FakePublisher
from giveaway_bot.services.claims import Claims
from giveaway_bot.services.draw_service import DrawService
from giveaway_bot.services.participation import Participation
from giveaway_bot.services.runner import Runner
from giveaway_bot.services.stats import Stats

ADMIN, ALICE, MALLORY = 900, 1, 666


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests: list[TelegramMethod] = []
        self._ids = count(100)

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109 (aiogram API)
        self.requests.append(method)
        if isinstance(method, SendMessage | SendDocument):
            return Message(
                message_id=next(self._ids),
                date=datetime.now(),
                chat=Chat(id=method.chat_id, type="private"),
                text=getattr(method, "text", None),
            )
        return True

    async def stream_content(self, *a, **kw):  # pragma: no cover - unused
        raise NotImplementedError

    async def close(self):
        pass

    def texts_to(self, chat_id: int) -> list[str]:
        return [
            r.text for r in self.requests if isinstance(r, SendMessage) and r.chat_id == chat_id
        ]


class SpyRunner(Runner):
    def __init__(self):
        self.spawned: list[int] = []

    def spawn_resolve(self, draw_id: int) -> None:
        self.spawned.append(draw_id)


@pytest.fixture
async def app(sessionmaker, clock):
    members = FakeMembership({ALICE: Membership.YES, MALLORY: Membership.YES})
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
    claims = Claims(sessionmaker, pub, draws, admin_ids={ADMIN}, now=clock)
    stats = Stats(sessionmaker, members, part, admin_ids={ADMIN}, now=clock)
    runner = SpyRunner()
    dp = Dispatcher(
        settings=Settings(bot_token="1:x", admin_ids=[ADMIN]),
        admin_ids={ADMIN},
        participation=part,
        draws=draws,
        claims=claims,
        stats=stats,
        runner=runner,
    )
    dp.include_routers(admin.router, user.router)
    session = RecordingSession()
    bot = Bot("123:abc", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    updates = count(1)

    async def send_text(from_id: int, text: str):
        msg = Message(
            message_id=next(updates),
            date=datetime.now(),
            chat=Chat(id=from_id, type="private"),
            from_user=User(id=from_id, is_bot=False, first_name=f"U{from_id}"),
            text=text,
            entities=(
                [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
                if text.startswith("/")
                else None
            ),
        )
        await dp.feed_update(bot, Update(update_id=next(updates), message=msg))

    async def press(from_id: int, data: str):
        query = CallbackQuery(
            id=str(next(updates)),
            from_user=User(id=from_id, is_bot=False, first_name=f"U{from_id}"),
            chat_instance="ci",
            data=data,
            message=Message(
                message_id=1, date=datetime.now(), chat=Chat(id=from_id, type="private"), text="x"
            ),
        )
        await dp.feed_update(bot, Update(update_id=next(updates), callback_query=query))

    class App:
        pass

    a = App()
    a.session, a.send_text, a.press, a.runner, a.draws, a.part = (
        session,
        send_text,
        press,
        runner,
        draws,
        part,
    )
    a.beacon = beacon
    yield a
    # Module-level routers can belong to one dispatcher at a time; detach for the next test.
    for r in (admin.router, user.router):
        r._parent_router = None


async def test_user_starts_picks_language_and_joins(app):
    await app.send_text(ALICE, "/start post_7")
    assert "Tilni tanlang" in app.session.texts_to(ALICE)[-1]

    await app.press(ALICE, "lang:ru")
    assert "Участвовать" in app.session.texts_to(ALICE)[-1]

    await app.press(ALICE, "join")
    code = (await app.part.get(ALICE)).entry_code
    assert code in app.session.texts_to(ALICE)[-1]
    assert (await app.part.get(ALICE)).source == "post_7"


async def test_non_admin_cannot_close_entries(app):
    await app.send_text(MALLORY, "/start")
    await app.press(MALLORY, "lang:ru")
    await app.press(MALLORY, "join")

    await app.press(MALLORY, "adm:close!:0")
    await app.send_text(MALLORY, "/admin")

    assert await app.draws.current() is None
    assert app.runner.spawned == []
    assert not any("Панель" in t for t in app.session.texts_to(MALLORY))


async def test_admin_closes_with_confirmation_and_resolve_is_scheduled(app):
    await app.send_text(ALICE, "/start")
    await app.press(ALICE, "join")

    await app.press(ADMIN, "adm:close:0")
    assert await app.draws.current() is None  # first press only asks

    await app.press(ADMIN, "adm:close!:0")
    draw = await app.draws.current()
    assert draw is not None and app.runner.spawned == [draw.id]
    assert any(isinstance(r, AnswerCallbackQuery) for r in app.session.requests)


async def test_verify_sends_formula_and_entry_code_file(app):
    await app.send_text(ALICE, "/start")
    await app.press(ALICE, "join")
    draw = await app.draws.close_entries(ADMIN)
    app.beacon.values[draw.beacon_round] = "ef" * 32
    await app.draws.resolve(draw.id)

    await app.send_text(ALICE, "/verify")

    assert draw.snapshot_hash in app.session.texts_to(ALICE)[-1]
    docs = [r for r in app.session.requests if isinstance(r, SendDocument)]
    assert docs and docs[-1].document.data == (await app.part.get(ALICE)).entry_code.encode()
