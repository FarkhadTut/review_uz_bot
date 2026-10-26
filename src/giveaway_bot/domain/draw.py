"""Pure draw rules. Everything here is published to participants, so it must stay
recomputable by anyone from the public inputs (see tools/verify_draw.py)."""

import hashlib
from collections.abc import Iterator, Sequence


def snapshot_hash(entry_codes: Sequence[str]) -> str:
    """SHA-256 hex of the entry codes sorted ascending, joined by "\\n", UTF-8."""
    return hashlib.sha256("\n".join(sorted(entry_codes)).encode("utf-8")).hexdigest()


def winner_index(randomness_hex: str, snapshot_hash_hex: str, n: int) -> int:
    """Index into the sorted snapshot: SHA256(randomness || snapshot_hash) mod n."""
    if n <= 0:
        raise ValueError("empty snapshot")
    digest = hashlib.sha256(bytes.fromhex(randomness_hex) + bytes.fromhex(snapshot_hash_hex))
    return int.from_bytes(digest.digest(), "big") % n


def candidate_order(start: int, n: int) -> Iterator[int]:
    """Indexes to try when the drawn candidate is ineligible: forward from start, wrapping once."""
    for step in range(n):
        yield (start + step) % n


def mask_name(first_name: str, username: str | None, *, fallback: str = "") -> str:
    """Public form of a winner's name: first name plus at most two username letters."""
    name = (first_name or "").strip()
    if username:
        tag = f"@{username[:2]}***"
        return f"{name} ({tag})" if name else tag
    return name or fallback
