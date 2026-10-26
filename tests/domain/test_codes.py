import re

import pytest

from giveaway_bot.domain.codes import new_entry_code, normalize_contact


def test_entry_code_is_8_char_crockford_base32_and_random():
    codes = {new_entry_code() for _ in range(500)}
    assert len(codes) == 500
    assert all(re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{8}", c) for c in codes)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("User@Mail.uz ", "user@mail.uz"),
        ("+998 90 123-45-67", "+998901234567"),
        ("998901234567", "+998901234567"),
        ("90 123 45 67", "+998901234567"),
    ],
)
def test_normalize_contact_accepts_email_or_uzbek_phone(raw, expected):
    assert normalize_contact(raw) == expected


@pytest.mark.parametrize("raw", ["", "hello", "+7 999 123 45 67", "user@", "12345"])
def test_normalize_contact_rejects_garbage(raw):
    assert normalize_contact(raw) is None
