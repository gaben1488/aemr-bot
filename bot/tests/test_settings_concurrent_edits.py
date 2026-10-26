"""Concurrent settings mutations must never silently overwrite another operator."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aemr_bot.services import settings_store


@pytest.fixture
async def settings_sessions(tmp_path):
    # Separate sessions and connections reproduce two operator transactions.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    async with engine.begin() as connection:
        await connection.execute(text(
            "CREATE TABLE settings (key VARCHAR(64) PRIMARY KEY, value JSON, "
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, synced_at DATETIME)"
        ))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("module_name", "key", "initial", "first_text", "second_text", "first_item", "second_item"),
    [
        (
            "admin_settings_list", "topics", ["ЖКХ"], "Дороги", "Транспорт",
            "Дороги", "Транспорт",
        ),
        (
            "admin_settings_obj", "emergency_contacts", [{"name": "Пожарные", "phone": "01"}],
            "Полиция\n02", "Скорая\n03",
            {"name": "Полиция", "phone": "02"}, {"name": "Скорая", "phone": "03"},
        ),
    ],
)
async def test_concurrent_add_rejects_stale_write_and_can_retry(
    monkeypatch, settings_sessions, module_name, key, initial,
    first_text, second_text, first_item, second_item,
):
    from importlib import import_module

    handler = import_module(f"aemr_bot.handlers.{module_name}")
    is_object = module_name.endswith("_obj")
    apply_add = handler._apply_obj_add if is_object else handler._apply_list_add
    async with settings_sessions() as session:
        await settings_store.set_value(session, key, initial)
        await session.commit()

    both_read = asyncio.Event()
    first_committed = asyncio.Event()
    original_get = settings_store.get
    reads = 0

    async def racing_get(session, setting_key):
        nonlocal reads
        snapshot = await original_get(session, setting_key)
        reads += 1
        if reads == 2:
            both_read.set()
        await asyncio.wait_for(both_read.wait(), timeout=5)
        if asyncio.current_task().get_name() == "second-operator":
            await asyncio.wait_for(first_committed.wait(), timeout=5)
        return snapshot

    @asynccontextmanager
    async def scope():
        async with settings_sessions() as session:
            yield session
            await session.commit()
        if asyncio.current_task().get_name() == "first-operator":
            first_committed.set()

    audit = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(handler, "session_scope", scope)
    monkeypatch.setattr(settings_store, "get", racing_get)
    monkeypatch.setattr(handler.ops_svc, "write_audit", audit)
    monkeypatch.setattr(handler, "op_send", send)
    monkeypatch.setattr(handler, "_show_obj_card" if is_object else "_show_list_card", AsyncMock())

    first = asyncio.create_task(apply_add(object(), 1, key, first_text), name="first-operator")
    second = asyncio.create_task(apply_add(object(), 2, key, second_text), name="second-operator")
    assert await asyncio.gather(first, second) == [True, False]
    audit.assert_awaited_once()
    assert "изменён" in send.await_args.args[1]
    async with settings_sessions() as session:
        assert await original_get(session, key) == initial + [first_item]

    # The rejected addition remains actionable and merges into a fresh snapshot.
    monkeypatch.setattr(settings_store, "get", original_get)
    assert await apply_add(object(), 2, key, second_text) is True
    async with settings_sessions() as session:
        assert await original_get(session, key) == initial + [first_item, second_item]


def _action_suffix(markup, action):
    prefix = f"op:set:{action}:"
    return [
        button.payload.removeprefix(prefix)
        for row in markup.payload.buttons
        for button in row
        if getattr(button, "payload", "").startswith(prefix)
    ]


@pytest.mark.parametrize("kind", ["list", "obj"])
@pytest.mark.parametrize("legacy_button", [False, True])
async def test_stale_delete_refreshes_without_deleting_shifted_entry(
    monkeypatch, settings_sessions, kind, legacy_button,
):
    from importlib import import_module

    from aemr_bot.ui import settings_keyboards as keyboards

    handler = import_module(f"aemr_bot.handlers.admin_settings_{kind}")
    key = "topics" if kind == "list" else "emergency_contacts"
    initial = ["A", "B", "C"] if kind == "list" else [
        {"name": name, "phone": "01"} for name in ("A", "B", "C")
    ]
    # The second button meant B. After A disappears, index 1 means C.
    keyboard = getattr(keyboards, f"op_settings_{kind}_keyboard")(key, initial)
    if kind == "list":
        suffix = _action_suffix(keyboard, "list_del")[1]
    else:
        suffix = _action_suffix(keyboard, "obj_view")[1]
        revision = suffix.rsplit(":", 1)[1]
        keyboard = keyboards.op_settings_obj_item_keyboard(key, 1, revision)
        suffix = _action_suffix(keyboard, "obj_del")[0]
    if legacy_button:
        suffix = f"{key}:1"

    async with settings_sessions() as session:
        await settings_store.set_value(session, key, initial[1:])
        await session.commit()

    @asynccontextmanager
    async def scope():
        async with settings_sessions() as session:
            yield session
            await session.commit()

    show = AsyncMock()
    audit = AsyncMock()
    monkeypatch.setattr(handler, "session_scope", scope)
    monkeypatch.setattr(handler, f"_show_{kind}_card", show)
    monkeypatch.setattr(handler.ops_svc, "write_audit", audit)
    await getattr(handler, f"_{kind}_delete")(object(), 7, suffix)
    async with settings_sessions() as session:
        assert await settings_store.get(session, key) == initial[1:]
    audit.assert_not_awaited()
    assert "изменён" in show.await_args.kwargs["notice"]


@pytest.mark.parametrize("kind", ["list", "obj"])
async def test_current_button_deletes_the_displayed_entry(monkeypatch, settings_sessions, kind):
    from importlib import import_module

    handler = import_module(f"aemr_bot.handlers.admin_settings_{kind}")
    key = "topics" if kind == "list" else "emergency_contacts"
    initial = ["A", "B", "C"] if kind == "list" else [
        {"name": name, "phone": "01"} for name in ("A", "B", "C")
    ]
    async with settings_sessions() as session:
        await settings_store.set_value(session, key, initial)
        await session.commit()

    @asynccontextmanager
    async def scope():
        async with settings_sessions() as session:
            yield session
            await session.commit()

    audit = AsyncMock()
    monkeypatch.setattr(handler, "session_scope", scope)
    monkeypatch.setattr(handler, f"_show_{kind}_card", AsyncMock())
    monkeypatch.setattr(handler.ops_svc, "write_audit", audit)
    suffix = f"{key}:1:{settings_store.value_revision(initial)}"
    await getattr(handler, f"_{kind}_delete")(object(), 7, suffix)
    async with settings_sessions() as session:
        assert await settings_store.get(session, key) == [initial[0], initial[2]]
    assert audit.await_args.kwargs["details"]["removed"] == initial[1]


async def test_stale_object_view_refreshes_before_offering_delete(monkeypatch, settings_sessions):
    from aemr_bot.handlers import admin_settings_obj as handler
    from aemr_bot.ui import settings_keyboards as keyboards

    initial = [{"name": name, "phone": "01"} for name in ("A", "B", "C")]
    suffix = _action_suffix(keyboards.op_settings_obj_keyboard("emergency_contacts", initial), "obj_view")[1]
    async with settings_sessions() as session:
        await settings_store.set_value(session, "emergency_contacts", initial[1:])
        await session.commit()

    @asynccontextmanager
    async def scope():
        async with settings_sessions() as session:
            yield session

    show = AsyncMock()
    screen = AsyncMock()
    monkeypatch.setattr(handler, "session_scope", scope)
    monkeypatch.setattr(handler, "_show_obj_card", show)
    monkeypatch.setattr(handler, "op_screen", screen)
    await handler._show_obj_item(object(), suffix)
    screen.assert_not_awaited()
    assert "изменён" in show.await_args.kwargs["notice"]


@pytest.mark.parametrize("previous", [None, False, 0, "text", [], [{"name": "A", "phone": "01"}]])
async def test_cas_compares_json_value_and_preserves_newer_synced_marker(settings_sessions, previous):
    async with settings_sessions() as session:
        await settings_store.set_value(session, "topics", previous)
        await settings_store.set_value(session, "topics", ["first"], expected_value=previous)
        await settings_store.mark_synced(session, ["topics"])
        with pytest.raises(settings_store.SettingConflictError):
            await settings_store.set_value(session, "topics", ["second"], expected_value=previous)
        assert await settings_store.get(session, "topics") == ["first"]
        assert await settings_store.get_dirty_keys(session) == []


async def test_cas_inserts_missing_default_then_rejects_stale_default(settings_sessions):
    async with settings_sessions() as session:
        previous = await settings_store.get(session, "topics")
        await settings_store.set_value(session, "topics", ["first"], expected_value=previous)
        with pytest.raises(settings_store.SettingConflictError):
            await settings_store.set_value(session, "topics", ["second"], expected_value=previous)
        assert await settings_store.get(session, "topics") == ["first"]
        assert await settings_store.get_dirty_keys(session) == ["topics"]


async def test_postgresql_cas_rechecks_after_writer_commit(session):
    """Real PostgreSQL checks CAS against the winner after waiting on its row lock."""
    await settings_store.set_value(session, "topics", ["original"])
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    async with factory() as first, factory() as second:
        first_value = await settings_store.get(first, "topics")
        second_value = await settings_store.get(second, "topics")
        await settings_store.set_value(first, "topics", ["first"], expected_value=first_value)
        started = asyncio.Event()

        async def competing_write():
            started.set()
            with pytest.raises(settings_store.SettingConflictError):
                await settings_store.set_value(second, "topics", ["second"], expected_value=second_value)
            await second.rollback()

        task = asyncio.create_task(competing_write())
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            done, _ = await asyncio.wait({task}, timeout=0.05)
            assert not done, "The competing upsert must wait for the first transaction"
            await first.commit()
            await asyncio.wait_for(task, timeout=5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert await settings_store.get(first, "topics") == ["first"]


@pytest.mark.parametrize("kind", ["list", "obj"])
async def test_delete_preserves_add_committed_after_its_read(monkeypatch, settings_sessions, kind):
    from importlib import import_module

    handler = import_module(f"aemr_bot.handlers.admin_settings_{kind}")
    key = "topics" if kind == "list" else "emergency_contacts"
    initial = ["A", "B", "C"] if kind == "list" else [
        {"name": name, "phone": "01"} for name in ("A", "B", "C")
    ]
    addition = "D" if kind == "list" else {"name": "D", "phone": "01"}
    async with settings_sessions() as session:
        await settings_store.set_value(session, key, initial)
        await session.commit()

    original_get = settings_store.get

    async def racing_get(session, setting_key):
        snapshot = await original_get(session, setting_key)
        async with settings_sessions() as other:
            await settings_store.set_value(other, key, initial + [addition])
            await other.commit()
        return snapshot

    @asynccontextmanager
    async def scope():
        async with settings_sessions() as session:
            yield session
            await session.commit()

    audit = AsyncMock()
    show = AsyncMock()
    monkeypatch.setattr(settings_store, "get", racing_get)
    monkeypatch.setattr(handler, "session_scope", scope)
    monkeypatch.setattr(handler, f"_show_{kind}_card", show)
    monkeypatch.setattr(handler.ops_svc, "write_audit", audit)
    await getattr(handler, f"_{kind}_delete")(
        object(), 7, f"{key}:1:{settings_store.value_revision(initial)}",
    )
    async with settings_sessions() as session:
        assert await original_get(session, key) == initial + [addition]
    audit.assert_not_awaited()
    assert "изменён" in show.await_args.kwargs["notice"]


async def test_conflicting_add_keeps_operator_input_intent(monkeypatch):
    from aemr_bot.handlers import admin_settings as facade
    from aemr_bot.handlers import admin_settings_list as handler
    from tests._helpers import make_event

    @asynccontextmanager
    async def scope():
        yield object()

    monkeypatch.setattr(facade, "ensure_role", AsyncMock(return_value=True))
    monkeypatch.setattr(handler, "session_scope", scope)
    monkeypatch.setattr(settings_store, "get", AsyncMock(return_value=["A"]))
    monkeypatch.setattr(settings_store, "set_value", AsyncMock(side_effect=settings_store.SettingConflictError))
    monkeypatch.setattr(handler, "op_send", AsyncMock())
    monkeypatch.setattr(handler.ops_svc, "write_audit", AsyncMock())
    facade._intent_set(501, key="topics", kind="list_add")
    try:
        assert await facade.handle_settings_edit_text(make_event(user_id=501), "B") is True
        assert facade._intent_get(501) is not None
        handler.ops_svc.write_audit.assert_not_awaited()
    finally:
        facade._intent_drop(501)
