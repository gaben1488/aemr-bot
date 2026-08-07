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


async def drain_background_tasks(timeout: float = 10.0) -> list[str]:
    """Дождаться фоновых задач перед остановкой. Вернуть имена недождавшихся.

    Зачем: при выходе `asyncio.run` отменяет всё незавершённое. Без
    ожидания рестарт рвёт задачу ровно там, где она была, — в том числе
    посреди сброса буфера доставок рассылки. Такой оборванный сброс
    повторяется со следующей пачки и задваивает счётчики «доставлено»,
    от чего защищает ограничение из миграции 0023. То есть без дренажа
    мы лечим следствие, оставляя причину.

    По истечении `timeout` задачи отменяются: висящий запрос к MAX API
    не должен держать остановку бесконечно — оператор перезапускает
    контейнер и ждёт, а не смотрит на зависший процесс.

    Имена возвращаются, чтобы вызывающий записал в лог, ЧТО именно не
    доиграло: «оборвали broadcast_send» — это уже диагноз, а молчаливая
    отмена диагнозом не является.
    """
    pending = [t for t in list(_BACKGROUND_TASKS) if not t.done()]
    if not pending:
        return []

    _, still_running = await asyncio.wait(pending, timeout=timeout)
    for task in still_running:
        task.cancel()
    if still_running:
        # Даём отменённым отработать `finally` (закрыть сессию БД,
        # снять блокировку) — без этого отмена оставляет ресурсы висеть.
        await asyncio.wait(still_running, timeout=2.0)
    return [t.get_name() for t in still_running]
