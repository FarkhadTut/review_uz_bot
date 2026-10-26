import hashlib
import json

import httpx
import pytest

from giveaway_bot.ports.beacon import BeaconError, DrandBeacon

CHAIN = "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971"
INFO = {"period": 3, "genesis_time": 1692803367, "hash": CHAIN}
SIG = (
    "b44679b9a59af2ec876b1a6b1ad52ea9b1615fc398"
    "2b19576350f93447cb1125e342b73a8dd2bacbe47e4b6b63ed5e39"
)
RAND = "fe290beca10872ef2fb164d2aa4442de4566183ec51c56ff3cd603d930e54fdd"
RELAYS = ["https://a.test", "https://b.test"]


def beacon(handler) -> DrandBeacon:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DrandBeacon(RELAYS, CHAIN, client=client, poll_interval=0)


def round_payload(randomness=RAND, signature=SIG):
    return {"round": 1000, "randomness": randomness, "signature": signature}


async def test_info_is_checked_and_rounds_map_to_time():
    b = beacon(lambda req: httpx.Response(200, json=INFO))
    await b.load_info()
    assert b.round_at(1692803367) == 1
    assert b.round_at(1692803367 + 2.9) == 1
    assert b.round_at(1692803367 + 3) == 2
    assert b.round_time(2) == 1692803367 + 3


async def test_info_with_wrong_chain_hash_is_rejected():
    b = beacon(lambda req: httpx.Response(200, json={**INFO, "hash": "00" * 32}))
    with pytest.raises(BeaconError):
        await b.load_info()


async def test_randomness_requires_all_relays_to_agree():
    b = beacon(lambda req: httpx.Response(200, json=round_payload()))
    assert await b.randomness(1000) == RAND


async def test_relay_disagreement_is_an_error():
    def handler(req):
        if req.url.host == "a.test":
            return httpx.Response(200, json=round_payload())
        other_sig = "ab" * 48
        return httpx.Response(
            200,
            json=round_payload(hashlib.sha256(bytes.fromhex(other_sig)).hexdigest(), other_sig),
        )

    with pytest.raises(BeaconError):
        await beacon(handler).randomness(1000)


async def test_randomness_must_be_sha256_of_signature():
    b = beacon(lambda req: httpx.Response(200, json=round_payload(randomness="00" * 32)))
    with pytest.raises(BeaconError):
        await b.randomness(1000)


async def test_unpublished_round_is_polled_until_available():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(425, text="too early")
        return httpx.Response(200, content=json.dumps(round_payload()))

    b = beacon(handler)
    b.relays = RELAYS[:1]
    assert await b.randomness(1000) == RAND
    assert calls["n"] == 3


async def test_one_relay_down_still_works_with_the_other():
    def handler(req):
        if req.url.host == "a.test":
            raise httpx.ConnectError("down")
        return httpx.Response(200, json=round_payload())

    assert await beacon(handler).randomness(1000) == RAND
