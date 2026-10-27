"""Updates go through the real aiogram Dispatcher and routers; only the Bot API session is
replaced, recording every outgoing request."""

import asyncio
import hashlib
from datetime import datetime
from itertools import count

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendDocument, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Document, Message, Update, User

from giveaway_bot.config import Settings
from giveaway_bot.handlers import admin, user
from giveaway_bot.handlers.user import VerifyLimiter
from giveaway_bot.i18n import t
from giveaway_bot.ports.beacon import FakeBeacon
from giveaway_bot.ports.membership import FakeMembership, Membership
from giveaway_bot.ports.publisher import FakePublisher
from giveaway_bot.services.claims import Claims
from giveaway_bot.services.draw_service import DrawService
from giveaway_bot.services.participation import Participation
from giveaway_bot.services.runner import Runner
from giveaway_bot.services.stats import Stats

ADMIN, ALICE, MALLORY, BOB = 900, 1, 666, 2


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests: list[TelegramMethod] = []
        self._ids = count(100)

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109 (aiogram API)
        self.requests.append(method)
        if isinstance(method, SendMessage | SendDocument):
            msg_id = next(self._ids)
            document = (
                Document(file_id=f"file-{msg_id}", file_unique_id=f"u{msg_id}")
                if isinstance(method, SendDocument)
                else None
            )
            return Message(
                message_id=msg_id,
                date=datetime.now(),
                chat=Chat(id=method.chat_id, type="private"),
                text=getattr(method, "text", None),
                document=document,
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
        settings=Settings(_env_file=None, bot_token="1:x", admin_ids=[ADMIN]),
        admin_ids={ADMIN},
        participation=part,
        draws=draws,
        claims=claims,
        stats=stats,
        runner=runner,
        verify_limiter=VerifyLimiter(cooldown=0),
    )
    dp.include_routers(admin.router, user.router)
    session = RecordingSession()
    bot = Bot("123:abc", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    updates = count(1)

    async def send_text(from_id: int, text: str, chat_type: str = "private"):
        msg = Message(
            message_id=next(updates),
            date=datetime.now(),
            chat=Chat(id=from_id if chat_type == "private" else -100, type=chat_type),
            from_user=User(id=from_id, is_bot=False, first_name=f"U{from_id}"),
            text=text,
            entities=(
                [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
                if text.startswith("/")
                else None
            ),
        )
        await dp.feed_update(bot, Update(update_id=next(updates), message=msg))

    async def press(from_id: int, data: str, markup=None):
        query = CallbackQuery(
            id=str(next(updates)),
            from_user=User(id=from_id, is_bot=False, first_name=f"U{from_id}"),
            chat_instance="ci",
            data=data,
            message=Message(
                message_id=1,
                date=datetime.now(),
                chat=Chat(id=from_id, type="private"),
                text="x",
                reply_markup=markup,
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
    a.beacon, a.members, a.pub, a.claims, a.dp = beacon, members, pub, claims, dp
    a.clock = clock
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


async def test_verify_sends_formula_and_entry_code_file_once_announced(app):
    for uid in (ALICE, MALLORY, BOB):
        app.members.members[uid] = Membership.YES
        await app.send_text(uid, "/start")
        await app.press(uid, "join")
    draw = await app.draws.close_entries(ADMIN)
    app.beacon.values[draw.beacon_round] = "ef" * 32
    await app.draws.resolve(draw.id)

    await app.send_text(ALICE, "/verify")  # drawn but not announced: nothing revealed
    assert app.session.texts_to(ALICE)[-1] == t("uz_cyrl", "verify_none")

    await app.draws.approve(draw.id, ADMIN)
    await app.send_text(ALICE, f"/verify {draw.id}")

    assert draw.snapshot_hash in app.session.texts_to(ALICE)[-1]
    data = [r for r in app.session.requests if isinstance(r, SendDocument)][-1].document.data
    assert hashlib.sha256(data).hexdigest() == draw.snapshot_hash
    codes = {(await app.part.get(uid)).entry_code for uid in (ALICE, MALLORY, BOB)}
    assert set(data.decode().split(chr(10))) == codes


async def test_verify_rejects_garbage_arguments(app):
    await app.send_text(ALICE, "/start")
    for arg in ("abc", "²", "9" * 40):
        await app.send_text(ALICE, f"/verify {arg}")
        assert app.session.texts_to(ALICE)[-1] == t(None, "verify_unknown")


async def test_winner_who_missed_the_dm_gets_claim_instructions_on_start(app):
    await app.send_text(ALICE, "/start")
    await app.press(ALICE, "lang:ru")
    await app.press(ALICE, "join")
    draw = await app.draws.close_entries(ADMIN)
    app.beacon.values[draw.beacon_round] = "ef" * 32
    await app.draws.resolve(draw.id)
    await app.draws.approve(draw.id, ADMIN)

    await app.send_text(ALICE, "/start")
    assert app.session.texts_to(ALICE)[-1] == t("ru", "win_dm", hours=48)

    await app.send_text(ALICE, "+998 90 123 45 67")
    assert "+998901234567" in app.session.texts_to(ALICE)[-1]
    await app.send_text(ALICE, "/status")
    assert app.session.texts_to(ALICE)[-1] == t("ru", "status_won")


async def test_double_confirm_press_publishes_once(app):
    await app.send_text(ALICE, "/start")
    await app.press(ALICE, "join")
    draw = await app.draws.close_entries(ADMIN)
    app.beacon.values[draw.beacon_round] = "ef" * 32
    await app.draws.resolve(draw.id)

    await asyncio.gather(
        app.press(ADMIN, f"adm:approve!:{draw.id}"), app.press(ADMIN, f"adm:approve!:{draw.id}")
    )
    assert len(app.pub.channel) == 2  # commitment + one results post
    # The second press is told it was already done instead of failing silently.
    assert app.session.texts_to(ADMIN) == [
        t(None, "admin_already_done", status=t(None, "state_published"))
    ]


async def test_group_chats_are_ignored(app):
    await app.send_text(ALICE, "/start", chat_type="group")
    await app.send_text(ADMIN, "/admin", chat_type="group")
    assert app.session.requests == []


async def test_malformed_admin_callbacks_are_ignored(app):
    for data in ("adm:", "adm:close", "adm:retry:x", "adm:retry:99999999999", "adm:x:1:2"):
        await app.press(ADMIN, data)
    assert app.session.texts_to(ADMIN) == []
    assert all(isinstance(r, AnswerCallbackQuery) for r in app.session.requests)


async def drawn_and_published(app, uids=(ALICE,)):
    for uid in uids:
        app.members.members[uid] = Membership.YES
        await app.send_text(uid, "/start")
        await app.press(uid, "join")
    draw = await app.draws.close_entries(ADMIN)
    app.beacon.values[draw.beacon_round] = "ef" * 32
    await app.draws.resolve(draw.id)
    await app.draws.approve(draw.id, ADMIN)
    return await app.draws.get(draw.id)


async def test_verify_reuses_the_uploaded_file_and_rate_limits_each_user(app):
    draw = await drawn_and_published(app, (ALICE, MALLORY))
    app.dp["verify_limiter"] = VerifyLimiter(cooldown=30)

    await app.send_text(ALICE, f"/verify {draw.id}")
    await app.send_text(MALLORY, f"/verify {draw.id}")
    await app.send_text(MALLORY, f"/verify {draw.id}")  # too soon

    docs = [r for r in app.session.requests if isinstance(r, SendDocument)]
    assert len(docs) == 2
    assert not isinstance(docs[0].document, str)  # first: uploaded
    assert isinstance(docs[1].document, str)  # second: Telegram file_id, no re-upload
    assert app.session.texts_to(MALLORY)[-1].startswith(
        t("uz_cyrl", "verify_cooldown", seconds=30)[:20]
    )


async def test_verify_by_id_before_announcement_reveals_nothing(app):
    await app.send_text(ALICE, "/start")
    await app.press(ALICE, "join")
    draw = await app.draws.close_entries(ADMIN)
    app.beacon.values[draw.beacon_round] = "ef" * 32
    await app.draws.resolve(draw.id)  # drawn, not announced

    await app.send_text(ALICE, f"/verify {draw.id}")
    assert app.session.texts_to(ALICE)[-1] == t("uz_cyrl", "verify_pending")
    assert not [r for r in app.session.requests if isinstance(r, SendDocument)]


async def test_status_tells_an_entrant_who_became_staff_they_are_not_eligible(app):
    await app.send_text(ALICE, "/start")
    await app.press(ALICE, "join")
    await app.part.add_staff(ALICE, added_by=ADMIN)

    await app.send_text(ALICE, "/status")
    assert app.session.texts_to(ALICE)[-1] == t("uz_cyrl", "not_eligible")
    await app.press(ALICE, "join")
    assert app.session.texts_to(ALICE)[-1] == t("uz_cyrl", "not_eligible")


async def test_second_claim_message_is_acknowledged_not_answered_with_help(app):
    await drawn_and_published(app)
    await app.send_text(ALICE, "a@b.uz")
    await app.send_text(ALICE, "c@d.uz")
    assert app.session.texts_to(ALICE)[-1] == t("uz_cyrl", "claim_already")


async def test_close_reports_when_telegram_cannot_list_channel_admins(app):
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import GetChatAdministrators

    await app.send_text(ALICE, "/start")
    await app.press(ALICE, "join")

    async def down(*, fresh=False):
        raise TelegramNetworkError(GetChatAdministrators(chat_id=1), "timeout")

    app.members.channel_admin_ids = down
    await app.press(ADMIN, "adm:close!:0")
    assert await app.draws.current() is None
    assert app.session.texts_to(ADMIN)[-1] == t(None, "check_error")


async def test_admin_redraw_activate_and_retry_buttons(app):
    from sqlalchemy import select

    from giveaway_bot.db.models import AuditLog

    draw = await drawn_and_published(app, (ALICE, MALLORY))

    await app.press(ADMIN, f"adm:redraw!:{draw.id}")  # claim window still open
    assert app.session.texts_to(ADMIN)[-1] == t(None, "admin_redraw_too_early", hours=48)

    app.clock.advance(hours=49)
    await app.claims.expire_due()
    await app.press(ADMIN, f"adm:redraw!:{draw.id}")
    child = await app.draws.current()
    assert child.parent_id == draw.id and app.runner.spawned[-1] == child.id
    await app.press(ADMIN, f"adm:redraw!:{draw.id}")  # repeat press
    assert app.session.texts_to(ADMIN)[-1] == t(
        None, "admin_already_done", status=t(None, "state_redrawn")
    )

    await app.press(ADMIN, f"adm:retry:{child.id}")
    assert app.runner.spawned[-1] == child.id
    async with app.draws.sm() as s:
        actions = list(await s.scalars(select(AuditLog.action)))
    assert "retry" in actions
    await app.press(ADMIN, "adm:retry:999")
    assert app.session.texts_to(ADMIN)[-1] == t(None, "admin_already_done", status="—")

    app.beacon.values[child.beacon_round] = "ef" * 32
    await app.draws.resolve(child.id)
    await app.draws.approve(child.id, ADMIN)
    child = await app.draws.get(child.id)
    await app.send_text(child.winner_tg_id, "a@b.uz")
    await app.press(ADMIN, f"adm:activate:{child.id}")
    assert app.session.texts_to(ADMIN)[-1] == t(None, "admin_activated", draw_no=child.id)
    await app.press(ADMIN, f"adm:activate:{child.id}")
    assert app.session.texts_to(ADMIN)[-1] == t(
        None, "admin_already_done", status=t(None, "state_activated")
    )


def _last(app, chat_id, kind):
    return [r for r in app.session.requests if isinstance(r, kind) and r.chat_id == chat_id][-1]


async def started(app, uid, lang="uz_cyrl"):
    await app.send_text(uid, "/start")
    await app.press(uid, f"lang:{lang}")


async def test_start_shows_the_bottom_menu_and_the_rules_card(app):
    from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

    await started(app, ALICE)
    await app.send_text(ALICE, "/start")
    sends = [r for r in app.session.requests if isinstance(r, SendMessage) and r.chat_id == ALICE]
    menu, rules = sends[-2], sends[-1]
    assert isinstance(menu.reply_markup, ReplyKeyboardMarkup)
    labels = [b.text for row in menu.reply_markup.keyboard for b in row]
    assert labels == [
        t("uz_cyrl", k) for k in ("menu_contest", "menu_status", "menu_verify", "menu_language")
    ]
    assert isinstance(rules.reply_markup, InlineKeyboardMarkup)
    buttons = [b for row in rules.reply_markup.inline_keyboard for b in row]
    assert buttons[0].callback_data == "join" and buttons[1].url


async def test_menu_buttons_work_in_every_language(app):
    await started(app, ALICE, "ru")
    await app.press(ALICE, "join")
    await app.send_text(ALICE, t("ru", "menu_status"))
    assert t("ru", "notify_state_on") in app.session.texts_to(ALICE)[-1]
    await app.send_text(ALICE, t("uz_latn", "menu_contest"))  # a label from another language
    assert "@reviewuznews" in app.session.texts_to(ALICE)[-1]
    await app.send_text(ALICE, t("ru", "menu_language"))
    assert app.session.texts_to(ALICE)[-1] == t(None, "lang_prompt")


async def test_pending_winner_pressing_a_menu_button_is_not_taken_as_a_claim(app):
    draw = await drawn_and_published(app)
    await app.send_text(ALICE, t("uz_cyrl", "menu_status"))
    await app.send_text(ALICE, t("uz_cyrl", "menu_verify"))
    assert (await app.draws.get(draw.id)).status == "published"
    assert t("uz_cyrl", "claim_invalid") not in app.session.texts_to(ALICE)
    assert not any("adm:activate" in str(kb) for to, _, kb in app.pub.sent if to == ADMIN)


async def test_mute_toggle_flips_the_setting_and_the_button_in_place(app):
    from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup

    from giveaway_bot.ports.publisher import to_markup
    from giveaway_bot.services.notices import notice_keyboard

    await started(app, ALICE)
    notice = to_markup(notice_keyboard("uz_cyrl", True, "https://t.me/c/1/5"))
    await app.press(ALICE, "notify:off", notice)

    assert (await app.part.get(ALICE)).notify is False
    edit = [r for r in app.session.requests if isinstance(r, EditMessageReplyMarkup)][-1]
    rows = edit.reply_markup.inline_keyboard
    assert rows[0][0].url == "https://t.me/c/1/5"  # the other button is kept
    assert rows[1][0].callback_data == "notify:on"
    assert rows[1][0].text == t("uz_cyrl", "btn_notify_on")
    answer = [r for r in app.session.requests if isinstance(r, AnswerCallbackQuery)][-1]
    assert answer.text == t("uz_cyrl", "notify_muted")

    await app.press(ALICE, "notify:on", edit.reply_markup)
    assert (await app.part.get(ALICE)).notify is True


async def test_status_shows_the_notice_setting_with_its_toggle(app):
    from aiogram.methods import EditMessageText

    await started(app, ALICE)
    await app.press(ALICE, "join")
    await app.send_text(ALICE, "/status")
    status = _last(app, ALICE, SendMessage)
    assert t("uz_cyrl", "notify_state_on") in status.text
    assert status.reply_markup.inline_keyboard[0][0].callback_data == "notify:off:status"

    await app.press(ALICE, "notify:off:status")
    edited = [r for r in app.session.requests if isinstance(r, EditMessageText)][-1]
    assert t("uz_cyrl", "notify_state_off") in edited.text
    assert edited.reply_markup.inline_keyboard[0][0].callback_data == "notify:on:status"


async def test_choosing_a_language_sets_that_chats_command_menu(app):
    from aiogram.methods import SetMyCommands

    await started(app, ALICE, "ru")
    await started(app, ADMIN, "uz_latn")
    menus = {
        r.scope.chat_id: r.commands for r in app.session.requests if isinstance(r, SetMyCommands)
    }
    assert [c.command for c in menus[ALICE]] == ["start", "status", "verify", "language", "help"]
    assert menus[ALICE][0].description == t("ru", "cmd_start")
    assert "admin" in [c.command for c in menus[ADMIN]]
    assert menus[ADMIN][0].description == t("uz_latn", "cmd_start")


def _join_button(markup):
    return next(b for row in markup.inline_keyboard for b in row if b.callback_data == "join")


async def test_join_button_turns_into_you_are_in_after_entering(app):
    from aiogram.methods import EditMessageReplyMarkup

    await started(app, ALICE, "ru")
    rules = _last(app, ALICE, SendMessage)
    await app.press(ALICE, "join", rules.reply_markup)

    edit = [r for r in app.session.requests if isinstance(r, EditMessageReplyMarkup)][-1]
    assert _join_button(edit.reply_markup).text == t("ru", "btn_joined")
    assert any(b.url for row in edit.reply_markup.inline_keyboard for b in row)  # channel kept

    before = len(app.session.requests)
    await app.press(ALICE, "join", edit.reply_markup)  # already shows it: no pointless edit
    assert not any(isinstance(r, EditMessageReplyMarkup) for r in app.session.requests[before:])
    assert t("ru", "btn_joined") != t("ru", "btn_join")


async def test_rules_card_shows_you_are_in_to_someone_already_entered(app):
    await started(app, ALICE, "ru")
    assert _join_button(_last(app, ALICE, SendMessage).reply_markup).text == t("ru", "btn_join")
    await app.press(ALICE, "join")
    await app.send_text(ALICE, "/start")
    assert _join_button(_last(app, ALICE, SendMessage).reply_markup).text == t("ru", "btn_joined")


async def test_current_draw_card_shows_the_winner_and_what_happens_next(app, clock):
    await started(app, ADMIN, "ru")
    draw = await drawn_and_published(app)
    alice = await app.part.get(ALICE)

    await app.press(ADMIN, "adm:current:0")
    card = app.session.texts_to(ADMIN)[-1]
    assert f"№{draw.id}" in card and t("ru", "state_published") in card
    assert "drawn" not in card and "published" not in card  # no raw status
    assert alice.first_name in card and alice.entry_code in card and str(ALICE) in card
    assert t("ru", "admin_left", h=48, m=0) in card

    clock.advance(hours=1)
    await app.send_text(ALICE, "alice@mail.uz")
    await app.press(ADMIN, "adm:current:0")
    card = app.session.texts_to(ADMIN)[-1]
    assert t("ru", "state_claimed") in card and "alice@mail.uz" in card

    await app.claims.activate(draw.id, ADMIN)
    await app.press(ADMIN, "adm:current:0")  # finished: the latest draw is still shown
    card = app.session.texts_to(ADMIN)[-1]
    assert f"№{draw.id}" in card and t("ru", "state_activated") in card
