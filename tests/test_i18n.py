import string

import pytest

from giveaway_bot.i18n import CHANNEL_LANG, LANGS, catalog, t


def _fields(text: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


@pytest.mark.parametrize("lang", LANGS)
def test_every_language_has_the_same_user_facing_keys_and_placeholders(lang):
    base = {k: v for k, v in catalog(CHANNEL_LANG).items() if not k.startswith("channel_")}
    other = catalog(lang)
    assert set(other) - {k for k in other if k.startswith("channel_")} == set(base)
    for key, text in base.items():
        assert _fields(other[key]) == _fields(text), f"{lang}.{key}"


def test_channel_posts_exist_only_in_channel_language():
    assert any(k.startswith("channel_") for k in catalog(CHANNEL_LANG))
    for lang in LANGS:
        if lang != CHANNEL_LANG:
            assert not any(k.startswith("channel_") for k in catalog(lang))


def test_t_formats_and_falls_back_to_channel_language():
    assert "K7M2Q9XA" in t("ru", "rules_in", code="K7M2Q9XA")
    assert t(None, "btn_channel") == catalog(CHANNEL_LANG)["btn_channel"]
