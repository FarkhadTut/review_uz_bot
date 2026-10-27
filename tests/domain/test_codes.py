import re

import pytest

from giveaway_bot.domain.codes import new_entry_code, normalize_phone


def test_entry_code_is_8_char_crockford_base32_and_random():
    codes = {new_entry_code() for _ in range(500)}
    assert len(codes) == 500
    assert all(re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{8}", c) for c in codes)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+998 90 123-45-67", "+998901234567"),
        ("998901234567", "+998901234567"),  # Telegram often omits the plus
        ("+7 999 123 45 67", "+79991234567"),  # foreign numbers are fine: staff call them
    ],
)
def test_normalize_phone_keeps_digits_with_a_plus(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "hello", "12345", "1" * 16])
def test_normalize_phone_rejects_implausible_numbers(raw):
    assert normalize_phone(raw) is None
