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
EXPECTED_MAXAPI_VERSION = "1.2.1"  # обновлять вместе с пином maxapi в pyproject.toml


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

    В 1.1.0 сигнатура (timeout, sock_connect, *, max_retries=3,
    retry_on_statuses, retry_backoff_factor, **kwargs). Передаём
    `max_retries` и `timeout` именованными в main.py. Если сигнатура
    снова изменится — этот тест RED'нет раньше, чем код в `main.py`
    упадёт `TypeError` в проде.
    """
    from maxapi.client.default import DefaultConnectionProperties

    sig = inspect.signature(DefaultConnectionProperties.__init__)
    params = list(sig.parameters.keys())
    assert params == [
        "self",
        "timeout",
        "sock_connect",
        "max_retries",
        "retry_on_statuses",
        "retry_backoff_factor",
        "kwargs",
    ], (
        f"Сигнатура DefaultConnectionProperties.__init__ изменилась: "
        f"{params}. Это breaking change в maxapi. Проверьте usage в "
        f"`aemr_bot/main.py` перед деплоем, обновите EXPECTED версию и "
        f"эту проверку."
    )


def test_bot_get_updates_signature_matches_polling_patch() -> None:
    """Guard на monkey-patch `bot.get_updates` в main.py.

    `_install_polling_timeout` подменяет метод на экземпляре и полагается
    на именованный параметр `timeout` (kwargs.setdefault('timeout', ...))
    и на то, что метод — coroutine. При апгрейде maxapi патч отвяжется
    МОЛЧА: таймаут перестанет проставляться, poll_watch перестанет
    отмечаться, /livez ослепнет. Если этот тест RED — обнови патч
    `_install_polling_timeout` в aemr_bot/main.py.
    """
    from maxapi import Bot

    sig = inspect.signature(Bot.get_updates)
    params = list(sig.parameters.keys())
    assert params == ["self", "limit", "timeout", "marker", "types"], (
        f"Сигнатура Bot.get_updates изменилась: {params}. Обнови "
        f"monkey-patch `_install_polling_timeout` в aemr_bot/main.py "
        f"(он полагается на kwarg `timeout`)."
    )
    assert inspect.iscoroutinefunction(Bot.get_updates), (
        "Bot.get_updates больше не coroutine — обёртка "
        "get_updates_with_timeout в aemr_bot/main.py сломается."
    )


def test_dispatcher_handle_signature_matches_dispatch_guards() -> None:
    """Guard на monkey-patch `dp.handle` в main.py.

    `_install_dispatch_guards` подменяет метод на экземпляре
    (guarded_handle) и полагается: handle — coroutine, первый аргумент —
    событие (event_object). Если maxapi переименует/уберёт метод или
    сменит форму вызова, per-user троттлинг и bounded-семафор отвяжутся
    молча — polling-путь останется без анти-флуда. Если этот тест RED —
    обнови патч `_install_dispatch_guards` в aemr_bot/main.py.
    """
    from maxapi import Dispatcher

    sig = inspect.signature(Dispatcher.handle)
    params = list(sig.parameters.keys())
    assert params == ["self", "event_object"], (
        f"Сигнатура Dispatcher.handle изменилась: {params}. Обнови "
        f"monkey-patch `_install_dispatch_guards` в aemr_bot/main.py."
    )
    assert inspect.iscoroutinefunction(Dispatcher.handle), (
        "Dispatcher.handle больше не coroutine — guarded_handle в "
        "aemr_bot/main.py сломается."
    )


def test_db_pool_ceiling_matches_dispatch_semaphore() -> None:
    """Потолок пула БД не меньше окна одновременной обработки апдейтов.

    Если semaphore пропускает больше обработчиков, чем пул может дать
    соединений, лишние молча стоят в очереди за коннектом — в пик
    (массовая подача после ЧС) это выглядит как «бот тормозит», а
    причина не видна ни в одном логе. Числа живут в разных файлах
    (main.py и db/session.py) и однажды уже разъехались: 32 против 15.
    """
    from aemr_bot import main
    from aemr_bot.db import session as db_session

    # _engine_kwargs читает settings.database_url — на юнит-окружении там
    # sqlite и pool-параметров нет. Достаём числа из исходника, это
    # честный контракт файла, а не рантайма.
    import ast
    import inspect as _inspect

    tree = ast.parse(_inspect.getsource(db_session._engine_kwargs))
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in ("pool_size", "max_overflow"):
            if isinstance(node.value, ast.Constant):
                found[node.arg] = node.value.value
    assert set(found) == {"pool_size", "max_overflow"}, (
        f"не нашёл pool_size/max_overflow в db/session.py: {found}"
    )
    ceiling = found["pool_size"] + found["max_overflow"]
    assert ceiling >= main._SEMAPHORE_CONCURRENCY, (
        f"пул БД ({found['pool_size']}+{found['max_overflow']}={ceiling}) меньше "
        f"окна обработки (main._SEMAPHORE_CONCURRENCY={main._SEMAPHORE_CONCURRENCY}) — "
        "обработчики будут стоять в очереди за соединением; выровняй числа."
    )
