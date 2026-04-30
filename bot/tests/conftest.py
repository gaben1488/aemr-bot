import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("ADMIN_GROUP_ID", "123")

DATABASE_URL = os.environ["DATABASE_URL"]

# Models use PostgreSQL JSONB; SQLite cannot create the schema. Skip the suite
# locally if the developer has no real Postgres reachable. CI sets DATABASE_URL
# to a postgres:// URL via a service container.
if not DATABASE_URL or DATABASE_URL.startswith("sqlite"):
    pytest.skip(
        "Tests require PostgreSQL (models use JSONB). "
        "Set DATABASE_URL=postgresql+asyncpg://... before running pytest.",
        allow_module_level=True,
    )

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from aemr_bot.db.models import Base  # noqa: E402


@pytest_asyncio.fixture
async def session() -> AsyncIterator:
    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
        await s.rollback()
    await engine.dispose()
