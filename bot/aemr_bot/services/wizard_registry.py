"""Единое хранилище мутабельного состояния визардов и intent'ов
оператора.

До этого модуля каждый handler хранил собственный module-level dict:
- `handlers/admin_commands.py:_op_wizards` — wizard «Добавить оператора»
- `handlers/broadcast.py:_wizards` — wizard рассылки
- `handlers/operator_reply.py:_reply_intent` — короткоживущий «оператор
  готовится ответить на обращение N»
- `handlers/operator_reply.py:_recent_replies` — дедуп уже отправленных
  ответов (кросс-процессная защита от двойного нажатия)

Минусы старой схемы:
- кросс-handler доступ через приватные имена (ruff SLF001 — 12+ мест:
  `broadcast_handler._wizards.pop(...)` в `appeal.py` etc)
- нет единой точки сброса при `/cancel` оператора — приходилось
  вручную дёргать каждое из 4 хранилищ
- состояние раскидано — при тестах не понятно что нужно мокать

Этот модуль — единая точка с публичным API. Сами мутабельные dict'ы
остаются на module-level (in-memory, single-process), но доступ к
ним идёт через явные функции:

    from aemr_bot.services import wizard_registry as wr

    wr.set_op_wizard(operator_id, {"step": "awaiting_id"})
    state = wr.get_op_wizard(operator_id)
    wr.clear_all_for(operator_id)  # сброс всех визардов оператора

Для тестов есть `wr.reset_all()` — обнуляет все хранилища.

Не вносит логику — только хранение. Бизнес-логика остаётся в handlers.
Это снижает риск регрессии при выделении: данные не меняются, меняется
только способ доступа.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

# ---- Internal storage (private to this module) ----------------------------

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
# Ключ: max_user_id оператора. Значение: (appeal_id, set_at_ts).
_reply_intent: dict[int, tuple[int, float]] = {}

# Дедуп недавно отправленных ответов оператора. Ключ: hash от
# (appeal_id, normalized_text). Значение: timestamp. Защита от
# мгновенного двойного нажатия «Ответить» с тем же текстом.
_recent_replies: dict[str, float] = {}


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


def get_reply_intent(operator_id: int) -> tuple[int, float] | None:
    return _reply_intent.get(operator_id)


def set_reply_intent(operator_id: int, appeal_id: int, ts: float) -> None:
    _reply_intent[operator_id] = (appeal_id, ts)


def drop_reply_intent(operator_id: int) -> None:
    _reply_intent.pop(operator_id, None)


# ---- Public API: recent replies (dedup) ----------------------------------


def is_recent_reply(key: str) -> bool:
    return key in _recent_replies


def remember_recent_reply(key: str, ts: float) -> None:
    _recent_replies[key] = ts


def evict_old_replies(cutoff_ts: float, max_entries: int = 256) -> None:
    """Очистить старые записи. Вызывается лениво из operator_reply
    при каждом новом ответе. cutoff_ts — timestamp граница."""
    # Удалить старые
    for key in list(_recent_replies.keys()):
        if _recent_replies[key] < cutoff_ts:
            del _recent_replies[key]
    # Если всё равно много — обрезать до max_entries (свежие)
    if len(_recent_replies) > max_entries:
        sorted_items = sorted(_recent_replies.items(), key=lambda kv: kv[1], reverse=True)
        _recent_replies.clear()
        _recent_replies.update(dict(sorted_items[:max_entries]))


# ---- Public API: bulk operations -----------------------------------------


def clear_all_for(operator_id: int) -> None:
    """Глобальный сброс всех визардов и intent'ов оператора.

    Вызывается из `/cancel` в админ-чате — оператор «потерялся» в
    каком-то wizard'е и хочет начать с чистого листа. До этой функции
    приходилось дёргать каждый dict отдельно через приватный доступ.
    """
    clear_op_wizard(operator_id)
    clear_broadcast_wizard(operator_id)
    drop_reply_intent(operator_id)


def reset_all() -> None:
    """Обнулить ВСЁ состояние. Только для unit-тестов между case'ами."""
    _op_wizards.clear()
    _broadcast_wizards.clear()
    _reply_intent.clear()
    _recent_replies.clear()


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
    no-op (юнит-тесты импортируют модуль вне asyncio context'а)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        loop.create_task(coro_factory())
    except Exception:
        log.warning("wizard_registry: persist task spawn failed", exc_info=False)


async def _persist_save_op(operator_id: int, state: dict[str, Any]) -> None:
    try:
        from aemr_bot.db.session import session_scope
        from aemr_bot.services import wizard_persist

        async with session_scope() as session:
            await wizard_persist.save_op_wizard(session, operator_id, state)
    except Exception:
        log.warning(
            "wizard_registry: persist op-wizard for %s failed", operator_id,
            exc_info=False,
        )


async def _persist_delete_op(operator_id: int) -> None:
    try:
        from aemr_bot.db.session import session_scope
        from aemr_bot.services import wizard_persist

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
        from aemr_bot.db.session import session_scope
        from aemr_bot.services import wizard_persist

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
        from aemr_bot.db.session import session_scope
        from aemr_bot.services import wizard_persist

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
