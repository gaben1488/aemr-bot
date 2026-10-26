"""Хранилище мутабельного in-memory состояния визардов и reply-intent'а
оператора (op_wizard, broadcast_wizard, reply_intent).

Не вносит логику, только хранение — бизнес-логика остаётся в handlers.

Фактический прод-контур (что реально читается вне тестов):
- reply-intent: `set_reply_intent` / `get_reply_intent` / `drop_reply_intent`
  — `handlers/operator_reply` (через wrapper-функции) и
  `handlers/admin_appeal_ops`.
- op/broadcast wizard persistence: `schedule_persist_op` вызывается из
  `admin_operators` при set/clear, `schedule_persist_broadcast` — из
  `handlers/broadcast_wizard` (там же поддерживается зеркало через
  `set_broadcast_wizard` / `clear_broadcast_wizard`). Словари
  `_op_wizards` / `_broadcast_wizards` наполняются на старте бота из БД
  через `services/wizard_persist.hydrate_into_registry`, а main.py
  раскладывает восстановленное по словарям самих handler'ов.

Глобальный сброс при `/cancel` живёт НЕ здесь: `handlers/appeal`
явными pop'ами гасит каждое hand­ler-локальное хранилище (broadcast
`_wizards`, op `_op_wizards`, reply-intent, settings `_edit_intents`,
templates `_wizards`, audience `_search_intents`). Прежний агрегатор
`clear_all_for` был мёртв и сломан-by-design (бил по чужим dict'ам) —
удалён.

Мотивация (рефакторинг SLF001, testability): см.
`docs/_meta/_archive/CODE_DECISIONS_LOG.md §5`.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Coroutine
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any

from aemr_bot.db.session import session_scope
from aemr_bot.services import wizard_persist
from aemr_bot.utils.background import spawn_background_task

log = logging.getLogger(__name__)

# ---- Внутреннее хранилище (закрытое для других модулей) ---------------------

# Wizard «Добавить оператора» в админ-панели IT.
# Ключ: max_user_id оператора, который сейчас ведёт wizard.
# Значение: dict со step ('awaiting_id'|'awaiting_role'|'awaiting_name'),
# target_id (int|None), role (OperatorRole|None), full_name (str|None).
_op_wizards: dict[int, dict[str, Any]] = {}

# Wizard рассылки.
# Ключ: max_user_id оператора. Значение: dict со step ('awaiting_text',
# 'awaiting_confirm'), text (str|None), preview_message_id (str|None).
_broadcast_wizards: dict[int, dict[str, Any]] = {}

# Активный intent оператора «отвечаю на обращение N» — короткоживущий,
# с TTL ~5 минут. Без TTL любой следующий текст оператора уходил бы
# жителю прошлого обращения. См. handlers/operator_reply.py.
# Ключ: max_user_id оператора.
# Значение: (appeal_id, is_final, set_at_ts):
#   - is_final=True — финальный ответ, обращение → ANSWERED.
#   - is_final=False — промежуточный ответ, обращение остаётся
#     IN_PROGRESS. Для диалогов/уточнений (см. PR intermediate-reply).
_reply_intent: dict[int, tuple[int, bool, float]] = {}


# ---- Public API: op_wizard ------------------------------------------------


def get_op_wizard(operator_id: int) -> dict[str, Any] | None:
    return _op_wizards.get(operator_id)


def set_op_wizard(operator_id: int, state: dict[str, Any]) -> None:
    _op_wizards[operator_id] = state


def update_op_wizard(operator_id: int, **patch: Any) -> dict[str, Any]:
    state = _op_wizards.setdefault(operator_id, {})
    state.update(patch)
    return state


def clear_op_wizard(operator_id: int) -> None:
    _op_wizards.pop(operator_id, None)


# ---- Public API: broadcast wizard ----------------------------------------


def get_broadcast_wizard(operator_id: int) -> dict[str, Any] | None:
    return _broadcast_wizards.get(operator_id)


def set_broadcast_wizard(operator_id: int, state: dict[str, Any]) -> None:
    _broadcast_wizards[operator_id] = state


def clear_broadcast_wizard(operator_id: int) -> None:
    _broadcast_wizards.pop(operator_id, None)


# ---- Public API: reply intent --------------------------------------------


def get_reply_intent(operator_id: int) -> tuple[int, bool, float] | None:
    """Вернуть (appeal_id, is_final, expires_at) или None если intent
    отсутствует/протух."""
    return _reply_intent.get(operator_id)


def set_reply_intent(
    operator_id: int,
    appeal_id: int,
    ts: float,
    *,
    is_final: bool = True,
) -> None:
    _reply_intent[operator_id] = (appeal_id, is_final, ts)


def drop_reply_intent(operator_id: int) -> None:
    _reply_intent.pop(operator_id, None)


# ---- Public API: bulk operations -----------------------------------------


def reset_all() -> None:
    """Обнулить ВСЁ состояние. Только для unit-тестов между case'ами."""
    _op_wizards.clear()
    _broadcast_wizards.clear()
    _reply_intent.clear()


# ---- Persistence hooks (миграция 0011) -----------------------------------
#
# Best-effort fire-and-forget сохранение wizard state в БД через
# services/wizard_persist. Зачем: in-memory dict'ы выше — primary cache,
# но рестарт бота терял state. Эти хуки сохраняют state в Postgres
# таблицу wizard_state без блокировки caller'а.
#
# Дизайн: handlers вызывают schedule_persist_* после изменения состояния.
# Эти хуки spawn'ят background task, который запишет в БД.
# На старте бота `wizard_persist.hydrate_*`
# подгружает обратно в in-memory.
#
# Если БД-запись упала — лог warning, in-memory остаётся правильным.
# Если caller вне event-loop'а (тесты, импорт-сайд-эффекты) — тихий
# no-op без ошибок.


