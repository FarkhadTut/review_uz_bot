from giveaway_bot.domain.draw import snapshot_hash, winner_index

CODES = ["K7M2Q9XA", "0ABCDEFG", "ZZZZ1111"]
HASH = "44d4b9f16271e93ce765cdb9887d6922cbae5c48e4d28c9253e755af3fa24162"


def test_snapshot_hash_is_sha256_of_sorted_codes_joined_by_newline():
    assert snapshot_hash(CODES) == HASH


def test_snapshot_hash_does_not_depend_on_input_order():
    assert snapshot_hash(list(reversed(CODES))) == HASH


def test_winner_index_matches_published_formula():
    # SHA256(bytes(randomness) || bytes(snapshot_hash)) as big-endian int, mod N
    assert winner_index("aa" * 32, HASH, 3) == 0
    assert winner_index("aa" * 32, HASH, 1000) == 453


def test_candidate_order_walks_forward_from_drawn_index_and_wraps_once():
    from giveaway_bot.domain.draw import candidate_order

    assert list(candidate_order(2, 4)) == [2, 3, 0, 1]
    assert list(candidate_order(0, 1)) == [0]


def test_mask_name_keeps_first_name_and_hides_most_of_username():
    from giveaway_bot.domain.draw import mask_name

    assert mask_name("Aziz", "azizbek_t") == "Aziz (@az***)"
    assert mask_name("Aziz", None) == "Aziz"
    assert mask_name("Aziz", "a") == "Aziz (@a***)"
    assert mask_name("", None, fallback="#K7M2") == "#K7M2"
