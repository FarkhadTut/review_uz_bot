import importlib.util
from pathlib import Path

from giveaway_bot.domain.draw import snapshot_hash, winner_index

spec = importlib.util.spec_from_file_location(
    "verify_draw", Path(__file__).parents[1] / "tools" / "verify_draw.py"
)
verify_draw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_draw)

CODES = ["K7M2Q9XA", "0ABCDEFG", "ZZZZ1111", "M00N0000"]


def test_script_reproduces_the_bots_result(tmp_path, capsys):
    f = tmp_path / "draw-1.txt"
    f.write_text("\n".join(sorted(CODES)), encoding="utf-8")
    h, rand = snapshot_hash(CODES), "12" * 32

    rc = verify_draw.main([str(f), "--hash", h, "--round", "5", "--randomness", rand])

    expected = sorted(CODES)[winner_index(rand, h, len(CODES))]
    assert rc == 0 and f"entry code {expected}" in capsys.readouterr().out


def test_script_flags_a_tampered_list(tmp_path):
    f = tmp_path / "draw-1.txt"
    f.write_text("\n".join(sorted(CODES[:-1])), encoding="utf-8")
    assert (
        verify_draw.main(
            [str(f), "--hash", snapshot_hash(CODES), "--round", "5", "--randomness", "12" * 32]
        )
        == 1
    )
