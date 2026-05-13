"""Юнит-тесты для чистых функций без БД.

Покрывают точечно: нормализацию телефона, vCF-парсинг, валидацию
настроек, period_window. Эти функции — точки контракта между бот и
внешними входами (текст MAX, JSON-настройка), регрессия в них тихая
и долго не замечается, а пользы от тестов много.
"""
from __future__ import annotations

import pytest

from aemr_bot.services.stats import VALID_PERIODS, period_window
from aemr_bot.services.users import _normalize_phone


# ---------- _normalize_phone ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+7 (415-31) 7-25-29", "4153172529"),
        ("89001234567", "9001234567"),
        ("79001234567", "9001234567"),
        ("+7-900-123-45-67", "9001234567"),
        ("8(900)1234567", "9001234567"),
        ("9001234567", "9001234567"),
        # 10-значный номер без кода страны — не срезаем (не 11 цифр)
        ("4153172529", "4153172529"),
        # пустые / мусор
        ("", ""),
        ("abcdef", ""),
        # ведущий 7 у иностранного 12-значного — не срезается (не 11 цифр)
        ("712345678901", "712345678901"),
    ],
)
def test_normalize_phone(raw: str, expected: str) -> None:
    assert _normalize_phone(raw) == expected


# ---------- period_window ----------


def test_period_window_today_returns_midnight_start() -> None:
    start, end, title = period_window("today")
    assert start is not None
    assert "за сегодня" in title
    assert end > start


def test_period_window_all_returns_none_start() -> None:
    start, end, title = period_window("all")
    assert start is None
    assert title == "за всё время"
    assert end is not None


@pytest.mark.parametrize("period", VALID_PERIODS)
def test_period_window_all_valid(period: str) -> None:
    start, end, title = period_window(period)
    assert isinstance(title, str) and len(title) > 0
    assert end is not None


def test_period_window_unknown_raises() -> None:
    with pytest.raises(ValueError):
        period_window("decade")


# ---------- repeat appeal context ----------


def test_apply_repeat_context_for_answered_appeal() -> None:
    from aemr_bot.db.models import AppealStatus
    from aemr_bot.handlers.appeal_runtime import _apply_repeat_context

    topic, summary = _apply_repeat_context(
        topic="Дороги",
        summary="Проблема повторилась.",
        data={
            "repeat_source_appeal_id": 15,
            "repeat_source_status": AppealStatus.ANSWERED.value,
        },
    )

    assert topic == "Обратная связь по отвеченному вопросу: Дороги"
    assert "Связано с обращением #15" in summary
    assert "Проблема повторилась." in summary


def test_apply_repeat_context_without_source_keeps_texts() -> None:
    from aemr_bot.handlers.appeal_runtime import _apply_repeat_context

    topic, summary = _apply_repeat_context(
        topic="ЖКХ",
        summary="Текст обращения.",
        data={},
    )

    assert topic == "ЖКХ"
    assert summary == "Текст обращения."


# ---------- settings_store.validate ----------


def test_validate_unknown_key_rejected() -> None:
    from aemr_bot.services import settings_store

    ok, _ = settings_store.validate("not_a_real_key", "x")
    assert ok is False


def test_validate_url_must_start_with_scheme() -> None:
    from aemr_bot.services import settings_store

    ok_https, _ = settings_store.validate("policy_url", "https://example.com")
    ok_http, _ = settings_store.validate("policy_url", "http://example.com")
    ok_javascript, _ = settings_store.validate("policy_url", "javascript:alert(1)")
    ok_relative, _ = settings_store.validate("policy_url", "/policy")
    assert ok_https is True
    assert ok_http is True
    assert ok_javascript is False
    assert ok_relative is False


def test_validate_string_length() -> None:
    from aemr_bot.services import settings_store

    ok_short, _ = settings_store.validate("appointment_text", "x")
    ok_normal, _ = settings_store.validate("appointment_text", "x" * 200)
    ok_too_long, _ = settings_store.validate("appointment_text", "x" * 3000)
    assert ok_short is True  # min_len=1, "x" длиной 1 проходит
    assert ok_normal is True
    assert ok_too_long is False


def test_validate_list_topics() -> None:
    from aemr_bot.services import settings_store

    ok_normal, _ = settings_store.validate("topics", ["тема А", "тема Б"])
    ok_empty, _ = settings_store.validate("topics", [])
    ok_wrong_type, _ = settings_store.validate("topics", "не список")
    assert ok_normal is True
    assert ok_empty is False  # min_items=1
    assert ok_wrong_type is False


def test_validate_emergency_contacts_shape() -> None:
    from aemr_bot.services import settings_store

    ok, _ = settings_store.validate(
        "emergency_contacts",
        [{"name": "ЕДДС", "phone": "112"}],
    )
    bad_missing_phone, _ = settings_store.validate(
        "emergency_contacts",
        [{"name": "ЕДДС"}],
    )
    bad_not_object, _ = settings_store.validate(
        "emergency_contacts",
        ["просто строка"],
    )
    assert ok is True
    assert bad_missing_phone is False
    assert bad_not_object is False


