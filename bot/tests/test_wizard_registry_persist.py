"""Тесты на persist hooks `services.wizard_registry`.

Покрывают `_spawn_persist`, `schedule_persist_op`,
`schedule_persist_broadcast` — фоновую запись wizard state в БД.

Без БД: подменяем session_scope/wizard_persist через monkeypatch.
Без event loop: проверяем graceful no-op (caller вне asyncio).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from aemr_bot.services import wizard_registry as wr


@pytest.fixture(autouse=True)
def _clean():
    """Между тестами — чистое состояние."""
    wr.reset_all()
    yield
    wr.reset_all()


class TestSpawnPersistOutsideLoop:
    """`_spawn_persist` без running loop — no-op без exception."""

    def test_no_running_loop_returns_silently(self) -> None:
        """`schedule_persist_op` за пределами event loop — graceful
        no-op. Сюда попадают unit-тесты и импорт-сайд-эффекты."""
        wr.set_op_wizard(42, {"step": "awaiting_id"})
        # Вне event loop фоновая задача persist НЕ спавнится (иначе
        # RuntimeError), но и наружу ничего не бросается.
        with patch.object(wr, "spawn_background_task") as spawn:
            result = wr.schedule_persist_op(42)
        assert result is None
        spawn.assert_not_called()

    def test_schedule_persist_broadcast_outside_loop(self) -> None:
        wr.set_broadcast_wizard(42, {"step": "awaiting_text"})
        with patch.object(wr, "spawn_background_task") as spawn:
            result = wr.schedule_persist_broadcast(42)
        assert result is None
        spawn.assert_not_called()


class TestSchedulePersistOpInsideLoop:
    """Внутри running loop — `schedule_persist_op` спавнит background
    task, который вызывает `wizard_persist.save_op_wizard`."""

    @pytest.mark.asyncio
    async def test_save_spawns_background_task(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[tuple[int, dict]] = []

        async def _fake_save(_session, operator_id, state):
            captured.append((operator_id, dict(state)))

        @asynccontextmanager
        async def _fake_session_scope():
            yield object()  # фиктивная сессия — fake_save её игнорирует

        monkeypatch.setattr(
            "aemr_bot.services.wizard_persist.save_op_wizard", _fake_save
        )
        monkeypatch.setattr(
            "aemr_bot.services.wizard_registry.session_scope",
            _fake_session_scope,
        )

        wr.set_op_wizard(42, {"step": "awaiting_id", "target_id": 100})
        wr.schedule_persist_op(42)

        # Background task запущен — дать loop'у выполнить его.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert captured, "background save был ожидаем, но не сработал"
        assert captured[0][0] == 42
        assert captured[0][1]["step"] == "awaiting_id"

    @pytest.mark.asyncio
    async def test_delete_when_state_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Если state=None и в registry нет записи — спавнится delete."""
        deleted: list[int] = []

        async def _fake_delete(_session, operator_id):
            deleted.append(operator_id)

        @asynccontextmanager
        async def _fake_session_scope():
            yield object()

        monkeypatch.setattr(
            "aemr_bot.services.wizard_persist.delete_op_wizard", _fake_delete
        )
        monkeypatch.setattr(
            "aemr_bot.services.wizard_registry.session_scope",
            _fake_session_scope,
        )

        # В registry нет op-wizard для оператора 99 — schedule с
        # state=None должен спавнить delete.
        wr.schedule_persist_op(99)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert deleted == [99]

    @pytest.mark.asyncio
    async def test_save_uses_explicit_state_when_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Если state передан явно — handler может хранить свой dict
        отдельно от registry. Snapshot должен быть передан как dict."""
        captured: list[dict] = []

        async def _fake_save(_session, _operator_id, state):
            captured.append(dict(state))

        @asynccontextmanager
        async def _fake_session_scope():
            yield object()

        monkeypatch.setattr(
            "aemr_bot.services.wizard_persist.save_op_wizard", _fake_save
        )
        monkeypatch.setattr(
            "aemr_bot.services.wizard_registry.session_scope",
            _fake_session_scope,
        )

        explicit_state = {"step": "awaiting_role", "target_id": 200}
        wr.schedule_persist_op(50, state=explicit_state)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert captured, "background save был ожидаем"
        assert captured[0] == explicit_state

    @pytest.mark.asyncio
    async def test_persist_save_swallows_exceptions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Если save в БД упало — фоновая задача гасит exception,
        in-memory остаётся консистентным."""

        async def _broken_save(_session, _operator_id, _state):
            raise RuntimeError("DB connection refused")

        @asynccontextmanager
        async def _fake_session_scope():
            yield object()

        monkeypatch.setattr(
            "aemr_bot.services.wizard_persist.save_op_wizard", _broken_save
        )
        monkeypatch.setattr(
            "aemr_bot.services.wizard_registry.session_scope",
            _fake_session_scope,
        )

        wr.set_op_wizard(42, {"x": 1})
        wr.schedule_persist_op(42)

        # exception не должно всплыть наружу — caller продолжает.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # in-memory не задет.
        assert wr.get_op_wizard(42) == {"x": 1}


class TestSchedulePersistBroadcastInsideLoop:
    """Аналогично op-wizard, но для broadcast wizard."""

    @pytest.mark.asyncio
    async def test_save_spawns_background_task(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[tuple[int, dict]] = []

        async def _fake_save(_session, operator_id, state):
            captured.append((operator_id, dict(state)))

        @asynccontextmanager
        async def _fake_session_scope():
            yield object()

        monkeypatch.setattr(
            "aemr_bot.services.wizard_persist.save_broadcast_wizard",
            _fake_save,
        )
        monkeypatch.setattr(
            "aemr_bot.services.wizard_registry.session_scope",
            _fake_session_scope,
        )

        wr.set_broadcast_wizard(42, {"step": "awaiting_text", "text": "hi"})
        wr.schedule_persist_broadcast(42)

        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert captured
        assert captured[0][0] == 42
        assert captured[0][1]["step"] == "awaiting_text"

    @pytest.mark.asyncio
    async def test_delete_when_state_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        deleted: list[int] = []

        async def _fake_delete(_session, operator_id):
            deleted.append(operator_id)

        @asynccontextmanager
        async def _fake_session_scope():
            yield object()

        monkeypatch.setattr(
            "aemr_bot.services.wizard_persist.delete_broadcast_wizard",
            _fake_delete,
        )
        monkeypatch.setattr(
            "aemr_bot.services.wizard_registry.session_scope",
            _fake_session_scope,
        )

        wr.schedule_persist_broadcast(77)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert deleted == [77]
