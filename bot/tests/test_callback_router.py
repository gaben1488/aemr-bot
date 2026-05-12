"""Тесты callback-router.

Цель — не покрыть каждую бизнес-ветку повторно, а зафиксировать матрицу
маршрутизации callback payload'ов: какие payload'ы считаются жительскими,
какие разрешены в админ-группе, и что malformed id-хвосты не запускают
привилегированные действия.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("maxapi", reason="callback dispatcher тесты требуют maxapi")


class _CapturingDispatcher:
    def __init__(self) -> None:
        self.callback_handler = None
        self.message_handler = None

    def message_callback(self):
        def deco(fn):
            self.callback_handler = fn
            return fn

        return deco

    def message_created(self):
        def deco(fn):
            self.message_handler = fn
            return fn

        return deco


@pytest.fixture
def on_callback():
    from aemr_bot.handlers import appeal

    dp = _CapturingDispatcher()
    appeal.register(dp)
    assert dp.callback_handler is not None
    return dp.callback_handler


def _callback_event(*, payload: str, chat_id: int = 555, user_id: int = 7):
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return SimpleNamespace(
        bot=bot,
        callback=SimpleNamespace(
            payload=payload,
            callback_id="cb-1",
            user=SimpleNamespace(user_id=user_id, first_name="X"),
        ),
        message=SimpleNamespace(
            answer=AsyncMock(),
            sender=SimpleNamespace(user_id=user_id, first_name="X"),
            recipient=SimpleNamespace(chat_id=chat_id),
            body=SimpleNamespace(text="", attachments=[], mid="m-1"),
        ),
    )


class TestRouteRegistry:
    @pytest.mark.parametrize(
        ("payload", "group", "admin_allowed"),
        [
            ("menu:new_appeal", "citizen_flow", False),
            ("consent:yes", "citizen_flow", False),
            ("consent:no", "citizen_flow", False),
            ("cancel", "citizen_flow", False),
            ("addr:reuse", "citizen_flow", False),
            ("addr:new", "citizen_flow", False),
            ("locality:0", "citizen_flow", False),
            ("topic:0", "citizen_flow", False),
            ("geo:confirm", "geo_flow", False),
            ("geo:edit_address", "geo_flow", False),
            ("geo:other_locality", "geo_flow", False),
            ("appeal:submit", "citizen_flow", False),
            ("broadcast:confirm", "broadcast_admin", True),
            ("broadcast:abort", "broadcast_admin", True),
            ("broadcast:edit", "broadcast_admin", True),
            ("broadcast:stop:1", "broadcast_admin", True),
            ("op:menu", "operator_admin", True),
            ("op:stats_today", "operator_admin", True),
            ("op:reply:1", "operator_admin", True),
            ("op:reopen:1", "operator_admin", True),
            ("op:close:1", "operator_admin", True),
            ("op:erase:1", "operator_admin", True),
            ("op:block:1", "operator_admin", True),
            ("op:unblock:1", "operator_admin", True),
            ("op:opadd:role", "operator_admin", True),
            ("op:setkey:topics", "operator_admin", True),
            ("menu:settings", "menu_fallback", False),
        ],
    )
    def test_payload_group_matrix(self, payload, group, admin_allowed) -> None:
        from aemr_bot.handlers import callback_router

        route = callback_router.route_for(payload)
        assert route.group == group
        assert route.admin_allowed is admin_allowed
        assert callback_router.is_admin_callback(payload) is admin_allowed

    def test_exact_routes_do_not_get_shadowed_by_prefix_routes(self) -> None:
        from aemr_bot.handlers import callback_router

        for route in callback_router.EXACT_ROUTES:
            assert callback_router.route_for(route.pattern) == route

    @pytest.mark.parametrize(
        ("payload", "prefix", "expected"),
        [
            ("topic:0", "topic:", 0),
            ("topic:42", "topic:", 42),
            ("topic:x", "topic:", None),
            ("topic:", "topic:", None),
            ("locality:x", "locality:", None),
            ("broadcast:stop:x", "broadcast:stop:", None),
            ("op:reply:x", "op:reply:", None),
        ],
    )
    def test_parse_int_tail(self, payload, prefix, expected) -> None:
        from aemr_bot.handlers import callback_router

        assert callback_router.parse_int_tail(payload, prefix) == expected


class TestAdminChatBoundary:
    @pytest.mark.asyncio
    async def test_citizen_payload_in_admin_chat_is_acked_and_ignored(
        self, on_callback
    ) -> None:
        event = _callback_event(payload="menu:new_appeal", chat_id=123)
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 123), \
             patch("aemr_bot.handlers.appeal.ack_callback", AsyncMock()) as ack, \
             patch("aemr_bot.handlers.appeal.appeal_funnel.start_appeal_flow",
                   AsyncMock()) as start_flow:
            await on_callback(event)

        ack.assert_awaited_once()
        start_flow.assert_not_called()

    @pytest.mark.asyncio
    async def test_broadcast_unsubscribe_is_not_admin_callback(
        self, on_callback
    ) -> None:
        event = _callback_event(payload="broadcast:unsubscribe", chat_id=123)
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 123), \
             patch("aemr_bot.handlers.appeal.ack_callback", AsyncMock()) as ack, \
             patch("aemr_bot.handlers.menu.handle_callback", AsyncMock()) as menu:
            await on_callback(event)

        ack.assert_awaited_once()
        menu.assert_not_called()


class TestMalformedAdminPayloads:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("payload", "target_patch"),
        [
            (
                "broadcast:stop:x",
                "aemr_bot.handlers.broadcast._handle_stop",
            ),
            (
                "op:reply:x",
                "aemr_bot.handlers.admin_commands.run_reply_intent",
            ),
            (
                "op:reopen:x",
                "aemr_bot.handlers.admin_commands.run_reopen",
            ),
            (
                "op:close:x",
                "aemr_bot.handlers.admin_commands.run_close",
            ),
            (
                "op:erase:x",
                "aemr_bot.handlers.admin_commands.run_erase_for_appeal",
            ),
            (
                "op:block:x",
                "aemr_bot.handlers.admin_commands.run_block_for_appeal",
            ),
            (
                "op:unblock:x",
                "aemr_bot.handlers.admin_commands.run_block_for_appeal",
            ),
        ],
    )
    async def test_malformed_ids_are_acked_without_action(
        self, on_callback, payload, target_patch
    ) -> None:
        event = _callback_event(payload=payload, chat_id=123)
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 123), \
             patch("aemr_bot.handlers.appeal.ack_callback", AsyncMock()) as ack, \
             patch(target_patch, AsyncMock()) as target:
            await on_callback(event)

        ack.assert_awaited_once()
        target.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", ["topic:x", "locality:x"])
    async def test_malformed_citizen_ids_are_acked_without_action(
        self, on_callback, payload
    ) -> None:
        event = _callback_event(payload=payload, chat_id=555)
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 123), \
             patch("aemr_bot.handlers.appeal.ack_callback", AsyncMock()) as ack, \
             patch("aemr_bot.handlers.appeal.appeal_funnel.ask_summary",
                   AsyncMock()) as ask_summary, \
             patch("aemr_bot.handlers.appeal.appeal_funnel.ask_address",
                   AsyncMock()) as ask_address:
            await on_callback(event)

        ack.assert_awaited_once()
        ask_summary.assert_not_called()
        ask_address.assert_not_called()
