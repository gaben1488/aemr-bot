"""Тесты services-функций, не требующих БД и maxapi.

Покрываем pure-функции:
- stats.period_window — для всех валидных периодов и невалидного
- stats._status_label — маппинг статусов
- settings_store DEFAULTS / SCHEMA консистентность
- idempotency.build_idempotency_key — все ветви (cb, mid, seq, ts, chat, user)
- users._normalize_phone — все формы (+7, 8, без префикса, мусор)
- broadcasts._eligible_filter — компилируется в SQL без exception
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


class TestPeriodWindow:
    def test_today(self) -> None:
        from aemr_bot.services.stats import period_window

        start, end, title = period_window("today")
        assert start is not None
        assert end > start
        assert "сегодня" in title

    def test_week(self) -> None:
        from aemr_bot.services.stats import period_window

        start, end, title = period_window("week")
        assert start is not None
        # 7 дней
        assert (end - start).days >= 6
        assert "7 дней" in title

    def test_month(self) -> None:
        from aemr_bot.services.stats import period_window

        start, end, title = period_window("month")
        assert "30 дней" in title

    def test_quarter(self) -> None:
        from aemr_bot.services.stats import period_window

        start, end, title = period_window("quarter")
        assert "квартал" in title

    def test_half_year(self) -> None:
        from aemr_bot.services.stats import period_window

        start, end, title = period_window("half_year")
        assert "полгода" in title

    def test_year(self) -> None:
        from aemr_bot.services.stats import period_window

        start, end, title = period_window("year")
        assert "год" in title

    def test_all_returns_none_start(self) -> None:
        from aemr_bot.services.stats import period_window

        start, end, title = period_window("all")
        assert start is None
        assert "всё время" in title

    def test_unknown_raises(self) -> None:
        from aemr_bot.services.stats import period_window

        with pytest.raises(ValueError):
            period_window("unknown")


class TestStatusLabel:
    def test_known_status_label(self) -> None:
        from aemr_bot.db.models import AppealStatus
        from aemr_bot.services.stats import _status_label

        assert _status_label(AppealStatus.NEW.value) == "Новое"
        assert _status_label(AppealStatus.IN_PROGRESS.value) == "В работе"
        assert _status_label(AppealStatus.ANSWERED.value) == "Завершено"
        assert _status_label(AppealStatus.CLOSED.value) == "Закрыто"

    def test_unknown_status_returns_raw(self) -> None:
        from aemr_bot.services.stats import _status_label

        assert _status_label("custom") == "custom"


class TestSettingsStoreDefaults:
    def test_all_schema_keys_have_defaults(self) -> None:
        from aemr_bot.services.settings_store import DEFAULTS, SCHEMA

        for key in SCHEMA:
            assert key in DEFAULTS, f"SCHEMA key '{key}' missing from DEFAULTS"

    def test_defaults_are_valid_per_schema(self) -> None:
        """Каждый дефолт-значение должен валидным согласно SCHEMA — иначе
        seed_if_empty запишет в БД невалидное значение."""
        from aemr_bot.services.settings_store import DEFAULTS, SCHEMA, validate

        for key, default in DEFAULTS.items():
            if key not in SCHEMA:
                continue
            if default is None:
                # Welcome/consent_text имеют None по умолчанию — ок.
                continue
            # Списки могут быть пустыми (emergency_contacts, topics, transport_dispatcher_contacts).
            # Они не пройдут min_items=1, но это ожидаемое состояние «не настроено».
            # Тогда пропускаем такие записи.
            rule = SCHEMA[key]
            if rule.get("type") is list and rule.get("min_items", 0) > 0 and len(default) == 0:
                continue
            ok, _ = validate(key, default)
            assert ok, f"Default value for '{key}' fails its own SCHEMA: {default!r}"

    def test_localities_default_has_known_settlements(self) -> None:
        from aemr_bot.services.settings_store import DEFAULTS

        assert "Елизовское ГП" in DEFAULTS["localities"]
        assert "Паратунское СП" in DEFAULTS["localities"]

    def test_policy_url_is_https(self) -> None:
        from aemr_bot.services.settings_store import DEFAULTS

        assert DEFAULTS["policy_url"].startswith("https://")


class TestBuildIdempotencyKey:
    def test_callback_event(self) -> None:
        from aemr_bot.services.idempotency import build_idempotency_key

        event = SimpleNamespace(
            update_type="message_callback",
            callback=SimpleNamespace(callback_id="CB-123"),
            message=None,
            timestamp=1000,
            chat_id=42,
            user=None,
        )
        key = build_idempotency_key(event)
        assert key is not None
        assert "cb=CB-123" in key
        assert "ts=1000" in key
        assert "chat=42" in key

    def test_message_with_mid_and_seq(self) -> None:
        from aemr_bot.services.idempotency import build_idempotency_key

        event = SimpleNamespace(
            update_type="message_created",
            callback=None,
            message=SimpleNamespace(
                body=SimpleNamespace(mid="M-1", seq=42, text="hi"),
                timestamp=2000,
            ),
            timestamp=None,
            chat_id=10,
            user=SimpleNamespace(user_id=99),
        )
        key = build_idempotency_key(event)
        assert key is not None
        assert "mid=M-1" in key
        assert "seq=42" in key
        assert "ts=2000" in key
        assert "user=99" in key

    def test_no_useful_fields_returns_none(self) -> None:
        from aemr_bot.services.idempotency import build_idempotency_key

        event = SimpleNamespace(
            update_type="bot_started",
            callback=None,
            message=None,
            timestamp=None,
            chat_id=None,
            user=None,
        )
        # Только update_type → ключ слишком слабый, возвращаем None.
        assert build_idempotency_key(event) is None

    def test_truncates_to_max_length(self) -> None:
        from aemr_bot.services.idempotency import (
            MAX_KEY_LENGTH,
            build_idempotency_key,
        )

        # Длинный mid поверх лимита.
        long_mid = "M" * 1000
        event = SimpleNamespace(
            update_type="message_created",
            callback=None,
            message=SimpleNamespace(
                body=SimpleNamespace(mid=long_mid, seq=None, text=""),
                timestamp=None,
            ),
            timestamp=None,
            chat_id=None,
            user=None,
        )
        key = build_idempotency_key(event)
        assert key is not None
        assert len(key) <= MAX_KEY_LENGTH

    def test_uses_class_name_when_no_update_type(self) -> None:
        from aemr_bot.services.idempotency import build_idempotency_key

        class FakeEvent:
            update_type = None
            callback = None
            message = SimpleNamespace(
                body=SimpleNamespace(mid="M-1", seq=None, text=""),
                timestamp=None,
            )
            timestamp = None
            chat_id = None
            user = None

        ev = FakeEvent()
        key = build_idempotency_key(ev)
        assert key is not None
        assert key.startswith("FakeEvent")


class TestUsersNormalizePhone:
    def test_keeps_only_digits(self) -> None:
        from aemr_bot.services.users import _normalize_phone

        assert _normalize_phone("+7 (415-31) 7-25-29") == "4153172529"

    def test_strips_leading_seven(self) -> None:
        from aemr_bot.services.users import _normalize_phone

        assert _normalize_phone("79001234567") == "9001234567"

    def test_strips_leading_eight(self) -> None:
        from aemr_bot.services.users import _normalize_phone

        assert _normalize_phone("89001234567") == "9001234567"

    def test_short_number_kept_as_is(self) -> None:
        from aemr_bot.services.users import _normalize_phone

        assert _normalize_phone("12345") == "12345"

    def test_empty(self) -> None:
        from aemr_bot.services.users import _normalize_phone

        assert _normalize_phone("") == ""
        assert _normalize_phone("---") == ""


class TestBroadcastsEligibleFilter:
    def test_compiles_to_sql(self) -> None:
        """_eligible_filter должно скомпилироваться в SQLAlchemy-выражение
        без исключений. Эмитимое SQL содержит все четыре условия."""
        from aemr_bot.services.broadcasts import _eligible_filter

        expr = _eligible_filter()
        compiled = str(expr.compile(compile_kwargs={"literal_binds": True}))
        assert "subscribed_broadcast" in compiled
        assert "consent_broadcast_at" in compiled
        assert "is_blocked" in compiled
        assert "first_name" in compiled
