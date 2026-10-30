from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from aemr_bot.handlers.admin_settings_shared import _intent_drop, _intent_get
from aemr_bot.services import settings_store, work_schedule


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    monkeypatch.setattr(work_schedule, "_active", None)
    monkeypatch.setattr(work_schedule, "_legacy", None)


@pytest.mark.asyncio
async def test_schedule_wizard_rejects_overlap_and_conflicting_edit(session, monkeypatch):
    from aemr_bot.handlers import admin_settings_schedule as ui
    @asynccontextmanager
    async def scope():
        yield session
    monkeypatch.setattr(ui, "session_scope", scope)
    monkeypatch.setattr(ui, "op_screen", AsyncMock())
    monkeypatch.setattr(ui, "op_send", AsyncMock())
    monkeypatch.setattr(ui.operators, "write_audit", AsyncMock())
    baseline = work_schedule.environment()
    await settings_store.set_value(session, work_schedule.KEY, baseline)
    await ui.start(object(), 89)
    assert not await ui.apply(object(), 89, "9 18 8 13")
    assert _intent_get(89) is not None
    newer = dict(baseline, end=19)
    await settings_store.set_value(session, work_schedule.KEY, newer)
    assert await ui.apply(object(), 89, "8 17 12 13")
    assert await settings_store.get(session, work_schedule.KEY) == newer
    ui.operators.write_audit.assert_not_awaited()
    _intent_drop(89)


@pytest.mark.asyncio
async def test_schedule_wizard_preserves_calendar_and_audits_change(session, monkeypatch):
    from aemr_bot.handlers import admin_settings_schedule as ui
    @asynccontextmanager
    async def scope():
        yield session
    monkeypatch.setattr(ui, "session_scope", scope)
    monkeypatch.setattr(ui, "op_screen", AsyncMock())
    monkeypatch.setattr(ui, "op_send", AsyncMock())
    monkeypatch.setattr(ui.operators, "write_audit", AsyncMock())
    baseline = dict(work_schedule.environment(), holidays=["2026-11-04"])
    await settings_store.set_value(session, work_schedule.KEY, baseline)
    await ui.start(object(), 90)
    assert await ui.apply(object(), 90, "8 17 12 13")
    saved = await settings_store.get(session, work_schedule.KEY)
    assert saved == dict(baseline, start=8, end=17)
    audit = ui.operators.write_audit.await_args.kwargs
    assert audit["operator_max_user_id"] == 90
    assert audit["details"]["before"] == baseline
    assert audit["details"]["after"] == saved
    _intent_drop(90)


@pytest.mark.asyncio
async def test_calendar_button_cannot_replace_newer_hours(session, monkeypatch):
    from aemr_bot.handlers import admin_settings_schedule as ui
    @asynccontextmanager
    async def scope():
        yield session
    monkeypatch.setattr(ui, "session_scope", scope)
    monkeypatch.setattr(ui, "op_screen", AsyncMock())
    baseline = work_schedule.environment()
    await settings_store.set_value(session, work_schedule.KEY, baseline)
    _, revision = await settings_store.get_versioned(session, work_schedule.KEY)
    changed = dict(baseline, end=19)
    await settings_store.set_value(session, work_schedule.KEY, changed)
    await ui.update_calendar(object(), 91, revision)
    assert await settings_store.get(session, work_schedule.KEY) == changed


@pytest.mark.asyncio
async def test_schedule_audit_persists_actor_and_timestamp(session, monkeypatch):
    from sqlalchemy import select

    from aemr_bot.db.models import AuditLog
    from aemr_bot.handlers import admin_settings_schedule as ui
    @asynccontextmanager
    async def scope():
        yield session
    monkeypatch.setattr(ui, "session_scope", scope)
    monkeypatch.setattr(ui, "op_screen", AsyncMock())
    await ui.start(object(), 92)
    assert await ui.apply(object(), 92, "8 17 12 13")
    await session.flush()
    audit = await session.scalar(select(AuditLog).where(AuditLog.target == work_schedule.KEY))
    assert audit.operator_max_user_id == 92
    assert audit.created_at is not None
    assert audit.details["after"]["start"] == 8
    _intent_drop(92)


@pytest.mark.asyncio
async def test_stale_cancel_cannot_drop_newer_schedule_edit(monkeypatch):
    from aemr_bot.handlers import admin_settings as facade
    from aemr_bot.handlers import admin_settings_schedule as ui
    from aemr_bot.handlers.admin_settings_shared import _intent_set
    monkeypatch.setattr(ui, "show", AsyncMock())
    _intent_set(93, key=work_schedule.KEY, kind="work_schedule", token="new-token")
    await facade._route_set_action(object(), 93, "schedule:cancel:old-token")
    assert _intent_get(93)["token"] == "new-token"
    await facade._route_set_action(object(), 93, "schedule:cancel:new-token")
    assert _intent_get(93) is None