# ---------- extract_phone (vCF / contact attachment) ----------


def test_extract_phone_from_max_info_dict() -> None:
    from aemr_bot.utils.attachments import extract_phone

    body = type(
        "FakeBody",
        (),
        {
            "attachments": [
                {
                    "type": "contact",
                    "payload": {
                        "max_info": {"phone": "+79001234567"},
                    },
                },
            ],
        },
    )()
    assert extract_phone(body) == "+79001234567"


def test_extract_phone_from_vcf_info_text() -> None:
    """vcf_info — это строка vCard с TEL-полем. Парсер должен достать
    номер из произвольного TEL-параметра."""
    from aemr_bot.utils.attachments import extract_phone

    body = type(
        "FakeBody",
        (),
        {
            "attachments": [
                {
                    "type": "contact",
                    "payload": {
                        "vcf_info": (
                            "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Иванов\r\n"
                            "TEL;TYPE=CELL:+79001234567\r\nEND:VCARD"
                        ),
                    },
                },
            ],
        },
    )()
    assert extract_phone(body) == "+79001234567"


def test_extract_phone_returns_none_when_no_contact() -> None:
    from aemr_bot.utils.attachments import extract_phone

    body = type(
        "FakeBody",
        (),
        {
            "attachments": [
                {"type": "image", "payload": {"url": "x"}},
            ],
        },
    )()
    assert extract_phone(body) is None


def test_extract_contact_name_from_vcf_info() -> None:
    from aemr_bot.utils.attachments import extract_contact_name

    body = type(
        "FakeBody",
        (),
        {
            "attachments": [
                {
                    "type": "contact",
                    "payload": {
                        "vcf_info": "BEGIN:VCARD\r\nFN:Иванов Иван\r\nEND:VCARD",
                    },
                },
            ],
        },
    )()
    assert extract_contact_name(body) == "Иванов Иван"


def test_extract_contact_name_from_max_info() -> None:
    from aemr_bot.utils.attachments import extract_contact_name

    body = type(
        "FakeBody",
        (),
        {
            "attachments": [
                {
                    "type": "contact",
                    "payload": {
                        "max_info": {"first_name": "Алексей"},
                    },
                },
            ],
        },
    )()
    assert extract_contact_name(body) == "Алексей"


# ---------- calendar_ru.is_workday / is_holiday ----------


def test_is_holiday_includes_loaded_dates(monkeypatch, tmp_path) -> None:
    """Загружает фейковый seed/holidays.json и проверяет, что 9 мая
    распознаётся как праздник, а 7 мая (рабочий чт) — нет."""
    from datetime import date

    import aemr_bot.services.calendar_ru as cal

    fake = tmp_path / "holidays.json"
    fake.write_text('{"2026": ["2026-05-09", "2026-05-11"]}', encoding="utf-8")
    monkeypatch.setattr(cal, "HOLIDAYS_PATH", fake)
    cal._load_holidays.cache_clear()

    assert cal.is_holiday(date(2026, 5, 9))
    assert cal.is_holiday(date(2026, 5, 11))
    assert not cal.is_holiday(date(2026, 5, 7))


def test_is_workday_handles_sunday_and_holiday(monkeypatch, tmp_path) -> None:
    from datetime import date

    import aemr_bot.services.calendar_ru as cal

    fake = tmp_path / "holidays.json"
    fake.write_text('{"2026": ["2026-05-09"]}', encoding="utf-8")
    monkeypatch.setattr(cal, "HOLIDAYS_PATH", fake)
    cal._load_holidays.cache_clear()

    # 2026-05-10 — воскресенье
    assert not cal.is_workday(date(2026, 5, 10))
    # 2026-05-09 — суббота, но праздник (День Победы)
    assert not cal.is_workday(date(2026, 5, 9))
    # 2026-05-12 — обычный вторник
    assert cal.is_workday(date(2026, 5, 12))


def test_is_workday_falls_back_when_holidays_missing(monkeypatch, tmp_path) -> None:
    """Без файла holidays.json считаем рабочими все дни кроме воскресенья.
    Это безопасный fallback: лучше избыточный reminder чем тишина в
    рабочий день."""
    from datetime import date
    from pathlib import Path

    import aemr_bot.services.calendar_ru as cal

    monkeypatch.setattr(cal, "HOLIDAYS_PATH", Path(tmp_path) / "missing.json")
    cal._load_holidays.cache_clear()

    assert cal.is_workday(date(2026, 5, 9))   # суббота — рабочий
    assert not cal.is_workday(date(2026, 5, 10))  # воскресенье — нет
