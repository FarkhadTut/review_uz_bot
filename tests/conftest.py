from datetime import datetime, timedelta

import pytest

from giveaway_bot.db.models import Base
from giveaway_bot.db.session import make_engine, make_sessionmaker


@pytest.fixture
async def sessionmaker(tmp_path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(engine)
    await engine.dispose()


class Clock:
    def __init__(self, start: datetime = datetime(2026, 11, 2, 9, 0)) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()
