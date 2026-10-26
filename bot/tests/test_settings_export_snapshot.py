"""Settings PRs clear only values present in the successful export."""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aemr_bot.db.models import Setting
from aemr_bot.services import settings_store


@pytest.fixture
async def settings_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.execute(text(
            "CREATE TABLE settings (key VARCHAR(64) PRIMARY KEY, value JSON, "
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, synced_at DATETIME)"
        ))
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


async def test_export_marking_preserves_newer_dirty_value(settings_session):
    session = settings_session
    await settings_store.set_value(session, "topics", ["old"])
    await settings_store.set_value(session, "localities", ["unchanged"])
    snapshot = await settings_store.export_synced(session)
    await settings_store.set_value(session, "topics", ["new"])
    marked = await settings_store.mark_synced(
        session, ["topics", "localities"], exported_values=snapshot,
    )
    assert marked == 1
    assert await settings_store.get_dirty_keys(session) == ["topics"]
    assert await session.scalar(select(Setting.value).where(Setting.key == "topics")) == ["new"]


async def test_export_detaches_nested_values(monkeypatch):
    value = [{"name": "original"}]
    monkeypatch.setattr(settings_store, "get", AsyncMock(return_value=value))
    snapshot = await settings_store.export_synced(object())
    value[0]["name"] = "changed"
    assert snapshot["emergency_contacts"] == [{"name": "original"}]


async def test_pr_creation_keeps_edits_during_network_request_dirty(monkeypatch, settings_session):
    from aemr_bot.handlers import admin_settings_pr as handler
    from aemr_bot.services import repo_sync

    session = settings_session
    await settings_store.set_value(session, "topics", ["old"])

    @asynccontextmanager
    async def scope():
        yield session

    exported = []

    async def create(_config, *, runtime_config, **kwargs):
        exported.append(runtime_config)
        await settings_store.set_value(session, "topics", ["new"])
        return SimpleNamespace(ok=True, pr_number=3, pr_url="https://example.test/pr/3", branch="settings")

    monkeypatch.setattr(handler, "session_scope", scope)
    monkeypatch.setattr(handler.ops_svc, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(handler.ops_svc, "write_audit", AsyncMock())
    monkeypatch.setattr(repo_sync, "load_config_from_env_and_settings", lambda **_: SimpleNamespace(repo="test"))
    monkeypatch.setattr(repo_sync, "create_settings_pr", create)
    monkeypatch.setattr(handler, "op_screen", AsyncMock())
    await handler._create_pr(object(), 1)
    assert exported[0]["topics"] == ["old"]
    assert await settings_store.get_dirty_keys(session) == ["topics"]


@pytest.mark.parametrize("value", [None, "text", 42, False, [{"name": "Fire", "phone": "01"}]])
async def test_conditional_export_marks_equal_json_values(settings_session, value):
    await settings_store.set_value(settings_session, "topics", value)
    assert await settings_store.mark_synced(
        settings_session, ["topics"], exported_values={"topics": value},
    ) == 1
    assert await settings_store.get_dirty_keys(settings_session) == []


async def test_conditional_export_does_not_acknowledge_unexported_key(settings_session):
    await settings_store.set_value(settings_session, "topics", ["new"])
    assert await settings_store.mark_synced(
        settings_session, ["topics"], exported_values={},
    ) == 0
    assert await settings_store.get_dirty_keys(settings_session) == ["topics"]
