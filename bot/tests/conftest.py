import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

# Tests use SQLite in-memory shared via aiosqlite for speed.
os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ADMIN_GROUP_ID", "123")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aemr_bot.db.models import Base


@pytest_asyncio.fixture
async def session() -> AsyncIterator:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    # On SQLite our JSONB columns are emulated via JSON; schema creation is enough for unit tests.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
        await s.rollback()
    await engine.dispose()
