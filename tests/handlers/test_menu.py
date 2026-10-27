import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SetMyCommands, SetMyDescription, SetMyShortDescription

from giveaway_bot.domain.codes import normalize_contact
from giveaway_bot.handlers.menu import MENU_KEYS, commands, menu_action, setup_profile
from giveaway_bot.i18n import LANGS, t
from tests.handlers.test_handlers import RecordingSession


@pytest.mark.parametrize("lang", LANGS)
def test_profile_texts_fit_telegram_limits(lang):
    assert len(t(lang, "bot_description")) <= 512
    assert len(t(lang, "bot_short_description")) <= 120
    for c in commands(lang, admin=True):
        assert 1 <= len(c.description) <= 256


def test_menu_labels_are_unambiguous_and_never_look_like_a_contact():
    labels = [t(lang, key) for lang in LANGS for key in MENU_KEYS]
    for lang in LANGS:
        for key in MENU_KEYS:
            assert menu_action(t(lang, key)) == key
    assert all(normalize_contact(label) is None for label in labels)
    assert menu_action("hello") is None and menu_action(None) is None


class FailingChatSession(RecordingSession):
    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109
        self.requests.append(method)
        if isinstance(method, SetMyCommands) and method.scope.type == "chat":
            raise TelegramBadRequest(method, "chat not found")
        return True


async def test_startup_profile_sets_menus_per_language_and_survives_api_errors():
    session = FailingChatSession()
    bot = Bot("123:abc", session=session)
    await setup_profile(bot, {900: None, 901: "ru"})  # admins never opened the bot: errors

    defaults = [
        r for r in session.requests if isinstance(r, SetMyCommands) and r.scope.type == "default"
    ]
    assert {r.language_code for r in defaults} == {None, "ru"}
    ru = next(r for r in defaults if r.language_code == "ru")
    assert ru.commands[0].description == t("ru", "cmd_start")
    assert {r.language_code for r in session.requests if isinstance(r, SetMyDescription)} == {
        None,
        "ru",
    }
    assert len([r for r in session.requests if isinstance(r, SetMyShortDescription)]) == 2
    chats = [r for r in session.requests if isinstance(r, SetMyCommands) and r.scope.type == "chat"]
    assert {r.scope.chat_id for r in chats} == {900, 901}
