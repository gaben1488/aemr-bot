"""Shared PostgreSQL fixture and delivery helper for erasure regressions."""
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from aemr_bot.db.models import Appeal, Base, Message, User
from aemr_bot.services import data_erasure


@pytest_asyncio.fixture(name="erasure_db")
async def erasure_database(monkeypatch, tmp_path):
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def scope():
        async with factory.begin() as session:
            yield session

    monkeypatch.setattr(data_erasure, "session_scope", scope)
    monkeypatch.setattr("aemr_bot.db.session.session_scope", scope)
    monkeypatch.setattr(data_erasure.settings, "erasure_state_dir", str(tmp_path / "journal"))
    monkeypatch.setattr(data_erasure.settings, "log_dir", str(tmp_path / "logs"))
    monkeypatch.setattr(data_erasure.settings, "backup_local_dir", str(tmp_path / "backups"))
    monkeypatch.setattr(data_erasure.settings, "backup_s3_bucket", None)
    (tmp_path / "backups").mkdir()
    async with scope() as session:
        resident = User(max_user_id=42, first_name="Иван", phone="+79123456789", consent_pdn_at=datetime.now(UTC))
        other = User(max_user_id=43, first_name="Анна", consent_pdn_at=datetime.now(UTC))
        session.add_all([resident, other])
        await session.flush()
        appeal = Appeal(user_id=resident.id, summary="Private", admin_message_id="first", last_admin_card_mid="last")
        unrelated = Appeal(user_id=other.id, summary="Preserve")
        session.add_all([appeal, unrelated])
        await session.flush()
        session.add(Message(appeal_id=appeal.id, direction="from_user", text="Private", max_message_id="incoming"))
        appeal_id, unrelated_id = appeal.id, unrelated.id
    yield SimpleNamespace(scope=scope, appeal_id=appeal_id, other_id=unrelated_id, path=tmp_path)
    await engine.dispose()


async def send_copy(db, *, mid="sent", kind="admin_card", appeal_id=None):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid=mid)))))
    await data_erasure.tracked_send(bot, kind=kind, appeal_id=appeal_id or db.appeal_id, text="Private")
    return bot
