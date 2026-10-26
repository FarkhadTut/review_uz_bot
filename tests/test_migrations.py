from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError


def test_migrations_build_exactly_the_models_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "m.db"))
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    try:
        command.check(cfg)  # raises if the models need a migration that doesn't exist
    except CommandError as e:  # pragma: no cover - the failure message is the point
        raise AssertionError(f"models and migrations differ: {e}") from e
    command.downgrade(cfg, "base")
