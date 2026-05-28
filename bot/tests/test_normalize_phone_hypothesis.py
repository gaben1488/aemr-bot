"""Property-based тесты `services/users._normalize_phone`.

Дополняют example-based тесты Hypothesis-генерацией edge cases:
юникод, длинные строки, неожиданные комбинации цифр и не-цифр.

Инварианты, которые должны выполняться **на любом входе** (валидном
или нет):

1. **Output — только цифры.** `_normalize_phone(x)` всегда возвращает
   строку из символов `[0-9]` (или пустую). Это контракт для
   `phone_normalized` колонки в БД.
2. **Idempotent.** `_normalize_phone(_normalize_phone(x))` ==
   `_normalize_phone(x)`. Двойная нормализация безопасна.
3. **Длина ≤ digit-count входа.** Output не может быть длиннее, чем
   количество цифровых символов на входе. Country code strip может
   только уменьшить.
4. **Country code strip срабатывает только на 11-значных РФ-форматах.**
   Если input даёт ровно 11 цифр начиная с 7 или 8 → output 10 цифр
   (без первой). Иначе output = все цифры.
5. **Не падает на любом string-input.** Защита от unsanitized
   пользовательского ввода в боте.
"""
from __future__ import annotations

import string

from hypothesis import given, strategies as st

from aemr_bot.services.users import _normalize_phone


# ──────────────────────────────────────────────────────────────────────
# Инвариант 1 — output только из цифр
# ──────────────────────────────────────────────────────────────────────


@given(st.text())
def test_output_is_digits_only(phone: str) -> None:
    """На любом входе output — только цифры или пустая строка."""
    result = _normalize_phone(phone)
    assert isinstance(result, str)
    assert all(ch.isdigit() for ch in result), (
        f"Output {result!r} содержит не-цифры (input {phone!r})"
    )


# ──────────────────────────────────────────────────────────────────────
# Инвариант 2 — idempotent
# ──────────────────────────────────────────────────────────────────────


@given(st.text())
def test_idempotent(phone: str) -> None:
    """Двойная нормализация даёт тот же результат."""
    once = _normalize_phone(phone)
    twice = _normalize_phone(once)
    assert once == twice, (
        f"Не idempotent: {phone!r} → {once!r} → {twice!r}"
    )


# ──────────────────────────────────────────────────────────────────────
# Инвариант 3 — длина output ≤ digit-count input
# ──────────────────────────────────────────────────────────────────────


@given(st.text())
def test_length_does_not_exceed_input_digits(phone: str) -> None:
    """Output не может быть длиннее, чем цифр на входе.

    Country code strip может только уменьшить длину; добавления
    символов нет.
    """
    input_digits = sum(1 for ch in phone if ch.isdigit())
    result = _normalize_phone(phone)
    assert len(result) <= input_digits, (
        f"Output {result!r} ({len(result)} chars) длиннее, чем input "
        f"digits ({input_digits}). Input: {phone!r}"
    )


# ──────────────────────────────────────────────────────────────────────
# Инвариант 4 — country code strip на 11-значных РФ-форматах
# ──────────────────────────────────────────────────────────────────────


# Генератор валидной 10-значной части номера (без code).
_TEN_DIGITS = st.text(alphabet="0123456789", min_size=10, max_size=10)


@given(_TEN_DIGITS, st.sampled_from(["7", "8"]))
def test_country_code_stripped_for_russian(
    ten_digits: str, country_code: str,
) -> None:
    """Input '7XXXXXXXXXX' или '8XXXXXXXXXX' → output 'XXXXXXXXXX'."""
    input_phone = country_code + ten_digits
    result = _normalize_phone(input_phone)
    assert result == ten_digits, (
        f"Country code не снят: {input_phone!r} → {result!r}, "
        f"ожидалось {ten_digits!r}"
    )


@given(_TEN_DIGITS, st.sampled_from(["7", "8"]))
def test_country_code_stripped_with_formatting(
    ten_digits: str, country_code: str,
) -> None:
    """Форматирование (пробелы, скобки, дефисы) перед нормализацией
    не должно ломать strip country code."""
    # Имитируем то, что MAX-кнопка контакта или оператор вводит:
    # «+7 (XXX) XXX-XX-XX» или «8-XXX-XXX-XX-XX».
    formatted = (
        f"+{country_code} ({ten_digits[:3]}) "
        f"{ten_digits[3:6]}-{ten_digits[6:8]}-{ten_digits[8:]}"
    )
    result = _normalize_phone(formatted)
    assert result == ten_digits, (
        f"Strip с форматированием не работает: "
        f"{formatted!r} → {result!r}, ожидалось {ten_digits!r}"
    )


# ──────────────────────────────────────────────────────────────────────
# Инвариант 5 — не падает на любом string-input
# ──────────────────────────────────────────────────────────────────────


@given(st.text(alphabet=string.printable))
def test_handles_all_printable_input(phone: str) -> None:
    """Любой printable-input не должен валить функцию."""
    # Просто вызов без exception — главное, что не падает.
    result = _normalize_phone(phone)
    assert isinstance(result, str)


@given(st.text(alphabet="абвгдеёжзийклмнопрстуфхцчшщъыьэюя.,-+"))
def test_handles_cyrillic_input(phone: str) -> None:
    """Кириллица + типичные разделители не валят функцию."""
    result = _normalize_phone(phone)
    assert result == "", (
        f"Кириллица не должна давать цифры в output: "
        f"{phone!r} → {result!r}"
    )


# ──────────────────────────────────────────────────────────────────────
# Edge-case sanity (не Hypothesis, явные)
# ──────────────────────────────────────────────────────────────────────


def test_empty_string() -> None:
    assert _normalize_phone("") == ""


def test_all_non_digits() -> None:
    assert _normalize_phone("abcdefg") == ""
    assert _normalize_phone("+++---") == ""


def test_short_number_not_stripped() -> None:
    """5 цифр — не РФ-формат, country code не снимаем."""
    assert _normalize_phone("12345") == "12345"


def test_twelve_digits_not_stripped() -> None:
    """12 цифр — не классический РФ-формат, оставляем все."""
    result = _normalize_phone("712345678901")
    assert result == "712345678901"


def test_eleven_digits_starting_with_9_not_stripped() -> None:
    """11 цифр НЕ начинающихся с 7 или 8 — не РФ-format, не трогаем."""
    result = _normalize_phone("91234567890")
    assert result == "91234567890"
