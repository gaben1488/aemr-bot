"""Политика «edit vs new» для карточки обращения в админ-чате.

Запрос владельца: карточка обращения должна **редактироваться** только при
трёх действиях:
  1. Ответ оператора (reply)
  2. Возобновление (reopen)
  3. Закрытие без ответа (close)

Во всех остальных случаях (block/unblock жителя, дополнение от жителя)
шлём **новую карточку** — оператор уже прокрутил чат далеко вниз,
edit на старой карточке вверху не виден.

Эти тесты RED'ят до фикса: они фиксируют ОЖИДАЕМОЕ поведение после
правки `_show_appeal_card_or_result` и followup-флоу.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event


pytest.importorskip("maxapi", reason="handlers tests require maxapi")


def _make_event(*, user_id: int = 7) -> SimpleNamespace:
    return make_event(
        chat_id=555,
        user_id=user_id,
        with_callback=True,
        with_edit_message=True,
    )


def _patch_card_render():
    """Подменяем `card_format.admin_card` и приклеиваем appeal с user,
    чтобы _show_appeal_card_or_result дошёл до send_or_edit_screen."""
    fake_user = SimpleNamespace(
        is_blocked=False,
        max_user_id=42,
        first_name="Тест",
        phone="+79991234567",
        subscribed_broadcast=False,
        consent_pdn_at=None,
        consent_revoked_at=None,
    )
    fake_appeal = SimpleNamespace(
        id=5,
        status="new",
        user=fake_user,
        closed_due_to_revoke=False,
        locality="—",
        address="—",
        topic="—",
        summary="—",
        attachments=[],
        messages=[],
        created_at=None,
        answered_at=None,
        closed_at=None,
    )
    return fake_appeal


# ---- WHITELIST: edit разрешён --------------------------------------


class TestReopenEditsInPlace:
    @pytest.mark.asyncio
    async def test_reopen_uses_edit_in_place(self) -> None:
        """run_reopen передаёт edit_in_place=True в helper —
        send_or_edit_screen НЕ форсит новую карточку."""
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        sent_screen = AsyncMock()
        appeal = _patch_card_render()
        with (
            patch(
                "aemr_bot.handlers.admin_appeal_ops.ensure_operator",
                AsyncMock(return_value=True),
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.session_scope",
                _fake_session_scope,
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.appeals_service.reopen",
                AsyncMock(return_value=True),
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.appeals_service.get_by_id",
                AsyncMock(return_value=appeal),
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
                AsyncMock(),
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.send_or_edit_screen",
                sent_screen,
            ),
            patch("aemr_bot.utils.event.ack_callback", AsyncMock()),
        ):
            await admin_appeal_ops.run_reopen(event, 5)
        assert sent_screen.await_count >= 1
        # Любой вызов из run_reopen НЕ должен ставить force_new_message=True
        for call in sent_screen.await_args_list:
            assert call.kwargs.get("force_new_message") is not True


class TestCloseEditsInPlace:
    @pytest.mark.asyncio
    async def test_close_uses_edit_in_place(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        sent_screen = AsyncMock()
        appeal = _patch_card_render()
        with (
            patch(
                "aemr_bot.handlers.admin_appeal_ops.ensure_operator",
                AsyncMock(return_value=True),
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.session_scope",
                _fake_session_scope,
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.appeals_service.close",
                AsyncMock(return_value=True),
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.appeals_service.get_by_id",
                AsyncMock(return_value=appeal),
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
                AsyncMock(),
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.send_or_edit_screen",
                sent_screen,
            ),
            patch("aemr_bot.utils.event.ack_callback", AsyncMock()),
        ):
            await admin_appeal_ops.run_close(event, 5)
        assert sent_screen.await_count >= 1
        for call in sent_screen.await_args_list:
            assert call.kwargs.get("force_new_message") is not True


# ---- NOT-WHITELIST: всегда новая карточка ----------------------------


class TestBlockSendsNewCard:
    @pytest.mark.asyncio
    async def test_block_forces_new_card(self) -> None:
        """run_block_for_appeal должен после блока ШЛАТЬ новую карточку,
        не edit'ить старую."""
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        sent_screen = AsyncMock()
        appeal = _patch_card_render()
        with (
            patch(
                "aemr_bot.handlers.admin_appeal_ops.ensure_role",
                AsyncMock(return_value=True),
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.session_scope",
                _fake_session_scope,
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.appeals_service.get_by_id",
                AsyncMock(return_value=appeal),
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.users_service.set_blocked",
                AsyncMock(return_value=True),
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
                AsyncMock(),
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.send_or_edit_screen",
                sent_screen,
            ),
            patch("aemr_bot.utils.event.ack_callback", AsyncMock()),
        ):
            await admin_appeal_ops.run_block_for_appeal(
                event, appeal_id=5, blocked=True
            )
        # Финальный вызов showing-карточки должен иметь force_new_message=True.
        # Берём последний вызов — он рисует карточку после действия.
        last_call = sent_screen.await_args_list[-1]
        assert last_call.kwargs.get("force_new_message") is True


class TestUnblockSendsNewCard:
    @pytest.mark.asyncio
    async def test_unblock_forces_new_card(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        sent_screen = AsyncMock()
        appeal = _patch_card_render()
        with (
            patch(
                "aemr_bot.handlers.admin_appeal_ops.ensure_role",
                AsyncMock(return_value=True),
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.session_scope",
                _fake_session_scope,
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.appeals_service.get_by_id",
                AsyncMock(return_value=appeal),
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.users_service.set_blocked",
                AsyncMock(return_value=True),
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
                AsyncMock(),
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.send_or_edit_screen",
                sent_screen,
            ),
            patch("aemr_bot.utils.event.ack_callback", AsyncMock()),
        ):
            await admin_appeal_ops.run_block_for_appeal(
                event, appeal_id=5, blocked=False
            )
        last_call = sent_screen.await_args_list[-1]
        assert last_call.kwargs.get("force_new_message") is True
