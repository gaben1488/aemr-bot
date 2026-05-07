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
