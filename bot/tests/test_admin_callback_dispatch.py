"""Тесты dispatch_admin_callback — таблица broadcast:* / op:* callback'ов.

Эти ветки вынесены из appeal.py:on_callback (батч 1 polish) и раньше
НЕ были покрыты ни одним тестом (отмечено картой рефактора). Здесь —
прямые тесты диспетчера: для каждого типа маршрута (exact / prefix-id /
prefix-raw / fallthrough / битый хвост) проверяем, что вызван нужный
handler и возвращён правильный bool-контракт.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("maxapi", reason="dispatch тянет handlers-цепочку")

from aemr_bot.handlers import admin_callback_dispatch as dispatch  # noqa: E402


def _event() -> SimpleNamespace:
    return SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock()),
        callback=SimpleNamespace(callback_id="cb-1"),
    )


# ---- exact-маршруты ---------------------------------------------------------


class TestExactRoutes:
    @pytest.mark.asyncio
    async def test_op_menu_acks_and_shows_menu(self) -> None:
        event = _event()
        with patch.object(dispatch, "ack_callback", AsyncMock()) as ack, \
             patch.object(dispatch.admin_commands, "show_op_menu",
                          AsyncMock()) as show:
            handled = await dispatch.dispatch_admin_callback(event, "op:menu")
        assert handled is True
        ack.assert_awaited_once()
        show.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_op_diag_delegates_to_run_diag(self) -> None:
        event = _event()
        with patch.object(dispatch, "ack_callback", AsyncMock()), \
             patch.object(dispatch.admin_commands, "run_diag",
                          AsyncMock()) as run_diag:
            handled = await dispatch.dispatch_admin_callback(event, "op:diag")
        assert handled is True
        run_diag.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_op_stats_week_passes_period(self) -> None:
        event = _event()
        with patch.object(dispatch, "ack_callback", AsyncMock()), \
             patch.object(dispatch.admin_commands, "run_stats",
                          AsyncMock()) as run_stats:
            handled = await dispatch.dispatch_admin_callback(
                event, "op:stats_week"
            )
        assert handled is True
        run_stats.assert_awaited_once_with(event, "week")

    @pytest.mark.asyncio
    async def test_op_stats_today_shows_menu_only_if_sent(self) -> None:
        event = _event()
        # run_stats_today вернул True → меню показывается.
        with patch.object(dispatch, "ack_callback", AsyncMock()), \
             patch.object(dispatch.admin_commands, "run_stats_today",
                          AsyncMock(return_value=True)), \
             patch.object(dispatch.admin_commands, "show_op_menu",
                          AsyncMock()) as show:
            await dispatch.dispatch_admin_callback(event, "op:stats_today")
        show.assert_awaited_once()
        # run_stats_today вернул False → меню НЕ показывается.
        with patch.object(dispatch, "ack_callback", AsyncMock()), \
             patch.object(dispatch.admin_commands, "run_stats_today",
                          AsyncMock(return_value=False)), \
             patch.object(dispatch.admin_commands, "show_op_menu",
                          AsyncMock()) as show2:
            await dispatch.dispatch_admin_callback(event, "op:stats_today")
        show2.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_broadcast_confirm_delegates(self) -> None:
        event = _event()
        with patch.object(dispatch.broadcast_handler, "_handle_confirm",
                          AsyncMock()) as confirm:
            handled = await dispatch.dispatch_admin_callback(
                event, "broadcast:confirm"
            )
        assert handled is True
        confirm.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_op_reply_cancel_delegates_without_extra_ack(self) -> None:
        # ack делегирован внутрь run_reply_cancel — диспетчер не акает.
        event = _event()
        with patch.object(dispatch.admin_commands, "run_reply_cancel",
                          AsyncMock()) as cancel:
            handled = await dispatch.dispatch_admin_callback(
                event, "op:reply_cancel"
            )
        assert handled is True
        cancel.assert_awaited_once_with(event)


# ---- prefix-id маршруты (op:<verb>:<int>) ----------------------------------


class TestPrefixIdRoutes:
    @pytest.mark.asyncio
    async def test_op_reply_parses_id(self) -> None:
        event = _event()
        with patch.object(dispatch.admin_commands, "run_reply_intent",
                          AsyncMock()) as run:
            handled = await dispatch.dispatch_admin_callback(
                event, "op:reply:42"
            )
        assert handled is True
        run.assert_awaited_once_with(event, 42)

    @pytest.mark.asyncio
    async def test_op_block_passes_blocked_true(self) -> None:
        event = _event()
        with patch.object(dispatch.admin_commands, "run_block_for_appeal",
                          AsyncMock()) as run:
            await dispatch.dispatch_admin_callback(event, "op:block:7")
        run.assert_awaited_once_with(event, 7, blocked=True)

    @pytest.mark.asyncio
    async def test_op_unblock_passes_blocked_false(self) -> None:
        event = _event()
        with patch.object(dispatch.admin_commands, "run_block_for_appeal",
                          AsyncMock()) as run:
            await dispatch.dispatch_admin_callback(event, "op:unblock:7")
        run.assert_awaited_once_with(event, 7, blocked=False)

    @pytest.mark.asyncio
    async def test_broadcast_stop_parses_id(self) -> None:
        event = _event()
        with patch.object(dispatch.broadcast_handler, "_handle_stop",
                          AsyncMock()) as stop:
            handled = await dispatch.dispatch_admin_callback(
                event, "broadcast:stop:9"
            )
        assert handled is True
        stop.assert_awaited_once_with(event, 9)

    @pytest.mark.asyncio
    async def test_malformed_id_acks_without_action(self) -> None:
        # Битый хвост (`op:reply:abc`) — stale/повреждённая кнопка.
        # Диспетчер ack'ает и возвращает True, но handler НЕ вызывает.
        event = _event()
        with patch.object(dispatch, "ack_callback", AsyncMock()) as ack, \
             patch.object(dispatch.admin_commands, "run_reply_intent",
                          AsyncMock()) as run:
            handled = await dispatch.dispatch_admin_callback(
                event, "op:reply:not-a-number"
            )
        assert handled is True
        ack.assert_awaited_once()
        run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_id_tail_acks_without_action(self) -> None:
        event = _event()
        with patch.object(dispatch, "ack_callback", AsyncMock()) as ack, \
             patch.object(dispatch.admin_commands, "run_close",
                          AsyncMock()) as run:
            handled = await dispatch.dispatch_admin_callback(event, "op:close:")
        assert handled is True
        ack.assert_awaited_once()
        run.assert_not_awaited()


# ---- prefix-raw маршруты (handler сам разбирает payload) -------------------


class TestPrefixRawRoutes:
    @pytest.mark.asyncio
    async def test_op_aud_passes_full_payload(self) -> None:
        event = _event()
        with patch.object(dispatch.admin_commands, "run_audience_action",
                          AsyncMock()) as run:
            handled = await dispatch.dispatch_admin_callback(
                event, "op:aud:block:5"
            )
        assert handled is True
        run.assert_awaited_once_with(event, "op:aud:block:5")

    @pytest.mark.asyncio
    async def test_op_setkey_passes_full_payload(self) -> None:
        event = _event()
        with patch.object(dispatch.admin_commands, "run_settings_action",
                          AsyncMock()) as run:
            handled = await dispatch.dispatch_admin_callback(
                event, "op:setkey:topics"
            )
        assert handled is True
        run.assert_awaited_once_with(event, "op:setkey:topics")


# ---- fallthrough контракт ---------------------------------------------------


class TestFallthrough:
    @pytest.mark.asyncio
    async def test_citizen_payload_returns_false(self) -> None:
        # Жительский payload — не admin-callback, dispatch отдаёт False,
        # caller продолжает fallthrough в menu.handle_callback.
        event = _event()
        assert await dispatch.dispatch_admin_callback(
            event, "menu:new_appeal"
        ) is False

    @pytest.mark.asyncio
    async def test_unknown_op_tail_returns_false(self) -> None:
        # `op:` обёртка с неизвестным хвостом — раньше управление
        # проваливалось из if-обёртки в menu.handle_callback. Контракт
        # сохранён: dispatch возвращает False.
        event = _event()
        assert await dispatch.dispatch_admin_callback(
            event, "op:totally_unknown"
        ) is False

    @pytest.mark.asyncio
    async def test_unknown_broadcast_tail_returns_false(self) -> None:
        event = _event()
        assert await dispatch.dispatch_admin_callback(
            event, "broadcast:weird"
        ) is False


# ---- синхронность с callback_router ----------------------------------------


class TestRegistrySync:
    def test_every_dispatch_route_is_admin_in_router(self) -> None:
        """Каждый exact/prefix маршрут диспетчера должен быть
        admin_allowed=True в callback_router — иначе admin-chat guard
        в on_callback отсечёт кнопку до того, как она дойдёт сюда."""
        from aemr_bot.handlers import callback_router

        patterns = (
            list(dispatch._EXACT.keys())
            + [p for p, _ in dispatch._PREFIX_ID]
            + [p for p, _ in dispatch._PREFIX_RAW]
        )
        not_admin = [
            p for p in patterns
            if not callback_router.is_admin_callback(p)
        ]
        assert not not_admin, (
            f"Маршруты диспетчера, не помеченные admin в callback_router "
            f"(их отсечёт admin-chat guard): {not_admin}"
        )
