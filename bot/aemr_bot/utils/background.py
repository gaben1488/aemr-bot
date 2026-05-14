"""Запуск фоновых asyncio-задач с защитой от сборщика мусора.

Вынесено из `aemr_bot.main` (батч 4 polish). Раньше `handlers/
broadcast.py` делал `from aemr_bot.main import spawn_background_task`
внутри функции — handler импортировал точку входа приложения, явная
циклическая зависимость `handlers → main`, замаскированная lazy-
импортом. `utils/` — нижний слой без зависимостей от handlers/main,
поэтому импорт отсюда безопасен на module-level.
"""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

# Strong references к фоновым asyncio-таскам. По документации Python
# 3.11+ event loop хранит лишь слабую ссылку на task'и из
# `asyncio.create_task`, и сборщик мусора может прервать их посреди
# работы. Особенно опасно для рассылок (`_run_broadcast`) и `_recover`
# на старте. Кладём task сюда, в done_callback вычищаем, чтобы set
# не рос.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def spawn_background_task(
    coro: Coroutine[Any, Any, Any], *, name: str | None = None
) -> asyncio.Task:
    """Запустить корутину в фоне с защитой от GC."""
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task
