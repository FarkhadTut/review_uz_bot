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
RELAYS = ["https://a.test", "https://b.test", "https://c.test"]


def beacon(handler, relays=RELAYS, max_polls=400) -> DrandBeacon:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DrandBeacon(
        relays,
        CHAIN,
        genesis=1692803367,
        period=3,
        min_agreeing=2,
        client=client,
        poll_interval=0,
        max_polls=max_polls,
    )


def round_payload(randomness=RAND, signature=SIG):
    return {"round": 1000, "randomness": randomness, "signature": signature}


async def test_info_is_checked_and_rounds_map_to_time():
    b = beacon(lambda req: httpx.Response(200, json=INFO))
    await b.load_info()
    assert b.round_at(1692803367) == 1
    assert b.round_at(1692803367 + 2.9) == 1
    assert b.round_at(1692803367 + 3) == 2
    assert b.round_time(2) == 1692803367 + 3


@pytest.mark.parametrize("field", ["genesis_time", "period"])
async def test_info_with_unexpected_timing_is_rejected(field):
    b = beacon(lambda req: httpx.Response(200, json={**INFO, field: INFO[field] + 1}))
    with pytest.raises(BeaconError):
        await b.load_info()


async def test_round_math_works_from_pinned_values_without_info():
    assert beacon(lambda req: None).round_at(1692803367 + 3) == 2


async def test_info_with_wrong_chain_hash_is_rejected():
    b = beacon(lambda req: httpx.Response(200, json={**INFO, "hash": "00" * 32}))
    with pytest.raises(BeaconError):
        await b.load_info()


async def test_randomness_is_returned_when_relays_agree():
    b = beacon(lambda req: httpx.Response(200, json=round_payload()))
    assert await b.randomness(1000) == RAND


async def test_split_vote_without_a_majority_is_never_resolved_by_picking_one():
    other_sig = "ab" * 48
    other = round_payload(hashlib.sha256(bytes.fromhex(other_sig)).hexdigest(), other_sig)
    four = [*RELAYS, "https://d.test"]

    def handler(req):
        return httpx.Response(
            200, json=other if req.url.host in ("a.test", "b.test") else round_payload()
        )

    with pytest.raises(BeaconError):
        await beacon(handler, relays=four, max_polls=2).randomness(1000)


async def test_randomness_must_be_sha256_of_signature():
    b = beacon(
        lambda req: httpx.Response(200, json=round_payload(randomness="00" * 32)), max_polls=2
    )
    with pytest.raises(BeaconError):
        await b.randomness(1000)


async def test_unpublished_round_is_polled_until_available():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(425, text="too early")
        return httpx.Response(200, content=json.dumps(round_payload()))

    b = beacon(handler, relays=RELAYS[:2])
    assert await b.randomness(1000) == RAND
    assert calls["n"] == 4  # 2 relays x (1 too-early poll + 1 published)


async def test_one_relay_down_is_fine_while_two_others_agree():
    def handler(req):
        if req.url.host == "a.test":
            raise httpx.ConnectError("down")
        return httpx.Response(200, json=round_payload())

    assert await beacon(handler).randomness(1000) == RAND


async def test_a_single_reachable_relay_is_never_trusted_alone():
    def handler(req):
        if req.url.host != "a.test":
            raise httpx.ConnectError("down")
        return httpx.Response(200, json=round_payload())

    with pytest.raises(BeaconError):
        await beacon(handler, max_polls=3).randomness(1000)


async def test_one_garbage_relay_does_not_block_two_agreeing_ones():
    def handler(req):
        if req.url.host == "a.test":
            return httpx.Response(200, text="<html>cdn error</html>")
        return httpx.Response(200, json=round_payload())

    assert await beacon(handler).randomness(1000) == RAND


async def test_one_forged_relay_is_outvoted_by_the_others():
    forged_sig = "ab" * 48
    forged = round_payload(hashlib.sha256(bytes.fromhex(forged_sig)).hexdigest(), forged_sig)

    def handler(req):
        return httpx.Response(200, json=forged if req.url.host == "a.test" else round_payload())

    assert await beacon(handler).randomness(1000) == RAND


async def test_wrong_round_or_bad_signature_from_one_relay_is_ignored():
    def handler(req):
        if req.url.host == "a.test":
            return httpx.Response(200, json={**round_payload(), "round": 999})
        if req.url.host == "b.test":
            return httpx.Response(200, json=round_payload(randomness="00" * 32))
        return httpx.Response(200, json=round_payload())

    # Only c.test is valid: one relay is never enough.
    with pytest.raises(BeaconError):
        await beacon(handler, max_polls=2).randomness(1000)
