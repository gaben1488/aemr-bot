"""Тесты на info-богатые уведомления оператору.

Запрос: «в информирующих сообщениях о подписках-отписках присутствует
только max_id - я бы хотел чтобы было подробней».

Реализовано в `services/admin_events._describe_user(max_user_id)` —
возвращает блок «Житель: имя · phone · MAX id N \\n Статус: 🔔 ... ·
✅ согласие ...».

Здесь тесты на формат и edge cases.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope


pytest.importorskip("maxapi", reason="нужен для config")


def _make_user(**overrides) -> SimpleNamespace:
    defaults = {
        "first_name": "Сергей",
        "phone": "+79991234567",
        "subscribed_broadcast": False,
        "consent_pdn_at": None,
        "consent_revoked_at": None,
        "is_blocked": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestDescribeUser:
    @pytest.mark.asyncio
    async def test_user_not_found_returns_only_max_id(self) -> None:
        from aemr_bot.services import admin_events

        with (
            patch("aemr_bot.services.admin_events.session_scope",
                  _fake_session_scope),
            patch(
                "aemr_bot.services.admin_events.users_service.find_by_max_id",
                AsyncMock(return_value=None),
            ),
        ):
            desc = await admin_events._describe_user(42)
        assert "MAX id 42" in desc
        assert "—" in desc

    @pytest.mark.asyncio
    async def test_subscribed_with_consent_shows_full(self) -> None:
        from aemr_bot.services import admin_events

        user = _make_user(
            subscribed_broadcast=True,
            consent_pdn_at=datetime.now(timezone.utc),
        )
        with (
            patch("aemr_bot.services.admin_events.session_scope",
                  _fake_session_scope),
            patch(
                "aemr_bot.services.admin_events.users_service.find_by_max_id",
                AsyncMock(return_value=user),
            ),
        ):
            desc = await admin_events._describe_user(42)
        assert "Сергей" in desc
        # masked phone — последние 4 цифры
        assert "4567" in desc
        # полный телефон НЕ светим
        assert "9991234567" not in desc
        assert "MAX id 42" in desc
        assert "🔔 подписан на рассылку" in desc
        assert "✅ согласие активно" in desc

    @pytest.mark.asyncio
    async def test_revoked_consent_marked(self) -> None:
        from aemr_bot.services import admin_events

        user = _make_user(consent_revoked_at=datetime.now(timezone.utc))
        with (
            patch("aemr_bot.services.admin_events.session_scope",
                  _fake_session_scope),
            patch(
                "aemr_bot.services.admin_events.users_service.find_by_max_id",
                AsyncMock(return_value=user),
            ),
        ):
            desc = await admin_events._describe_user(42)
        assert "🔁 согласие отозвано" in desc
        # При отзыве consent_pdn_at в логике не приоритетен —
        # «активно» НЕ должно появиться
        assert "✅ согласие активно" not in desc

    @pytest.mark.asyncio
    async def test_blocked_marked(self) -> None:
        from aemr_bot.services import admin_events

        user = _make_user(is_blocked=True)
        with (
            patch("aemr_bot.services.admin_events.session_scope",
                  _fake_session_scope),
            patch(
                "aemr_bot.services.admin_events.users_service.find_by_max_id",
                AsyncMock(return_value=user),
            ),
        ):
            desc = await admin_events._describe_user(42)
        assert "🚫 заблокирован" in desc


class TestNotifyHelpers:
    """Что финальные сообщения включают descriptor (не только max_id)."""

    @pytest.mark.asyncio
    async def test_notify_broadcast_subscribed_includes_name_phone(self) -> None:
        from aemr_bot.services import admin_events

        bot = SimpleNamespace(send_message=AsyncMock())
        user = _make_user(
            subscribed_broadcast=True,
            consent_pdn_at=datetime.now(timezone.utc),
        )
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
            patch("aemr_bot.services.admin_events.session_scope",
                  _fake_session_scope),
            patch(
                "aemr_bot.services.admin_events.users_service.find_by_max_id",
                AsyncMock(return_value=user),
            ),
        ):
            await admin_events.notify_broadcast_subscribed(bot, max_user_id=42)
        sent_text = bot.send_message.await_args.kwargs["text"]
        assert "Сергей" in sent_text
        assert "4567" in sent_text
        assert "MAX id 42" in sent_text
        assert "подписался" in sent_text


class TestMaskPhone:
    def test_simple_mask(self) -> None:
        from aemr_bot.services.admin_events import _mask_phone

        assert _mask_phone("+79991234567") == "+7***4567"
        assert _mask_phone("89991234567") == "+7***4567"
        assert _mask_phone(None) == "—"
        assert _mask_phone("") == "—"
        # короткий номер не маскируем (нечего скрывать)
        assert _mask_phone("12") == "12"
