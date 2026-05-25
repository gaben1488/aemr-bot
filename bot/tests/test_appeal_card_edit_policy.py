"""Event-log семантика admin appeal card (DDD pivot 2026-05-25).

Заменяет старый файл «edit vs new policy» — старая модель смешивала
event-карточки и навигацию. Новая модель: admin-карточка обращения
= иммутабельная запись о событии, каждое изменение публикует новую
карточку. Никогда не редактируется.

Эти тесты фиксируют поведение для четырёх action'ов оператора:
reopen / close / block / unblock. Все ведут к НОВОЙ карточке через
admin_card.render (помечено внутри как send_message, не edit).
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


def _make_appeal():
    user = SimpleNamespace(
        is_blocked=False,
        max_user_id=42,
        first_name="Тест",
        phone="+79991234567",
        subscribed_broadcast=False,
        consent_pdn_at=None,
        consent_revoked_at=None,
    )
    appeal = SimpleNamespace(
        id=5,
        status="new",
        user=user,
        closed_due_to_revoke=False,
        locality="—", address="—", topic="—", summary="—",
        attachments=[],
        admin_message_id="original-1",
        last_admin_card_mid="latest-3",
        created_at=None,
    )
    appeal.__dict__["messages"] = []
    return appeal


class TestReopenPublishesEventCard:
    @pytest.mark.asyncio
    async def test_reopen_calls_render_not_edit(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        appeal = _make_appeal()
        render_mock = AsyncMock(return_value="new-event-mid")
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
                "aemr_bot.handlers.admin_appeal_ops.appeals_service.get_by_id_with_messages",
                AsyncMock(return_value=appeal),
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
                AsyncMock(),
            ),
            patch(
                "aemr_bot.services.admin_card.render", render_mock
            ),
            patch("aemr_bot.utils.event.ack_callback", AsyncMock()),
        ):
            await admin_appeal_ops.run_reopen(event, 5)
        # render вызван (без edit_message напрямую)
        render_mock.assert_awaited_once()
        event.bot.edit_message.assert_not_called()


class TestCloseClosesBothCardsBug:
    """Регрессия на конкретный bug владельца:
    «открыл 2 карточки обращений и закрыл одну — одна обновилась,
    другая нет».

    Старая семантика: edit оригинал admin_message_id → только
    оригинал обновлялся, та карточка где оператор реально тапнул —
    нет (если это была следовая после followup).

    Новая семантика: каждый close = новая карточка с CLOSED. Старые
    остаются как audit-trail, но оператор гарантированно видит
    «закрыто» в новой карточке внизу."""

    @pytest.mark.asyncio
    async def test_close_publishes_new_event_card(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        appeal = _make_appeal()
        render_mock = AsyncMock(return_value="closed-event-mid")
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
                "aemr_bot.handlers.admin_appeal_ops.appeals_service.get_by_id_with_messages",
                AsyncMock(return_value=appeal),
            ),
            patch(
                "aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
                AsyncMock(),
            ),
            patch(
                "aemr_bot.services.admin_card.render", render_mock
            ),
            patch("aemr_bot.utils.event.ack_callback", AsyncMock()),
        ):
            await admin_appeal_ops.run_close(event, 5)
        render_mock.assert_awaited_once()
        event.bot.edit_message.assert_not_called()
