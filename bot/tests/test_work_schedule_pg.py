from copy import deepcopy

import pytest

from aemr_bot.db.models import User
from aemr_bot.services import appeals, settings_store, work_schedule


@pytest.mark.asyncio
async def test_period_snapshots_survive_schedule_edit_and_reopen(session):
    user = User(max_user_id=910234)
    session.add(user)
    await session.flush()
    schedule = work_schedule.environment()
    schedule.update(start=8, end=19, lunch_start=12, lunch_end=13)
    await settings_store.set_value(session, work_schedule.KEY, schedule)
    first = await appeals.create_appeal(session, user, "Адрес", "Дорога", "Текст", [])
    assert first.sla_schedule_at_start == schedule
    changed = deepcopy(schedule)
    changed["end"] = 17
    await settings_store.set_value(session, work_schedule.KEY, changed)
    assert work_schedule.for_appeal(first) == schedule
    assert await appeals.close(session, first.id)
    assert await appeals.reopen(session, first.id) == "reopened"
    await session.refresh(first)
    assert first.sla_schedule_at_start == changed
    assert first.sla_history[-1]["schedule"] == schedule


@pytest.mark.asyncio
async def test_initialize_preserves_existing_db_values_and_legacy_baseline(session):
    original = work_schedule.environment()
    original["start"] = 8
    await settings_store.set_value(session, work_schedule.LEGACY_KEY, original)
    current = deepcopy(original)
    current["start"] = 7
    await settings_store.set_value(session, work_schedule.KEY, current)
    await work_schedule.initialize(session)
    assert await settings_store.get(session, work_schedule.KEY) == current
    assert await settings_store.get(session, work_schedule.LEGACY_KEY) == original


@pytest.mark.asyncio
async def test_schedule_conflict_cannot_overwrite_newer_calendar(session):
    value = work_schedule.environment()
    _, revision = await settings_store.get_versioned(session, work_schedule.KEY)
    await settings_store.set_value(session, work_schedule.KEY, value, expected_revision=revision)
    changed = deepcopy(value)
    changed["start"] = 8
    with pytest.raises(settings_store.SettingConflictError):
        await settings_store.set_value(session, work_schedule.KEY, changed, expected_revision=revision)
    assert await work_schedule.snapshot(session) == value


@pytest.mark.asyncio
async def test_two_concurrent_writers_only_one_revision_wins(session):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker
    value = work_schedule.environment()
    await settings_store.set_value(session, work_schedule.KEY, value)
    await session.commit()
    _, revision = await settings_store.get_versioned(session, work_schedule.KEY)
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    async def write(start):
        async with factory() as writer:
            try:
                await settings_store.set_value(writer, work_schedule.KEY, dict(value, start=start),
                                               expected_revision=revision)
                await writer.commit()
                return "saved"
            except settings_store.SettingConflictError:
                await writer.rollback()
                return "conflict"
    assert sorted(await asyncio.gather(write(7), write(8))) == ["conflict", "saved"]


@pytest.mark.asyncio
async def test_legacy_operational_calendar_does_not_follow_schedule_edits(session, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(work_schedule, "_active", None)
    monkeypatch.setattr(work_schedule, "_legacy", None)
    await work_schedule.initialize(session)
    baseline = await settings_store.get(session, work_schedule.LEGACY_KEY)
    await settings_store.set_value(session, work_schedule.KEY, dict(baseline, start=7))
    await work_schedule.refresh(session)
    assert work_schedule.active()["start"] == 7
    assert work_schedule.for_appeal(SimpleNamespace(sla_schedule_at_start=None)) == baseline
    # A later environment/restart cannot reconstruct a historical calendar.
    monkeypatch.setattr(work_schedule.settings, "sla_work_start_hour", 8)
    await work_schedule.initialize(session)
    assert await settings_store.get(session, work_schedule.LEGACY_KEY) == baseline


@pytest.mark.asyncio
async def test_saved_target_without_saved_calendar_remains_historically_unknown(session):
    from datetime import UTC, datetime, timedelta

    from aemr_bot.db.models import Appeal, Message
    user = User(max_user_id=910235)
    session.add(user)
    await session.flush()
    start = datetime(2026, 6, 16, 0, tzinfo=UTC)
    appeal = Appeal(user_id=user.id, status="answered", created_at=start, sla_hours_at_start=4,
                    answered_at=start + timedelta(days=1), summary="Текст", attachments=[])
    session.add(appeal)
    await session.flush()
    session.add(Message(appeal_id=appeal.id, direction="from_operator", text="Ответ",
                        created_at=start + timedelta(days=1)))
    await session.flush()
    assert await appeals.reopen(session, appeal.id) == "reopened"
    await session.refresh(appeal)
    assert appeal.sla_history[-1]["overdue"] is None
    assert appeal.sla_history[-1]["assessment"] == "at_reopen_legacy"
