"""Ordered best-effort writes must not resurrect a cancelled wizard."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from aemr_bot.services import wizard_registry as wr


@pytest.fixture
def writes(monkeypatch):
    tasks = []
    real_spawn = wr.spawn_background_task

    def spawn(coro, *, name=None):
        task = real_spawn(coro, name=name)
        tasks.append(task)
        return task

    @asynccontextmanager
    async def scope():
        yield object()

    monkeypatch.setattr(wr, "spawn_background_task", spawn)
    monkeypatch.setattr(wr, "session_scope", scope)
    return tasks


@pytest.mark.parametrize("kind", ["op", "broadcast"])
async def test_delete_waits_for_older_save_commit(monkeypatch, writes, kind):
    started, release = asyncio.Event(), asyncio.Event()
    stored = {}
    calls = []

    async def save(_session, operator_id, state):
        started.set()
        await release.wait()
        stored[operator_id] = state
        calls.append("save")

    async def delete(_session, operator_id):
        stored.pop(operator_id, None)
        calls.append("delete")

    monkeypatch.setattr(wr.wizard_persist, f"save_{kind}_wizard", save)
    monkeypatch.setattr(wr.wizard_persist, f"delete_{kind}_wizard", delete)
    schedule = getattr(wr, f"schedule_persist_{kind}")
    schedule(901, {"step": "draft"})
    await started.wait()
    schedule(901)
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*writes)
    assert calls == ["save", "delete"]
    assert stored == {}


@pytest.mark.parametrize("kind", ["op", "broadcast"])
async def test_nested_snapshot_is_detached_before_task_starts(monkeypatch, writes, kind):
    saved = []

    async def save(_session, _operator_id, state):
        saved.append(state)

    monkeypatch.setattr(wr.wizard_persist, f"save_{kind}_wizard", save)
    state = {"attachments": [{"token": "original"}]}
    getattr(wr, f"schedule_persist_{kind}")(902, state)
    state["attachments"][0]["token"] = "changed"
    await asyncio.gather(*writes)
    assert saved == [{"attachments": [{"token": "original"}]}]


async def test_different_keys_progress_while_one_save_is_blocked(monkeypatch, writes):
    started, release, independent = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def save(_session, _operator_id, _state):
        started.set()
        await release.wait()

    async def delete(_session, _operator_id):
        independent.set()

    monkeypatch.setattr(wr.wizard_persist, "save_op_wizard", save)
    monkeypatch.setattr(wr.wizard_persist, "delete_op_wizard", delete)
    monkeypatch.setattr(wr.wizard_persist, "delete_broadcast_wizard", delete)
    wr.schedule_persist_op(903, {"step": "draft"})
    await started.wait()
    wr.schedule_persist_op(904)
    wr.schedule_persist_broadcast(903)
    try:
        await asyncio.wait_for(independent.wait(), timeout=1)
    finally:
        release.set()
        await asyncio.gather(*writes)


async def test_failed_save_does_not_block_later_delete(monkeypatch, writes, caplog):
    calls = []

    async def save(_session, _operator_id, _state):
        calls.append("save")
        raise RuntimeError("database unavailable")

    async def delete(_session, _operator_id):
        calls.append("delete")

    monkeypatch.setattr(wr.wizard_persist, "save_op_wizard", save)
    monkeypatch.setattr(wr.wizard_persist, "delete_op_wizard", delete)
    wr.schedule_persist_op(905, {"step": "draft"})
    wr.schedule_persist_op(905)
    await asyncio.gather(*writes)
    assert calls == ["save", "delete"]
    assert "failed" in caplog.text
    assert ("op", 905) not in wr._persist_queues


@pytest.mark.parametrize("start_worker", [False, True])
async def test_cancelled_worker_discards_queue_and_releases_key(monkeypatch, writes, caplog, start_worker):
    started = asyncio.Event()
    calls = []

    async def save(_session, _operator_id, _state):
        started.set()
        await asyncio.Event().wait()
        calls.append("stale save")

    async def delete(_session, _operator_id):
        calls.append("delete")

    monkeypatch.setattr(wr.wizard_persist, "save_op_wizard", save)
    monkeypatch.setattr(wr.wizard_persist, "delete_op_wizard", delete)
    wr.schedule_persist_op(906, {"step": "draft"})
    if start_worker:
        await started.wait()
    wr.schedule_persist_op(906)
    writes[0].cancel()
    await asyncio.gather(*writes, return_exceptions=True)
    assert calls == []
    assert ("op", 906) not in wr._persist_queues
    assert "cancelled" in caplog.text
    wr.schedule_persist_op(906)
    await asyncio.gather(*writes, return_exceptions=True)
    assert calls == ["delete"]


async def test_spawn_failure_closes_worker_and_allows_retry(monkeypatch, writes, caplog):
    original_spawn = wr.spawn_background_task
    failed_coroutines = []

    def fail(coro, *, name=None):
        failed_coroutines.append(coro)
        raise RuntimeError("cannot spawn")

    deleted = []

    async def delete(_session, operator_id):
        deleted.append(operator_id)

    monkeypatch.setattr(wr.wizard_persist, "delete_op_wizard", delete)
    monkeypatch.setattr(wr, "spawn_background_task", fail)
    wr.schedule_persist_op(907)
    assert failed_coroutines[0].cr_frame is None
    assert ("op", 907) not in wr._persist_queues
    assert "spawn failed" in caplog.text
    monkeypatch.setattr(wr, "spawn_background_task", original_spawn)
    wr.schedule_persist_op(907)
    await asyncio.gather(*writes)
    assert deleted == [907]


async def test_queue_waits_for_session_commit_before_next_write(monkeypatch, writes):
    committing, release = asyncio.Event(), asyncio.Event()
    calls = []
    first = True

    @asynccontextmanager
    async def scope():
        nonlocal first
        initial = first
        first = False
        yield object()
        if initial:
            committing.set()
            await release.wait()
        calls.append("commit")

    async def save(_session, _operator_id, _state):
        calls.append("save")

    async def delete(_session, _operator_id):
        calls.append("delete")

    monkeypatch.setattr(wr, "session_scope", scope)
    monkeypatch.setattr(wr.wizard_persist, "save_op_wizard", save)
    monkeypatch.setattr(wr.wizard_persist, "delete_op_wizard", delete)
    wr.schedule_persist_op(908, {"step": "draft"})
    await committing.wait()
    wr.schedule_persist_op(908)
    wr.schedule_persist_op(908, {"step": "new draft"})
    await asyncio.sleep(0)
    assert calls == ["save"]
    release.set()
    await asyncio.gather(*writes)
    assert calls == ["save", "commit", "delete", "commit", "save", "commit"]
