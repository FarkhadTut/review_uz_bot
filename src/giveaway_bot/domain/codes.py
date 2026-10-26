import re
import secrets

# Crockford base32: no I, L, O, U, so codes survive being read aloud or retyped.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ENTRY_CODE_LENGTH = 8

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$")


def new_entry_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(ENTRY_CODE_LENGTH))


def normalize_contact(raw: str) -> str | None:
    """Email (lowercased) or Uzbek phone as +998XXXXXXXXX; None when neither."""
    text = raw.strip()
    if "@" in text:
        email = text.lower()
        return email if _EMAIL.match(email) else None
    digits = re.sub(r"[\s\-()]", "", text).removeprefix("+")
    if not digits.isdigit():
        return None
    if len(digits) == 9:
        digits = "998" + digits
    if len(digits) == 12 and digits.startswith("998"):
        return "+" + digits
    return None