# One worker owns all writes for a (wizard kind, operator) until drained.
# Keep it in the shared background registry so shutdown waits for the whole
# queue, including deletes enqueued while an older save is committing.
_PersistFactory = Callable[[], Coroutine[Any, Any, None]]


@dataclass(frozen=True, slots=True)
class _PersistOperation:
    factory: _PersistFactory
    is_delete: bool


_persist_queues: dict[
    tuple[str, int], tuple[deque[_PersistOperation], asyncio.Task[None]]
] = {}


def _spawn_persist(
    key: tuple[str, int], coro_factory: _PersistFactory, *, is_delete: bool = False,
) -> None:
    """Keep ordered, bounded writes; separate wizard keys remain independent.

    Each write is a complete snapshot or a delete, so later writes supersede
    intermediate snapshots. Keep at most three pending writes: the oldest,
    the latest delete barrier, and the newest snapshot. Keeping the delete
    before a recreated draft prevents an old draft returning if that new
    save fails. A stalled DB cannot accumulate every intermediate snapshot.

    Errors are best effort (logged by the write), and the next write still
    runs. Shutdown cancellation closes the active session, drops queued
    writes with a warning, and releases the key; it never launches detached
    successors. Outside a running loop this is a no-op.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    operation = _PersistOperation(coro_factory, is_delete)
    current = _persist_queues.get(key)
    if current is not None and not current[1].done():
        pending = current[0]
        if len(pending) >= 2:
            if is_delete:
                oldest = pending.popleft()
                pending.clear()
                pending.append(oldest)
            elif not pending[-1].is_delete:
                pending.pop()
        pending.append(operation)
        return

    pending = deque([operation])

    async def run() -> None:
        while pending:
            operation = pending.popleft()
            try:
                await operation.factory()
            except Exception:
                log.warning("wizard_registry: queued write for %s failed", key, exc_info=True)

    coro = run()
    try:
        task = spawn_background_task(coro, name="wizard_registry.persist")
    except Exception:
        coro.close()
        log.warning("wizard_registry: persist task spawn failed", exc_info=False)
        return
    _persist_queues[key] = (pending, task)

    def cleanup(done: asyncio.Task[None]) -> None:
        if done.cancelled():
            log.warning(
                "wizard_registry: persistence for %s cancelled; %s queued writes discarded",
                key, len(pending),
            )
        pending.clear()
        if _persist_queues.get(key) == (pending, done):
            _persist_queues.pop(key, None)

    task.add_done_callback(cleanup)


async def _persist_save_op(operator_id: int, state: dict[str, Any]) -> None:
    try:
        async with session_scope() as session:
            await wizard_persist.save_op_wizard(session, operator_id, state)
    except Exception:
        log.warning(
            "wizard_registry: persist op-wizard for %s failed", operator_id,
            exc_info=False,
        )


async def _persist_delete_op(operator_id: int) -> None:
    try:
        async with session_scope() as session:
            await wizard_persist.delete_op_wizard(session, operator_id)
    except Exception:
        log.warning(
            "wizard_registry: delete op-wizard for %s failed", operator_id,
            exc_info=False,
        )


async def _persist_save_broadcast(
    operator_id: int, state: dict[str, Any]
) -> None:
    try:
        async with session_scope() as session:
            await wizard_persist.save_broadcast_wizard(
                session, operator_id, state
            )
    except Exception:
        log.warning(
            "wizard_registry: persist broadcast-wizard for %s failed",
            operator_id, exc_info=False,
        )


async def _persist_delete_broadcast(operator_id: int) -> None:
    try:
        async with session_scope() as session:
            await wizard_persist.delete_broadcast_wizard(
                session, operator_id
            )
    except Exception:
        log.warning(
            "wizard_registry: delete broadcast-wizard for %s failed",
            operator_id, exc_info=False,
        )


class _RegistryDefault(Enum):
    CURRENT = "current"


def schedule_persist_op(
    operator_id: int,
    state: dict[str, Any] | None | _RegistryDefault = _RegistryDefault.CURRENT,
) -> None:
    """После set/update/clear op-wizard — синхронизировать с БД в фоне.

    Явный `state=None` всегда удаляет состояние, включая зеркало после
    гидратации. Без аргумента берём состояние из registry. Переданный
    dict — снимок handler'а, который хранит собственное состояние.
    """
    if state is _RegistryDefault.CURRENT:
        state = _op_wizards.get(operator_id)
    if state is None:
        clear_op_wizard(operator_id)
        _spawn_persist(
            ("op", operator_id), lambda: _persist_delete_op(operator_id), is_delete=True,
        )
    else:
        # Копия dict, чтобы фоновая запись видела immutable snapshot.
        snapshot = deepcopy(state)
        _spawn_persist(("op", operator_id), lambda: _persist_save_op(operator_id, snapshot))


def schedule_persist_broadcast(
    operator_id: int,
    state: dict[str, Any] | None | _RegistryDefault = _RegistryDefault.CURRENT,
) -> None:
    """После set/update/clear broadcast-wizard — синхронизировать с БД.

    `state` см. docstring `schedule_persist_op`.
    """
    if state is _RegistryDefault.CURRENT:
        state = _broadcast_wizards.get(operator_id)
    if state is None:
        clear_broadcast_wizard(operator_id)
        _spawn_persist(
            ("broadcast", operator_id), lambda: _persist_delete_broadcast(operator_id),
            is_delete=True,
        )
    else:
        snapshot = deepcopy(state)
        _spawn_persist(
            ("broadcast", operator_id), lambda: _persist_save_broadcast(operator_id, snapshot)
        )
