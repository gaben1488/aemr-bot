"""Restart cancellation and bounded persistence during a stalled DB commit."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from aemr_bot import main
from aemr_bot.handlers import admin_operators_wizard as op_wizard
from aemr_bot.services import wizard_persist as wp
from aemr_bot.services import wizard_registry as wr
from aemr_bot.utils.background import drain_background_tasks


@pytest.fixture
def persistence(monkeypatch):
    stored = {}
    calls = []
    tasks = []
    real_spawn = wr.spawn_background_task

    @asynccontextmanager
    async def scope():
        yield object()

    def spawn(coro, *, name=None):
        task = real_spawn(coro, name=name)
        tasks.append(task)
        return task

    async def save(_session, operator_id, state):
        stored[operator_id] = state
        calls.append(("save", state))

    async def delete(_session, operator_id):
        stored.pop(operator_id, None)
        calls.append(("delete", None))

    wr.reset_all()
    op_wizard._op_wizards.clear()
    monkeypatch.setattr(wr, "session_scope", scope)
    monkeypatch.setattr(wr, "spawn_background_task", spawn)
    for kind in ("op", "broadcast"):
        monkeypatch.setattr(wp, f"save_{kind}_wizard", save)
        monkeypatch.setattr(wp, f"delete_{kind}_wizard", delete)
    yield SimpleNamespace(stored=stored, calls=calls, tasks=tasks)
    op_wizard._op_wizards.clear()
    wr.reset_all()


@pytest.mark.parametrize("action", ["cancel", "expiry"])
async def test_hydrated_operator_registration_stays_deleted_after_restart(
    monkeypatch, persistence, action,
):
    persistence.stored[971] = {
        "step": "awaiting_name", "target_id": 42, "role": "it",
    }

    class HydrationSession:
        async def execute(self, _statement):
            return SimpleNamespace(rowcount=0, all=list)

        async def scalars(self, _statement):
            rows = [SimpleNamespace(
                id=operator_id, kind="op", operator_max_user_id=operator_id,
                state=state,
            ) for operator_id, state in persistence.stored.items()]
            return SimpleNamespace(all=lambda: rows)

    @asynccontextmanager
    async def scope():
        yield HydrationSession()

    monkeypatch.setattr(main, "session_scope", scope)
    await main._hydrate_wizards()
    assert op_wizard._op_wizard_get(971)["target_id"] == 42
    if action == "cancel":
        op_wizard._op_wizard_drop(971)
    else:
        op_wizard._op_wizards[971]["expires_at"] = -1
        assert op_wizard._op_wizard_get(971) is None
    await asyncio.gather(*persistence.tasks)
    assert persistence.stored == {}
    assert wr.get_op_wizard(971) is None

    wr.reset_all()
    op_wizard._op_wizards.clear()
    await main._hydrate_wizards()
    assert op_wizard._op_wizard_get(971) is None


@pytest.mark.parametrize("kind", ["op", "broadcast"])
async def test_explicit_none_deletes_even_when_registry_has_a_snapshot(persistence, kind):
    getattr(wr, f"set_{kind}_wizard")(972, {"step": "stale"})
    getattr(wr, f"schedule_persist_{kind}")(972, None)
    await asyncio.gather(*persistence.tasks)
    assert persistence.calls == [("delete", None)]
    assert getattr(wr, f"get_{kind}_wizard")(972) is None


@pytest.mark.parametrize("kind", ["op", "broadcast"])
@pytest.mark.parametrize("ending", ["save", "cancel", "failed_recreate"])
async def test_blocked_commit_bounds_snapshots_and_preserves_latest_delete(
    monkeypatch, persistence, kind, ending,
):
    committing, release = asyncio.Event(), asyncio.Event()
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

    original_save = getattr(wp, f"save_{kind}_wizard")

    async def save(session, operator_id, state):
        if ending == "failed_recreate" and state["step"] == "latest":
            persistence.calls.append(("failed_save", state))
            raise RuntimeError("new draft could not be saved")
        await original_save(session, operator_id, state)

    monkeypatch.setattr(wr, "session_scope", scope)
    monkeypatch.setattr(wp, f"save_{kind}_wizard", save)
    schedule = getattr(wr, f"schedule_persist_{kind}")
    schedule(973, {"step": "first"})
    await committing.wait()
    try:
        for index in range(1000):
            schedule(973, {"step": "editing", "revision": index})
            if index % 10 == 0:
                schedule(973, None)
        schedule(973, None)
        if ending != "cancel":
            schedule(973, {"step": "latest"})

        # Retain at most an oldest write, the latest delete barrier, and
        # the newest complete snapshot, irrespective of update count.
        assert len(wr._persist_queues[(kind, 973)][0]) <= 3
        assert len(persistence.tasks) == 1
    finally:
        release.set()
        await asyncio.gather(*persistence.tasks)

    assert (kind, 973) not in wr._persist_queues
    if ending == "save":
        assert persistence.stored == {973: {"step": "latest"}}
        assert persistence.calls[-2:] == [
            ("delete", None), ("save", {"step": "latest"}),
        ]
    else:
        assert persistence.stored == {}
        assert ("delete", None) in persistence.calls


async def test_shutdown_drain_waits_for_coalesced_delete(monkeypatch, persistence):
    committing, release = asyncio.Event(), asyncio.Event()
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

    monkeypatch.setattr(wr, "session_scope", scope)
    wr.schedule_persist_op(974, {"step": "first"})
    await committing.wait()
    for index in range(100):
        wr.schedule_persist_op(974, {"revision": index})
    wr.schedule_persist_op(974, None)
    drain = asyncio.create_task(drain_background_tasks(timeout=1.0))
    try:
        await asyncio.sleep(0)
        assert not drain.done()
    finally:
        release.set()
    assert await drain == []
    assert persistence.stored == {}
    assert persistence.calls[-1] == ("delete", None)
    assert ("op", 974) not in wr._persist_queues
    assert len(persistence.tasks) == 1
