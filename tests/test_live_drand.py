import pytest

from giveaway_bot.config import Settings
from giveaway_bot.ports.beacon import DrandBeacon


@pytest.mark.live
async def test_real_quicknet_round_1000_agreed_by_the_default_relays():
    s = Settings(_env_file=None, bot_token="1:x")
    beacon = DrandBeacon(
        s.drand_relays,
        s.drand_chain_hash,
        genesis=s.drand_genesis,
        period=s.drand_period,
        min_agreeing=s.drand_min_agreeing,
    )
    await beacon.load_info()  # every reachable relay serves the pinned chain
    assert await beacon.randomness(1000) == (
        "fe290beca10872ef2fb164d2aa4442de4566183ec51c56ff3cd603d930e54fdd"
    )
    await beacon.aclose()
