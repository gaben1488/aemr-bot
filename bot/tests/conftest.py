import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("ADMIN_GROUP_ID", "123")

DATABASE_URL = os.environ["DATABASE_URL"]

# Pure-юнит-тесты (нормализация телефона, валидация settings и т.п.) не
# требуют Postgres и должны проходить локально даже без поднятой БД.
# Скип целевой только для DB-фикстуры — иначе скипалась бы вся сюита.


@pytest_asyncio.fixture
async def session() -> AsyncIterator:
    if not DATABASE_URL or DATABASE_URL.startswith("sqlite"):
        pytest.skip(
            "Test requires PostgreSQL (models use JSONB). "
            "Set DATABASE_URL=postgresql+asyncpg://... before running pytest."
        )
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from aemr_bot.db.models import Base

    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
        await s.rollback()
    await engine.dispose()
