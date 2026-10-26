import pytest

from giveaway_bot.singleton import AlreadyRunning, single_instance


def test_a_second_instance_on_the_same_data_dir_is_refused(tmp_path):
    first = single_instance(tmp_path / "bot.lock")
    with pytest.raises(AlreadyRunning):
        single_instance(tmp_path / "bot.lock")
    first.release()
    single_instance(tmp_path / "bot.lock").release()  # free again after release
