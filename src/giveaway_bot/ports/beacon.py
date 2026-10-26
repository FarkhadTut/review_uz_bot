"""Public randomness from the drand quicknet beacon (https://drand.love).

The draw commits to a future round number before that round exists, so nobody (including
us) can know or steer the randomness when the participant snapshot is fixed."""

import asyncio
import hashlib
import logging
import math
from typing import Protocol

import httpx

log = logging.getLogger(__name__)

_UNREACHABLE = object()


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
        client: httpx.AsyncClient | None = None,
        poll_interval: float = 1.5,
        max_polls: int = 400,
    ) -> None:
        self.relays = relays
        self.chain_hash = chain_hash
        self._client = client or httpx.AsyncClient(timeout=10)
        self._poll_interval = poll_interval
        self._max_polls = max_polls
        self.genesis: int | None = None
        self.period: int | None = None

    async def load_info(self) -> None:
        """Read genesis/period from the first reachable relay and confirm the chain hash."""
        for relay in self.relays:
            try:
                resp = await self._client.get(f"{relay}/{self.chain_hash}/info")
                resp.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("drand relay %s unreachable for info: %s", relay, e)
                continue
            info = resp.json()
            if info.get("hash") != self.chain_hash:
                raise BeaconError(f"relay {relay} serves chain {info.get('hash')}")
            self.genesis, self.period = int(info["genesis_time"]), int(info["period"])
            return
        raise BeaconError("no drand relay reachable")

    def round_at(self, unix_time: float) -> int:
        """The round that is current at unix_time (rounds start at 1 on genesis)."""
        assert self.genesis is not None and self.period is not None, "call load_info() first"
        return math.floor((unix_time - self.genesis) / self.period) + 1

    def round_time(self, round_no: int) -> float:
        assert self.genesis is not None and self.period is not None, "call load_info() first"
        return self.genesis + (round_no - 1) * self.period

    async def randomness(self, round_no: int) -> str:
        """Randomness hex of a round, waiting until it is published.

        Every reachable relay must return the same value and the value must equal
        SHA-256(signature); a disagreement is never resolved by picking one."""
        for _ in range(self._max_polls):
            values = await asyncio.gather(*(self._fetch(r, round_no) for r in self.relays))
            published = {v for v in values if isinstance(v, str)}
            if len(published) > 1:
                raise BeaconError(f"relays disagree on round {round_no}: {published}")
            # Accept once no reachable relay still reports "not yet published".
            if published and None not in values:
                return published.pop()
            await asyncio.sleep(self._poll_interval)
        raise BeaconError(f"round {round_no} not available after {self._max_polls} polls")

    async def _fetch(self, relay: str, round_no: int) -> str | object | None:
        """Randomness hex; None if not yet published; _UNREACHABLE on transport errors."""
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
        data = resp.json()
        if int(data["round"]) != round_no:
            raise BeaconError(f"relay {relay} returned round {data['round']} for {round_no}")
        randomness = data["randomness"]
        if hashlib.sha256(bytes.fromhex(data["signature"])).hexdigest() != randomness:
            raise BeaconError(f"relay {relay}: randomness is not SHA-256(signature)")
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
