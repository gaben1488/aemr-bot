import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

os.environ.setdefault("BOT_TOKEN", "test-token")
# Pseudo-URL по умолчанию: pure-тесты импортируют модули с engine-
# on-import (db/session.py, services/idempotency.py) без падения.
# Используем sqlite+aiosqlite — aiosqlite есть в dev-deps локально и
# не требует asyncpg. Реальный DATABASE_URL ставится через CI env или
# локальной переменной до запуска — и перебивает этот default через
# setdefault-семантику.
_PSEUDO_DB = "sqlite+aiosqlite:///:memory:"
os.environ.setdefault("DATABASE_URL", _PSEUDO_DB)
os.environ.setdefault("ADMIN_GROUP_ID", "123")

DATABASE_URL = os.environ["DATABASE_URL"]
# Pure-юнит-тесты не требуют Postgres. Маркер «реальная БД» — postgresql
# в URL (модели используют JSONB, sqlite не подойдёт).
_HAS_REAL_DB = DATABASE_URL.startswith("postgresql")


@pytest_asyncio.fixture
async def session() -> AsyncIterator:
    if not _HAS_REAL_DB or DATABASE_URL.startswith("sqlite"):
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
