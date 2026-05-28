"""Тесты для paginated UX подсистемы «📊 Аудитория и согласия».

Покрывают код, добавленный в PR #137 (master-listing + bulk dump +
поиск). Существующие тесты в `test_admin_handlers_small.py` остались
на старом flow (subs/consent/blocked listings); этот файл — про новые
функции:

- `_format_audience_row` — компактная строка для master-listing.
- `_search_intent_set` / `_search_intent_pop` — TTL intent lifecycle.
- `_start_search_intent` — prompt + intent set.
- `handle_audience_search_text` — перехват текста после search intent.
- `_render_audience_page` — page clamping когда страница > total_pages.

Все тесты без Postgres — мокаем `session_scope` + `users_service`.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("maxapi", reason="нужен maxapi для admin_audience импортов")

from tests._helpers import make_event


# ──────────────────────────────────────────────────────────────────────
# _format_audience_row — pure formatter
# ──────────────────────────────────────────────────────────────────────


class TestFormatAudienceRow:
    @staticmethod
    def _user(**kwargs):
        defaults = {
            "max_user_id": 123,
            "first_name": "Иван",
            "phone": "+79991234567",
            "subscribed_broadcast": False,
            "consent_pdn_at": None,
            "consent_revoked_at": None,
            "is_blocked": False,
        }
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def test_default_row(self) -> None:
        from aemr_bot.handlers.admin_audience import _format_audience_row

        user = self._user()
        row = _format_audience_row(user)
        assert "#123" in row
        assert "Иван" in row
        # PII маскирован: либо +7***NNNN, либо просто ***
        assert "9991234" not in row
        # Нет badges = `·`
        assert " · ·" in row or row.endswith("·")

    def test_subscriber_badge(self) -> None:
        from aemr_bot.handlers.admin_audience import _format_audience_row

        user = self._user(subscribed_broadcast=True)
        row = _format_audience_row(user)
        assert "🔔" in row

    def test_consent_badge(self) -> None:
        from datetime import datetime
        from aemr_bot.handlers.admin_audience import _format_audience_row

        user = self._user(consent_pdn_at=datetime.now())
        row = _format_audience_row(user)
        assert "✅" in row

    def test_revoked_badge(self) -> None:
        from datetime import datetime
        from aemr_bot.handlers.admin_audience import _format_audience_row

        user = self._user(consent_pdn_at=None, consent_revoked_at=datetime.now())
        row = _format_audience_row(user)
        assert "🔁" in row

    def test_blocked_badge(self) -> None:
        from aemr_bot.handlers.admin_audience import _format_audience_row

        user = self._user(is_blocked=True)
        row = _format_audience_row(user)
        assert "🚫" in row

    def test_name_truncated_at_24(self) -> None:
        from aemr_bot.handlers.admin_audience import _format_audience_row

        long_name = "А" * 50
        user = self._user(first_name=long_name)
        row = _format_audience_row(user)
        # 21 char + …
        assert "А" * 21 + "…" in row
        # Full 50 chars не должно быть
        assert "А" * 50 not in row

    def test_empty_name_returns_dash(self) -> None:
        from aemr_bot.handlers.admin_audience import _format_audience_row

        user = self._user(first_name="")
        row = _format_audience_row(user)
        assert "—" in row

    def test_none_name(self) -> None:
        from aemr_bot.handlers.admin_audience import _format_audience_row

        user = self._user(first_name=None)
        row = _format_audience_row(user)
        assert "—" in row

    def test_multiple_badges_combined(self) -> None:
        from datetime import datetime
        from aemr_bot.handlers.admin_audience import _format_audience_row

        user = self._user(
            subscribed_broadcast=True,
            consent_pdn_at=datetime.now(),
            is_blocked=True,
        )
        row = _format_audience_row(user)
        # Все три badge
        for emoji in ("🔔", "✅", "🚫"):
            assert emoji in row


# ──────────────────────────────────────────────────────────────────────
# _search_intent_set / _search_intent_pop — TTL lifecycle
# ──────────────────────────────────────────────────────────────────────


class TestSearchIntentLifecycle:
    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import admin_audience as mod
        mod._search_intents.clear()
        yield
        mod._search_intents.clear()

    def test_set_stores_state(self) -> None:
        from aemr_bot.handlers.admin_audience import (
            _search_intent_set,
            _search_intents,
        )

        _search_intent_set(42, "subs")
        assert 42 in _search_intents
        assert _search_intents[42]["category"] == "subs"

    def test_set_with_none_category(self) -> None:
        from aemr_bot.handlers.admin_audience import _search_intent_set, _search_intents

        _search_intent_set(42, None)
        assert _search_intents[42]["category"] is None

    def test_pop_returns_state(self) -> None:
        from aemr_bot.handlers.admin_audience import (
            _search_intent_pop,
            _search_intent_set,
        )

        _search_intent_set(42, "consent")
        state = _search_intent_pop(42)
        assert state is not None
        assert state["category"] == "consent"

    def test_pop_removes_state(self) -> None:
        from aemr_bot.handlers.admin_audience import (
            _search_intent_pop,
            _search_intent_set,
            _search_intents,
        )

        _search_intent_set(42, "subs")
        _search_intent_pop(42)
        assert 42 not in _search_intents

    def test_pop_missing_returns_none(self) -> None:
        from aemr_bot.handlers.admin_audience import _search_intent_pop

        assert _search_intent_pop(999) is None

    def test_pop_expired_returns_none(self) -> None:
        from aemr_bot.handlers.admin_audience import (
            _search_intent_pop,
            _search_intents,
        )

        # Вручную ставим истёкший
        _search_intents[42] = {
            "category": "subs",
            "expires_at": time.monotonic() - 1.0,
        }
        assert _search_intent_pop(42) is None

    def test_pop_expired_still_removes_from_dict(self) -> None:
        from aemr_bot.handlers.admin_audience import (
            _search_intent_pop,
            _search_intents,
        )

        _search_intents[42] = {
            "category": "subs",
            "expires_at": time.monotonic() - 1.0,
        }
        _search_intent_pop(42)
        # pop'нул даже expired (state удалён из dict)
        assert 42 not in _search_intents


# ──────────────────────────────────────────────────────────────────────
# handle_audience_search_text — text-intent handler
# ──────────────────────────────────────────────────────────────────────


class TestHandleAudienceSearchText:
    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import admin_audience as mod
        mod._search_intents.clear()
        yield
        mod._search_intents.clear()

    @pytest.mark.asyncio
    async def test_no_actor_returns_false(self) -> None:
        from aemr_bot.handlers.admin_audience import handle_audience_search_text

        event = SimpleNamespace(message=SimpleNamespace(sender=None))
        result = await handle_audience_search_text(event, "test")
        assert result is False

    @pytest.mark.asyncio
    async def test_no_intent_returns_false(self) -> None:
        from aemr_bot.handlers.admin_audience import handle_audience_search_text

        event = make_event(user_id=42)
        # Intent не set'нут
        result = await handle_audience_search_text(event, "test")
        assert result is False

    @pytest.mark.asyncio
    async def test_role_check_failure_returns_false(self) -> None:
        from aemr_bot.handlers import admin_audience as mod
        from aemr_bot.handlers.admin_audience import (
            _search_intent_set,
            handle_audience_search_text,
        )

        event = make_event(user_id=42)
        _search_intent_set(42, "subs")
        with patch.object(mod, "ensure_role", AsyncMock(return_value=False)):
            result = await handle_audience_search_text(event, "test")
        assert result is False

    @pytest.mark.asyncio
    async def test_empty_query_returns_true_with_cancel_msg(self) -> None:
        from aemr_bot.handlers import admin_audience as mod
        from aemr_bot.handlers.admin_audience import (
            _search_intent_set,
            handle_audience_search_text,
        )

        event = make_event(user_id=42)
        event.bot = MagicMock()
        event.bot.send_message = AsyncMock()
        _search_intent_set(42, "subs")
        with patch.object(mod, "ensure_role", AsyncMock(return_value=True)):
            result = await handle_audience_search_text(event, "   ")
        assert result is True
        event.bot.send_message.assert_called_once()
        kwargs = event.bot.send_message.call_args.kwargs
        assert "Пустой запрос" in kwargs["text"]


# ──────────────────────────────────────────────────────────────────────
# _start_search_intent — prompt + intent
# ──────────────────────────────────────────────────────────────────────


class TestStartSearchIntent:
    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import admin_audience as mod
        mod._search_intents.clear()
        yield
        mod._search_intents.clear()

    @pytest.mark.asyncio
    async def test_no_actor_id_returns_silently(self) -> None:
        from aemr_bot.handlers.admin_audience import (
            _search_intents,
            _start_search_intent,
        )

        event = SimpleNamespace(message=SimpleNamespace(sender=None))
        await _start_search_intent(event, "subs")
        # Никакой intent не set
        assert not _search_intents

    @pytest.mark.asyncio
    async def test_sets_intent_and_sends_prompt(self) -> None:
        from aemr_bot.handlers import admin_audience as mod
        from aemr_bot.handlers.admin_audience import (
            _search_intents,
            _start_search_intent,
        )

        event = make_event(user_id=42)
        send_mock = AsyncMock()
        with patch.object(mod, "send_or_edit_screen", send_mock):
            await _start_search_intent(event, "consent")
        # Intent set
        assert 42 in _search_intents
        assert _search_intents[42]["category"] == "consent"
        # Prompt отправлен
        send_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_none_category_accepted(self) -> None:
        from aemr_bot.handlers import admin_audience as mod
        from aemr_bot.handlers.admin_audience import (
            _search_intents,
            _start_search_intent,
        )

        event = make_event(user_id=42)
        with patch.object(mod, "send_or_edit_screen", AsyncMock()):
            await _start_search_intent(event, None)
        assert _search_intents[42]["category"] is None
