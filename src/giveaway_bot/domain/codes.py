import re
import secrets

# Crockford base32: no I, L, O, U, so codes survive being read aloud or retyped.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ENTRY_CODE_LENGTH = 8


def new_entry_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(ENTRY_CODE_LENGTH))


def normalize_phone(raw: str | None) -> str | None:
    """A shared Telegram contact's number as +digits (Telegram may omit the plus);
    None when it isn't a plausible international number."""
    digits = re.sub(r"\D", "", raw or "")
    return f"+{digits}" if 7 <= len(digits) <= 15 else None
