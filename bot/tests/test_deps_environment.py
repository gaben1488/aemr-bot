"""Guard на drift между локальной/CI/Docker средой и uv.lock.

Контекст (PR #48-#50): я тестировал локально на maxapi 1.0.0, а в Docker
0.9.18 — деплой положил бота `TypeError: ClientSession got max_retries`.
Корень — локальный pip-install мимо uv → системный maxapi 1.0.0; CI
использовал тот же системный python.

Этот тест RED'нет, если установленная версия maxapi (и его сигнатура
ключевого API) расходится с тем, что закреплено в `uv.lock` для prod.
Запускать обязан только через `uv run pytest` — тогда .venv совпадает
с lock'ом, тест зелёный. Через системный python с другой версией —
тест красный, явно показывает корень.
"""
from __future__ import annotations

import inspect

import pytest


pytest.importorskip("maxapi", reason="maxapi нужен для проверки версии и API")


# Закреплённая prod-версия из uv.lock. При апгрейде maxapi:
# 1. Бамп `maxapi~=X` в pyproject.toml
# 2. `uv lock --upgrade-package maxapi`
# 3. Поднять `EXPECTED_MAXAPI_VERSION` ниже
# 4. Тесты + ручной smoke + Dockerfile rebuild перед merge
EXPECTED_MAXAPI_VERSION = "0.9.18"


def _installed_maxapi_version() -> str:
    """Достать установленную версию через importlib.metadata."""
    from importlib.metadata import version

    return version("maxapi")


def test_maxapi_version_matches_lock() -> None:
    """Установленная maxapi == prod (из uv.lock).

    Drift = риск «у меня работает / на проде падает». Чинить либо
    `uv sync` (downgrade локально), либо осознанным апгрейдом через
    процедуру в docs/DEPS.md.
    """
    actual = _installed_maxapi_version()
    assert actual == EXPECTED_MAXAPI_VERSION, (
        f"maxapi {actual} установлена локально, но prod = "
        f"{EXPECTED_MAXAPI_VERSION} (из uv.lock).\n"
        f"Запускайте тесты через `uv run pytest`, либо "
        f"`cd bot && uv sync --extra dev` чтобы синхронизировать .venv.\n"
        f"Если намеренный апгрейд — следуйте docs/DEPS.md."
    )


def test_default_connection_signature_matches_prod_api() -> None:
    """Guard на ключевую API surface: DefaultConnectionProperties.__init__.

    В 0.9.18 сигнатура (timeout, sock_connect, **kwargs) — `max_retries`
    НЕ принимается как именованный. Если внезапно появится maxapi 1.x
    с другой сигнатурой и автомат поставит её, этот тест RED'нет
    раньше, чем код в `main.py` упадёт `TypeError` в проде.
    """
    from maxapi.client.default import DefaultConnectionProperties

    sig = inspect.signature(DefaultConnectionProperties.__init__)
    params = list(sig.parameters.keys())
    assert params == ["self", "timeout", "sock_connect", "kwargs"], (
        f"Сигнатура DefaultConnectionProperties.__init__ изменилась: "
        f"{params}. Это breaking change в maxapi. Проверьте usage в "
        f"`aemr_bot/main.py` перед деплоем, обновите EXPECTED версию и "
        f"эту проверку."
    )
