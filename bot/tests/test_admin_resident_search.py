"""Тесты `/find_resident` — Cluster G (Codex PR 9).

Поиск жителя оператором по телефону или MAX user id. Покрываем
все ветки `run_find_resident`:

1. **`_detect_query_kind`** — pure классификатор: max_user_id (4-9 цифр),
   phone (10+ цифр или с `+`), invalid.
2. **`_mask_query_for_audit`** — маскировка PII для audit-log.
3. **`_format_*` helpers** — рендер карточки результата.
4. **`run_find_resident`** — non-admin chat / non-operator / empty query /
   invalid query / max_user_id found / phone found / not found /
   audit-log запись.

Поведение `find_by_phone` при множественном совпадении — отдельный
тест (возвращает None, оператор видит not-found с подсказкой о
max_user_id).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


pytest.importorskip("maxapi", reason="нужен maxapi для admin_resident_search импортов")

from tests._helpers import make_event


# ──────────────────────────────────────────────────────────────────────
# Pure helpers — детектор и маскировка
# ──────────────────────────────────────────────────────────────────────


class TestDetectQueryKind:
    """`_detect_query_kind(query)` — классификатор входа."""

    @pytest.mark.parametrize(
        "query,expected_kind",
        [
            ("123456789", "max_user_id"),  # 9 цифр — макс граница id
            ("12345", "max_user_id"),  # короткий id 5 цифр
            ("1234", "max_user_id"),  # граница 4 цифры
            ("+79991234567", "phone"),  # явный phone
            ("79991234567", "phone"),  # 11 цифр без `+` → phone
            ("999123456789", "phone"),  # 12 цифр (международный)
            ("", "invalid"),
            ("   ", "invalid"),
            ("abc", "invalid"),
            ("12", "invalid"),  # < 4 цифр
            ("123", "invalid"),
            ("+abc", "phone"),  # `+` префикс → phone (let downstream validate)
        ],
    )
    def test_classifies(self, query: str, expected_kind: str) -> None:
        from aemr_bot.handlers.admin_resident_search import _detect_query_kind

        kind, _ = _detect_query_kind(query)
        assert kind == expected_kind

    def test_phone_without_plus_gets_normalised(self) -> None:
        from aemr_bot.handlers.admin_resident_search import _detect_query_kind

        kind, value = _detect_query_kind("79991234567")
        assert kind == "phone"
        assert value.startswith("+")

    def test_phone_with_plus_kept_as_is(self) -> None:
        from aemr_bot.handlers.admin_resident_search import _detect_query_kind

        kind, value = _detect_query_kind("+79991234567")
        assert kind == "phone"
        assert value == "+79991234567"


class TestMaskQueryForAudit:
    """`_mask_query_for_audit(kind, value)` — PII masking перед audit."""

    def test_phone_masked(self) -> None:
        from aemr_bot.handlers.admin_resident_search import _mask_query_for_audit

        masked = _mask_query_for_audit("phone", "+79991234567")
        # _mask_phone возвращает «+7***NNNN».
        assert "***" in masked
        assert "4567" in masked
        assert "9991" not in masked  # средние цифры не должны утечь

    def test_max_user_id_passthrough(self) -> None:
        from aemr_bot.handlers.admin_resident_search import _mask_query_for_audit

        # max_user_id — публичный идентификатор MAX, не PII.
        assert _mask_query_for_audit("max_user_id", "123456") == "123456"


# ──────────────────────────────────────────────────────────────────────
# Format helpers
# ──────────────────────────────────────────────────────────────────────


class TestFormatStatuses:
    def test_consent_active(self) -> None:
        from aemr_bot.handlers.admin_resident_search import _format_consent_status

        user = SimpleNamespace(
            consent_pdn_at=datetime.now(timezone.utc),
            consent_revoked_at=None,
        )
        assert "активно" in _format_consent_status(user)

    def test_consent_revoked(self) -> None:
        from aemr_bot.handlers.admin_resident_search import _format_consent_status

        user = SimpleNamespace(
            consent_pdn_at=None,
            consent_revoked_at=datetime.now(timezone.utc),
        )
        assert "отозвано" in _format_consent_status(user)

    def test_consent_never(self) -> None:
        from aemr_bot.handlers.admin_resident_search import _format_consent_status

        user = SimpleNamespace(consent_pdn_at=None, consent_revoked_at=None)
        assert "нет" in _format_consent_status(user)

    def test_subscribe_active(self) -> None:
        from aemr_bot.handlers.admin_resident_search import _format_subscribe_status

        user = SimpleNamespace(subscribed_broadcast=True)
        assert "активна" in _format_subscribe_status(user)

    def test_subscribe_none(self) -> None:
        from aemr_bot.handlers.admin_resident_search import _format_subscribe_status

        user = SimpleNamespace(subscribed_broadcast=False)
        assert "нет" in _format_subscribe_status(user)

    def test_last_appeal_none(self) -> None:
        from aemr_bot.handlers.admin_resident_search import _format_last_appeal

        assert "нет" in _format_last_appeal(None)

    def test_last_appeal_renders(self) -> None:
        from aemr_bot.handlers.admin_resident_search import _format_last_appeal

        appeal = SimpleNamespace(
            id=42,
            created_at=datetime(2026, 5, 28, 12, 30, tzinfo=timezone.utc),
            topic="Уличное освещение",
            status="new",
        )
        result = _format_last_appeal(appeal)
        assert "#42" in result
        assert "Уличное освещение" in result
        assert "new" in result


# ──────────────────────────────────────────────────────────────────────
# run_find_resident — top-level handler
# ──────────────────────────────────────────────────────────────────────


class TestRunFindResident:
    """Top-level handler — диспетчер по query + audit + render."""

    @pytest.mark.asyncio
    async def test_non_admin_chat_returns_silently(self) -> None:
        from aemr_bot.handlers import admin_resident_search as mod

        event = make_event(chat_id=42)  # не admin
        with patch.object(mod, "is_admin_chat", return_value=False):
            await mod.run_find_resident(event, "12345")
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_operator_returns_silently(self) -> None:
        from aemr_bot.handlers import admin_resident_search as mod

        event = make_event()
        with patch.object(mod, "is_admin_chat", return_value=True), \
             patch.object(mod, "ensure_operator",
                          AsyncMock(return_value=None)):
            await mod.run_find_resident(event, "12345")
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_query_shows_usage(self) -> None:
        from aemr_bot.handlers import admin_resident_search as mod

        event = make_event()
        operator = SimpleNamespace(max_user_id=999)
        with patch.object(mod, "is_admin_chat", return_value=True), \
             patch.object(mod, "ensure_operator",
                          AsyncMock(return_value=operator)):
            await mod.run_find_resident(event, "")
        event.bot.send_message.assert_awaited_once()
        kwargs = event.bot.send_message.await_args.kwargs
        assert "Использование" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_invalid_query_shows_usage(self) -> None:
        from aemr_bot.handlers import admin_resident_search as mod

        event = make_event()
        operator = SimpleNamespace(max_user_id=999)
        with patch.object(mod, "is_admin_chat", return_value=True), \
             patch.object(mod, "ensure_operator",
                          AsyncMock(return_value=operator)):
            await mod.run_find_resident(event, "abc")
        kwargs = event.bot.send_message.await_args.kwargs
        assert "Использование" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_max_user_id_found_renders_card(self) -> None:
        from aemr_bot.handlers import admin_resident_search as mod

        event = make_event()
        operator = SimpleNamespace(max_user_id=999)
        user = SimpleNamespace(
            id=7,
            max_user_id=123456,
            first_name="Алексей",
            phone="+79991234567",
            consent_pdn_at=datetime.now(timezone.utc),
            consent_revoked_at=None,
            subscribed_broadcast=True,
            is_blocked=False,
        )
        with patch.object(mod, "is_admin_chat", return_value=True), \
             patch.object(mod, "ensure_operator",
                          AsyncMock(return_value=operator)), \
             patch.object(mod, "session_scope") as scope, \
             patch.object(mod.users_service, "find_by_max_id",
                          AsyncMock(return_value=user)), \
             patch.object(mod.ops_svc, "write_audit",
                          AsyncMock()) as audit, \
             patch.object(mod.appeals_service, "list_for_user",
                          AsyncMock(return_value=[])), \
             patch.object(mod.appeals_service, "count_for_user",
                          AsyncMock(return_value=0)):
            scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            await mod.run_find_resident(event, "123456")
        # Audit: один resident_search_found.
        audit.assert_awaited_once()
        audit_kwargs = audit.await_args.kwargs
        assert audit_kwargs["action"] == "resident_search_found"
        assert audit_kwargs["operator_max_user_id"] == 999
        # Карточка отправлена.
        event.bot.send_message.assert_awaited_once()
        text = event.bot.send_message.await_args.kwargs["text"]
        assert "Алексей" in text
        assert "123456" in text
        # PII маскировка: полный телефон НЕ должен светиться.
        assert "+79991234567" not in text
        assert "***" in text

    @pytest.mark.asyncio
    async def test_phone_found_renders_card_with_masked_phone(self) -> None:
        from aemr_bot.handlers import admin_resident_search as mod

        event = make_event()
        operator = SimpleNamespace(max_user_id=999)
        user = SimpleNamespace(
            id=7,
            max_user_id=123456,
            first_name="Мария",
            phone="+79997654321",
            consent_pdn_at=None,
            consent_revoked_at=None,
            subscribed_broadcast=False,
            is_blocked=False,
        )
        with patch.object(mod, "is_admin_chat", return_value=True), \
             patch.object(mod, "ensure_operator",
                          AsyncMock(return_value=operator)), \
             patch.object(mod, "session_scope") as scope, \
             patch.object(mod.users_service, "find_by_phone",
                          AsyncMock(return_value=user)), \
             patch.object(mod.ops_svc, "write_audit", AsyncMock()), \
             patch.object(mod.appeals_service, "list_for_user",
                          AsyncMock(return_value=[])), \
             patch.object(mod.appeals_service, "count_for_user",
                          AsyncMock(return_value=3)):
            scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            await mod.run_find_resident(event, "+79997654321")
        text = event.bot.send_message.await_args.kwargs["text"]
        assert "Мария" in text
        assert "+79997654321" not in text
        assert "***" in text

    @pytest.mark.asyncio
    async def test_not_found_audit_and_message(self) -> None:
        from aemr_bot.handlers import admin_resident_search as mod

        event = make_event()
        operator = SimpleNamespace(max_user_id=999)
        with patch.object(mod, "is_admin_chat", return_value=True), \
             patch.object(mod, "ensure_operator",
                          AsyncMock(return_value=operator)), \
             patch.object(mod, "session_scope") as scope, \
             patch.object(mod.users_service, "find_by_max_id",
                          AsyncMock(return_value=None)), \
             patch.object(mod.ops_svc, "write_audit",
                          AsyncMock()) as audit:
            scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            await mod.run_find_resident(event, "123456")
        audit.assert_awaited_once()
        audit_kwargs = audit.await_args.kwargs
        assert audit_kwargs["action"] == "resident_search_not_found"
        text = event.bot.send_message.await_args.kwargs["text"]
        assert "не найдено" in text.lower() or "ничего не найдено" in text

    @pytest.mark.asyncio
    async def test_blocked_user_shows_blocked_line(self) -> None:
        from aemr_bot.handlers import admin_resident_search as mod

        event = make_event()
        operator = SimpleNamespace(max_user_id=999)
        user = SimpleNamespace(
            id=7,
            max_user_id=42,
            first_name="X",
            phone="+79991234567",
            consent_pdn_at=None,
            consent_revoked_at=None,
            subscribed_broadcast=False,
            is_blocked=True,
        )
        with patch.object(mod, "is_admin_chat", return_value=True), \
             patch.object(mod, "ensure_operator",
                          AsyncMock(return_value=operator)), \
             patch.object(mod, "session_scope") as scope, \
             patch.object(mod.users_service, "find_by_max_id",
                          AsyncMock(return_value=user)), \
             patch.object(mod.ops_svc, "write_audit", AsyncMock()), \
             patch.object(mod.appeals_service, "list_for_user",
                          AsyncMock(return_value=[])), \
             patch.object(mod.appeals_service, "count_for_user",
                          AsyncMock(return_value=0)):
            scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            await mod.run_find_resident(event, "42")
        text = event.bot.send_message.await_args.kwargs["text"]
        assert "Заблокирован" in text
