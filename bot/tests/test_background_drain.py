"""Дренаж фоновых задач перед остановкой бота.

Почему это важно: без ожидания `asyncio.run` обрывает незавершённые
задачи на выходе — в том числе сброс буфера доставок рассылки. Такой
оборванный сброс повторяется со следующей пачки и задваивает счётчики
«доставлено», от чего защищает ограничение из миграции 0023. Дренаж
закрывает причину, ограничение — следствие; тесты ниже сторожат
причину.
"""
from __future__ import annotations

import asyncio

import pytest

from aemr_bot.utils.background import (
    _BACKGROUND_TASKS,
    drain_background_tasks,
    spawn_background_task,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Реестр глобальный — чистим до и после, чтобы тесты не влияли
    друг на друга и не ловили чужие задачи из соседних модулей."""
    _BACKGROUND_TASKS.clear()
    yield
    _BACKGROUND_TASKS.clear()


@pytest.mark.asyncio
async def test_waits_for_task_to_finish() -> None:
    """Незавершённая задача успевает доиграть, а не обрывается.

    Если это сломается, рестарт снова будет рвать сброс доставок
    посреди записи.
    """
    done: list[str] = []

    async def slow_write() -> None:
        await asyncio.sleep(0.05)
        done.append("written")

    spawn_background_task(slow_write(), name="slow_write")
    abandoned = await drain_background_tasks(timeout=2.0)

    assert done == ["written"], "задача обязана доиграть до конца"
    assert abandoned == [], "никого не бросили — списку неоткуда взяться"


@pytest.mark.asyncio
async def test_hung_task_cancelled_and_named() -> None:
    """Зависшая задача отменяется по таймауту, её имя возвращается.

    Остановка бота не должна ждать вечно висящий запрос к MAX API:
    оператор перезапускает контейнер и ждёт. Но и молчать нельзя —
    имя уходит в лог, чтобы было видно, ЧТО именно оборвали.
    """
    async def never_ends() -> None:
        await asyncio.sleep(3600)

    task = spawn_background_task(never_ends(), name="broadcast_send")
    abandoned = await drain_background_tasks(timeout=0.05)

    assert abandoned == ["broadcast_send"], "имя недождавшейся задачи в отчёте"
    assert task.cancelled() or task.done(), "зависшая задача обязана быть снята"


@pytest.mark.asyncio
async def test_cancelled_task_runs_its_cleanup() -> None:
    """У отменённой задачи отрабатывает finally — сессия БД закрывается.

    Без второго ожидания после cancel() ресурсы остались бы висеть:
    отмена лишь возбуждает CancelledError, но не гарантирует, что
    задача успела выполнить свой блок очистки.
    """
    cleaned: list[str] = []

    async def with_cleanup() -> None:
        try:
            await asyncio.sleep(3600)
        finally:
            cleaned.append("closed")

    spawn_background_task(with_cleanup(), name="with_cleanup")
    await drain_background_tasks(timeout=0.05)

    assert cleaned == ["closed"], "блок очистки обязан отработать после отмены"


@pytest.mark.asyncio
async def test_no_tasks_is_cheap_noop() -> None:
    """Пустой реестр — мгновенный выход без ожидания таймаута."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await drain_background_tasks(timeout=5.0) == []
    assert loop.time() - started < 0.5, "пустой дренаж не должен ждать таймаут"
