"""Public randomness from the drand quicknet beacon (https://drand.love).

The draw commits to a future round number before that round exists, so nobody (including
us) can know or steer the randomness when the participant snapshot is fixed."""

import asyncio
import hashlib
import logging
import math
from collections import Counter
from typing import Protocol

import httpx

log = logging.getLogger(__name__)

_UNREACHABLE = object()
_INVALID = object()  # answered, but with something that isn't a valid beacon for the round


class BeaconError(Exception):
    pass


class Beacon(Protocol):
    chain_hash: str

    def round_at(self, unix_time: float) -> int: ...

    def round_time(self, round_no: int) -> float: ...

    async def randomness(self, round_no: int) -> str: ...


class DrandBeacon:
    def __init__(
        self,
        relays: list[str],
        chain_hash: str,
        *,
        genesis: int,
        period: int,
        min_agreeing: int = 2,
        client: httpx.AsyncClient | None = None,
        poll_interval: float = 1.5,
        max_polls: int = 40,  # ~1 min; the runner retries and alerts admins
    ) -> None:
        if min_agreeing > len(relays):
            raise ValueError(f"need at least {min_agreeing} relays, got {len(relays)}")
        self.relays = relays
        self.chain_hash = chain_hash
        # Pinned, not taken from a relay: a relay lying about timing could make a "future"
        # round one that is already public.
        self.genesis = genesis
        self.period = period
        self.min_agreeing = min_agreeing
        self._client = client or httpx.AsyncClient(timeout=10)
        self._poll_interval = poll_interval
        self._max_polls = max_polls

    async def load_info(self) -> None:
        """Startup check: every reachable relay must serve this chain with the pinned timing."""
        reachable = 0
        for relay in self.relays:
            try:
                resp = await self._client.get(f"{relay}/{self.chain_hash}/info")
                resp.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("drand relay %s unreachable for info: %s", relay, e)
                continue
            info = resp.json()
            served = (info.get("hash"), int(info["genesis_time"]), int(info["period"]))
            if served != (self.chain_hash, self.genesis, self.period):
                raise BeaconError(f"relay {relay} serves {served}, expected pinned quicknet")
            reachable += 1
        if reachable < self.min_agreeing:
            raise BeaconError(f"only {reachable} drand relays reachable, need {self.min_agreeing}")

    def round_at(self, unix_time: float) -> int:
        """The round that is current at unix_time (rounds start at 1 on genesis)."""
        return math.floor((unix_time - self.genesis) / self.period) + 1

    def round_time(self, round_no: int) -> float:
        return self.genesis + (round_no - 1) * self.period

    async def aclose(self) -> None:
        await self._client.aclose()

    async def randomness(self, round_no: int) -> str:
        """Randomness hex of a round, waiting until it is published.

        A value is accepted when at least min_agreeing relays return it (each checked as
        SHA-256(signature)) and it outnumbers every other value. A relay that is down,
        broken or lying is outvoted, never trusted alone; a split vote is never resolved
        by picking one side."""
        for _ in range(self._max_polls):
            values = await asyncio.gather(*(self._fetch(r, round_no) for r in self.relays))
            votes = Counter(v for v in values if isinstance(v, str))
            if votes:
                (best, count), *rest = votes.most_common()
                runner_up = rest[0][1] if rest else 0
                if count >= self.min_agreeing and count > runner_up:
                    if rest:
                        log.warning("round %s: outvoted relay values %s", round_no, dict(rest))
                    return best
            await asyncio.sleep(self._poll_interval)
        raise BeaconError(
            f"round {round_no}: no value from {self.min_agreeing}+ agreeing relays "
            f"after {self._max_polls} polls"
        )

    async def _fetch(self, relay: str, round_no: int) -> str | object | None:
        """Randomness hex; None if not yet published; _UNREACHABLE on transport errors;
        _INVALID for a malformed, wrong-round or self-inconsistent answer."""
        try:
            resp = await self._client.get(f"{relay}/{self.chain_hash}/public/{round_no}")
        except httpx.HTTPError as e:
            log.warning("drand relay %s error: %s", relay, e)
            return _UNREACHABLE
        if resp.status_code in (404, 425):
            return None
        if resp.status_code != 200:
            log.warning("drand relay %s status %s", relay, resp.status_code)
            return _UNREACHABLE
        try:
            data = resp.json()
            randomness = data["randomness"]
            valid = int(data["round"]) == round_no and (
                hashlib.sha256(bytes.fromhex(data["signature"])).hexdigest() == randomness
            )
        except (ValueError, KeyError, TypeError) as e:
            log.warning("drand relay %s sent an unreadable answer: %s", relay, e)
            return _INVALID
        if not valid:
            log.warning("drand relay %s sent an invalid beacon for round %s", relay, round_no)
            return _INVALID
        return randomness


class FakeBeacon:
    """Test double: genesis 0, period 3, randomness set per round."""

    chain_hash = "fake"

    def __init__(self, values: dict[int, str] | None = None) -> None:
        self.values = values or {}

    def round_at(self, unix_time: float) -> int:
        return math.floor(unix_time / 3) + 1

    def round_time(self, round_no: int) -> float:
        return (round_no - 1) * 3

    async def randomness(self, round_no: int) -> str:
        if round_no not in self.values:
            raise BeaconError(f"fake round {round_no} not set")
        return self.values[round_no]
