"""Тесты на services/settings_store — валидация ключей и значений.

Сама БД (set_value, get) тестируется в интеграционном тесте с PG;
здесь — только pure validation/SCHEMA."""
from __future__ import annotations

import pytest

from aemr_bot.services.settings_store import SCHEMA, validate


class TestSchemaContents:
    def test_required_keys_present(self) -> None:
        """Эти ключи бот ожидает — без них будет crash при первом
        обращении. Регрессия: если кто-то удалит из SCHEMA — тест упадёт."""
        required = {
            "policy_url",
            "topics",
            "localities",
            "appointment_text",
            "emergency_contacts",
        }
        missing = required - SCHEMA.keys()
        assert not missing, f"missing in SCHEMA: {missing}"


class TestValidate:
    def test_unknown_key_rejected(self) -> None:
        ok, reason = validate("nonexistent_key_xyz", "value")
        assert ok is False
        assert "не разрешён" in reason or "unknown" in reason.lower()

    def test_string_key_accepts_string(self) -> None:
        ok, reason = validate("policy_url", "https://example.com/policy.pdf")
        assert ok is True

    def test_list_key_rejects_string(self) -> None:
        ok, reason = validate("topics", "not-a-list")
        assert ok is False

    def test_list_key_accepts_list(self) -> None:
        ok, _ = validate("topics", ["Дороги", "ЖКХ"])
        assert ok is True

    def test_localities_list(self) -> None:
        ok, _ = validate("localities", ["Елизовское ГП", "Паратунское СП"])
        assert ok is True

    def test_str_too_long_rejected(self) -> None:
        ok, reason = validate("appointment_text", "x" * 100_000)
        assert ok is False
        # max_len ограничение
        assert "длин" in reason.lower() or "max" in reason.lower()

    @pytest.mark.parametrize(
        "key,value,expected_ok",
        [
            ("emergency_contacts", [{"name": "01", "phone": "01"}], True),
            ("emergency_contacts", [{"name": "01"}], False),  # без phone
            ("emergency_contacts", [], False),  # пустой список
            ("emergency_contacts", "not-a-list", False),
        ],
    )
    def test_emergency_contacts_validation(
        self, key: str, value, expected_ok: bool
    ) -> None:
        ok, _ = validate(key, value)
        assert ok is expected_ok
