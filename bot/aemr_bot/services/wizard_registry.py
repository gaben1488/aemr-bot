"""Хранилище мутабельного in-memory состояния визардов и reply-intent'а
оператора (op_wizard, broadcast_wizard, reply_intent).

Не вносит логику, только хранение — бизнес-логика остаётся в handlers.

Фактический прод-контур (что реально читается вне тестов):
- reply-intent: `set_reply_intent` / `get_reply_intent` / `drop_reply_intent`
  — `handlers/operator_reply` (через wrapper-функции) и
  `handlers/admin_appeal_ops`.
- op/broadcast wizard persistence: `schedule_persist_op` /
  `schedule_persist_broadcast` (вызываются из `admin_operators` при
  set/clear), а `set_op_wizard` / `set_broadcast_wizard` + словари
  `_op_wizards` / `_broadcast_wizards` наполняются на старте бота из БД
  через `services/wizard_persist.hydrate_into_registry`.

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
# Дизайн: handlers продолжают звать sync-функции (set_op_wizard,
# clear_op_wizard и т.д.) — а эти хуки автоматически spawn'ят background
# task, который запишет в БД. На старте бота `wizard_persist.hydrate_*`
# подгружает обратно в in-memory.
#
# Если БД-запись упала — лог warning, in-memory остаётся правильным.
# Если caller вне event-loop'а (тесты, импорт-сайд-эффекты) — тихий
# no-op без ошибок.


def _spawn_persist(coro_factory) -> None:
    """Запустить async-coro в фоне, если есть running loop. Без него —
    no-op (юнит-тесты импортируют модуль вне asyncio context'а).

    Использует `utils.background.spawn_background_task` для защиты от
    GC: голый `loop.create_task` хранится в event loop только через
    weakref, и сборщик мусора может прервать задачу до завершения
    (особенно опасно для коротких persist'ов в БД)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        spawn_background_task(
            coro_factory(), name="wizard_registry.persist"
        )
    except Exception:
        log.warning("wizard_registry: persist task spawn failed", exc_info=False)


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


def schedule_persist_op(
    operator_id: int, state: dict[str, Any] | None = None
) -> None:
    """После set/update/clear op-wizard — синхронизировать с БД в фоне.

    Если `state` передан явно — используем его (handler хранит свой
    собственный dict, не registry's). Если не передан — берём из
    registry. None в обоих случаях = delete.
    """
    if state is None:
        state = _op_wizards.get(operator_id)
    if state is None:
        _spawn_persist(lambda: _persist_delete_op(operator_id))
    else:
        # Копия dict, чтобы фоновая запись видела immutable snapshot.
        snapshot = dict(state)
        _spawn_persist(lambda: _persist_save_op(operator_id, snapshot))


def schedule_persist_broadcast(
    operator_id: int, state: dict[str, Any] | None = None
) -> None:
    """После set/update/clear broadcast-wizard — синхронизировать с БД.

    `state` см. docstring `schedule_persist_op`.
    """
    if state is None:
        state = _broadcast_wizards.get(operator_id)
    if state is None:
        _spawn_persist(lambda: _persist_delete_broadcast(operator_id))
    else:
        snapshot = dict(state)
        _spawn_persist(lambda: _persist_save_broadcast(operator_id, snapshot))
