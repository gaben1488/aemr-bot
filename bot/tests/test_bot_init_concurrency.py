"""Регрессионные тесты на корневой фикс hang'а при тапах (PR fix-hang).

Корень: maxapi по умолчанию делает sequential dispatch в polling
(`await self.handle(event)` без `use_create_task`) и держит HTTP-таймаут
150 сек × 3 retry. Один тормозящий запрос к MAX блокировал обработку
всех следующих тапов.

Эти тесты фиксируют:
- `dp.use_create_task is True` — concurrent dispatch обработчиков;
- `bot.default_connection.max_retries == settings.max_api_retries` —
  retries контролируются нашим конфигом, не дефолтом maxapi;
- `bot.default_connection.timeout.total == settings.max_api_timeout_seconds` —
  таймаут наш, не 150 сек дефолт.

Если кто-то снимет use_create_task или вернёт maxapi-дефолты — этот
тест RED'нет, hang вернётся.
"""
from __future__ import annotations

import pytest


pytest.importorskip("maxapi", reason="maxapi нужен для тестов config bot/dispatcher")


def test_dispatcher_uses_create_task_for_concurrent_dispatch() -> None:
    """Без use_create_task один долгий callback блокирует polling loop."""
    from aemr_bot.main import dp

    assert dp.use_create_task is True, (
        "Dispatcher должен запускаться с use_create_task=True, иначе "
        "при тапе на кнопку весь polling блокируется на время "
        "обработки одного события — это корень hang'а."
    )


def test_bot_http_timeout_is_below_maxapi_default() -> None:
    """maxapi-дефолт 150 сек слишком долгий — должен быть наш конфиг."""
    from aemr_bot.config import settings as cfg
    from aemr_bot.main import bot

    expected = cfg.max_api_timeout_seconds
    actual = bot.default_connection.timeout.total

    assert actual == pytest.approx(expected), (
        f"bot.default_connection.timeout.total={actual}, ожидаемое "
        f"settings.max_api_timeout_seconds={expected}. "
        f"maxapi-дефолт 150 сек ловить нельзя — будут зависания."
    )
    # Сам конфиг тоже проверим: должен быть строго меньше 150 сек.
    assert expected < 150, (
        f"max_api_timeout_seconds={expected} ≥ 150 — это maxapi-дефолт, "
        f"hang при медленном MAX вернётся."
    )


def test_bot_http_retries_are_capped() -> None:
    """maxapi-дефолт 3 retry с backoff — слишком долго для интерактивных
    callbacks. Должен быть наш cap."""
    from aemr_bot.config import settings as cfg
    from aemr_bot.main import bot

    expected = cfg.max_api_retries
    actual = bot.default_connection.max_retries

    assert actual == expected, (
        f"bot.default_connection.max_retries={actual}, ожидаемое "
        f"settings.max_api_retries={expected}."
    )
    assert expected <= 2, (
        f"max_api_retries={expected} > 2 — каскадный backoff раздувает "
        f"время ответа до десятков секунд, оператор воспринимает как hang."
    )
