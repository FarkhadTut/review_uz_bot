import pytest

from giveaway_bot.config import Settings
from giveaway_bot.ports.beacon import DrandBeacon


@pytest.mark.live
async def test_real_quicknet_round_1000_from_both_relays():
    s = Settings(bot_token="1:x")
    beacon = DrandBeacon(s.drand_relays, s.drand_chain_hash)
    await beacon.load_info()
    assert beacon.period == 3
    assert await beacon.randomness(1000) == (
        "fe290beca10872ef2fb164d2aa4442de4566183ec51c56ff3cd603d930e54fdd"
    )
