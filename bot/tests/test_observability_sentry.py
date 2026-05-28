"""Тесты PII-фильтра + init_sentry для observability/sentry.py.

Sentry-sdk опционален: тестируем только нашу обвязку (PII-маскирование,
no-op без DSN), а саму sentry-sdk мокаем.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from aemr_bot.observability.sentry import (
    _before_send,
    _mask_pii,
    _scrub_event,
    init_sentry,
)


# ──────────────────────────────────────────────────────────────────────
# _mask_pii — PII-фильтр на строках
# ──────────────────────────────────────────────────────────────────────


class TestMaskPii:
    def test_empty_string(self) -> None:
        assert _mask_pii("") == ""

    def test_plain_text_unchanged(self) -> None:
        assert _mask_pii("Hello world, no PII") == "Hello world, no PII"

    def test_bare_phone_plus7(self) -> None:
        assert _mask_pii("Call +79991234567 ASAP") == "Call +7***NNNN ASAP"

    def test_bare_phone_starts_with_8(self) -> None:
        assert _mask_pii("Phone 89991234567 logged") == "Phone 8***NNNN logged"

    def test_phone_equals_pattern(self) -> None:
        assert _mask_pii("Failed: phone=+79991234567") == "Failed: phone=+7***"

    def test_phone_colon_pattern(self) -> None:
        assert _mask_pii("phone: 89991234567") == "phone: +7***"

    def test_max_user_id_pattern(self) -> None:
        assert (
            _mask_pii("Error for max_user_id=165729385 in handler")
            == "Error for max_user_id=*** in handler"
        )

    def test_user_id_without_max_prefix(self) -> None:
        assert _mask_pii("user_id=12345") == "user_id=***"

    def test_appeal_id_pattern(self) -> None:
        assert _mask_pii("appeal_id=42 not found") == "appeal_id=*** not found"

    def test_multiple_patterns_in_one_string(self) -> None:
        text = (
            "Crash in handler for max_user_id=165729385 "
            "with phone=+79991234567 — appeal_id=42"
        )
        result = _mask_pii(text)
        assert "165729385" not in result
        assert "79991234567" not in result
        assert "appeal_id=42" not in result
        assert "max_user_id=***" in result

    def test_idempotent(self) -> None:
        """Повторный mask не должен ничего менять."""
        text = "phone=+79991234567 max_user_id=165729385"
        once = _mask_pii(text)
        twice = _mask_pii(once)
        assert once == twice

    def test_short_number_not_masked(self) -> None:
        """4-5 цифр без PII-контекста — не телефон. Не маскируем."""
        assert _mask_pii("Found 1234 items") == "Found 1234 items"

    def test_case_insensitive_field_names(self) -> None:
        assert _mask_pii("PHONE=+79991234567") == "PHONE=+7***"
        assert _mask_pii("Max_User_Id=165729385") == "Max_User_Id=***"


# ──────────────────────────────────────────────────────────────────────
# _scrub_event — обход вложенной структуры Sentry event
# ──────────────────────────────────────────────────────────────────────


class TestScrubEvent:
    def test_top_level_message_masked(self) -> None:
        event = {"message": "Failed for max_user_id=12345"}
        result = _scrub_event(event)
        assert result["message"] == "Failed for max_user_id=***"

    def test_exception_values_masked(self) -> None:
        event = {
            "exception": {
                "values": [
                    {"value": "phone=+79991234567 invalid"},
                    {"value": "another error"},
                ]
            }
        }
        result = _scrub_event(event)
        assert result["exception"]["values"][0]["value"] == "phone=+7*** invalid"
        assert result["exception"]["values"][1]["value"] == "another error"

    def test_breadcrumbs_message_masked(self) -> None:
        event = {
            "breadcrumbs": {
                "values": [
                    {"message": "callback for max_user_id=12345"},
                    {"message": "no PII here"},
                ]
            }
        }
        result = _scrub_event(event)
        assert (
            result["breadcrumbs"]["values"][0]["message"]
            == "callback for max_user_id=***"
        )
        assert result["breadcrumbs"]["values"][1]["message"] == "no PII here"

    def test_breadcrumbs_data_dict_masked(self) -> None:
        event = {
            "breadcrumbs": {
                "values": [
                    {
                        "message": "DB query",
                        "data": {
                            "query": "SELECT * FROM users WHERE phone=+79991234567",
                            "rows": 5,
                        },
                    }
                ]
            }
        }
        result = _scrub_event(event)
        data = result["breadcrumbs"]["values"][0]["data"]
        assert "+79991234567" not in data["query"]
        assert data["rows"] == 5  # non-string не трогаем

    def test_extra_context_masked(self) -> None:
        event = {
            "extra": {
                "context": "max_user_id=12345",
                "level": "error",
            }
        }
        result = _scrub_event(event)
        assert result["extra"]["context"] == "max_user_id=***"
        assert result["extra"]["level"] == "error"

    def test_missing_fields_safe(self) -> None:
        """Минимальный event без всех опциональных полей."""
        result = _scrub_event({})
        assert result == {}

    def test_unexpected_types_safe(self) -> None:
        """Поля могут быть None / int / list — не должны валить scrubber."""
        event: dict = {
            "message": None,
            "exception": "not a dict",
            "breadcrumbs": {"values": None},
            "extra": [1, 2, 3],
        }
        # Не должен падать
        result = _scrub_event(event)
        assert result is event


# ──────────────────────────────────────────────────────────────────────
# _before_send — sentry hook
# ──────────────────────────────────────────────────────────────────────


class TestBeforeSend:
    def test_returns_scrubbed_event(self) -> None:
        event = {"message": "phone=+79991234567"}
        result = _before_send(event, hint={})
        assert result is not None
        assert result["message"] == "phone=+7***"

    def test_scrubber_exception_does_not_drop_event(self) -> None:
        """Если scrubber падает, event всё равно возвращается (лучше
        пропустить unscrubbed, чем потерять exception в проде)."""
        with patch(
            "aemr_bot.observability.sentry._scrub_event",
            side_effect=RuntimeError("scrubber broken"),
        ):
            event = {"message": "something"}
            result = _before_send(event, hint={})
            assert result is event


# ──────────────────────────────────────────────────────────────────────
# init_sentry — env-toggle + graceful no-op
# ──────────────────────────────────────────────────────────────────────


class TestInitSentry:
    def test_no_dsn_returns_false(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            assert init_sentry(dsn=None) is False
            assert init_sentry(dsn="") is False
        assert any("SENTRY_DSN не задан" in r.message for r in caplog.records)

    def test_missing_sentry_sdk_returns_false(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Если sentry-sdk не установлен — init возвращает False, бот
        работает без observability."""
        with patch.dict("sys.modules", {"sentry_sdk": None}):
            with caplog.at_level(logging.WARNING):
                result = init_sentry(dsn="https://x@sentry.io/1")
            assert result is False
            assert any(
                "sentry-sdk не установлен" in r.message for r in caplog.records
            )

    def test_init_with_dsn_calls_sdk(self) -> None:
        """С DSN — sentry_sdk.init вызван с правильными параметрами."""
        fake_sdk = MagicMock()
        with patch.dict("sys.modules", {"sentry_sdk": fake_sdk}):
            result = init_sentry(
                dsn="https://abc@sentry.io/42", environment="staging"
            )
            assert result is True
            fake_sdk.init.assert_called_once()
            kwargs = fake_sdk.init.call_args.kwargs
            assert kwargs["dsn"] == "https://abc@sentry.io/42"
            assert kwargs["environment"] == "staging"
            assert kwargs["send_default_pii"] is False
            assert kwargs["traces_sample_rate"] == 0.0
            # before_send hook подключён — это наш PII-фильтр.
            assert kwargs["before_send"] is not None

    def test_init_failure_returns_false(self) -> None:
        """Если sentry_sdk.init упал — init_sentry не валит бот."""
        fake_sdk = MagicMock()
        fake_sdk.init.side_effect = RuntimeError("init broken")
        with patch.dict("sys.modules", {"sentry_sdk": fake_sdk}):
            assert init_sentry(dsn="https://x@sentry.io/1") is False
