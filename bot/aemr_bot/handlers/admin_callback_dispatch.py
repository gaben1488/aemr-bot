"""Dispatch admin/operator callback-payload'ов (`broadcast:*` / `op:*`).

Вынесено из `handlers/appeal.py:on_callback` (батч 1 polish). Там это
был ~155-строчный if-elif внутри 403-строчной функции. Здесь —
декларативные таблицы `_EXACT` / `_PREFIX` плюс тонкие handler'ы;
`on_callback` теперь делегирует одной строкой через
`dispatch_admin_callback`.

Контракт `dispatch_admin_callback(event, payload) -> bool`:
- вернул `True`  — payload распознан и обработан, дальше не идём;
- вернул `False` — payload не admin-callback (или admin-обёртка
  `op:`/`broadcast:` с неизвестным хвостом) → caller продолжает
  fallthrough в `menu.handle_callback`. Этот инвариант критичен:
  раньше unknown `op:weird` проваливался из if-обёртки в menu —
  поведение сохранено.

Импорты `admin_commands` / `broadcast` — top-level: цикла нет
(`admin_commands` тянет только `services.appeals` лениво,
`broadcast` не импортирует `appeal` вовсе). Это заодно убирает
lazy-import внутри `on_callback`.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from aemr_bot.handlers import admin_commands
from aemr_bot.handlers import broadcast as broadcast_handler
from aemr_bot.handlers import callback_router
from aemr_bot.utils.event import ack_callback

# Сигнатуры handler'ов:
#   exact-handler:  async def (event) -> None
#   prefix-handler: async def (event, tail_id: int) -> None
#     (целочисленный хвост уже распарсен; None-хвост обрабатывается
#      диспетчером — ack + стоп, без вызова handler'а)
ExactHandler = Callable[[object], Awaitable[None]]
PrefixHandler = Callable[[object, int], Awaitable[None]]


# ---- broadcast:* exact ------------------------------------------------------


async def _broadcast_confirm(event) -> None:
    await broadcast_handler._handle_confirm(event)


async def _broadcast_abort(event) -> None:
    await broadcast_handler._handle_abort(event)


async def _broadcast_edit(event) -> None:
    await broadcast_handler._handle_edit(event)


async def _broadcast_stop(event, broadcast_id: int) -> None:
    await broadcast_handler._handle_stop(event, broadcast_id)


# ---- op:* exact -------------------------------------------------------------
#
# Большинство op-кнопок: ack_callback + один вызов admin_commands.run_*.
# Однотипные сворачиваем фабриками, уникальные — явными функциями.


def _ack_then(coro_factory: Callable[[object], Awaitable[None]]) -> ExactHandler:
    """Handler-обёртка: ack callback, затем выполнить coro_factory(event).

    `coro_factory` ОБЯЗАН быть lambda/функцией, резолвящей целевой
    вызов в рантайме (`lambda e: admin_commands.run_X(e)`), а не
    прямой ссылкой `admin_commands.run_X`. Иначе таблица заморозит
    ссылку на момент импорта: тесты не смогут её замокать, а любой
    hot-reload получит устаревший вызов.
    """
    async def _handler(event) -> None:
        await ack_callback(event)
        await coro_factory(event)
    return _handler


def _stats_handler(period: str) -> ExactHandler:
    """op:stats_<period> → ack + run_stats(event, period)."""
    async def _handler(event) -> None:
        await ack_callback(event)
        await admin_commands.run_stats(event, period)
    return _handler


async def _op_stats_today(event) -> None:
    # Особый: после выгрузки за сегодня возвращаем операторское меню,
    # но только если выгрузка реально ушла (run_stats_today → bool).
    await ack_callback(event)
    if await admin_commands.run_stats_today(event):
        await admin_commands.show_op_menu(event, pin=False)


# ---- op:<verb>:<id> prefix-handler'ы ---------------------------------------
# Целочисленный хвост парсит dispatch_admin_callback и передаёт сюда.


async def _op_reply(event, appeal_id: int) -> None:
    await admin_commands.run_reply_intent(event, appeal_id)


async def _op_reopen(event, appeal_id: int) -> None:
    await admin_commands.run_reopen(event, appeal_id)


async def _op_close(event, appeal_id: int) -> None:
    await admin_commands.run_close(event, appeal_id)


async def _op_erase(event, appeal_id: int) -> None:
    await admin_commands.run_erase_for_appeal(event, appeal_id)


async def _op_block(event, appeal_id: int) -> None:
    await admin_commands.run_block_for_appeal(event, appeal_id, blocked=True)


async def _op_unblock(event, appeal_id: int) -> None:
    await admin_commands.run_block_for_appeal(event, appeal_id, blocked=False)


# ---- Таблицы маршрутов ------------------------------------------------------
#
# _EXACT — точное совпадение payload. _PREFIX_ID — `op:<verb>:<id>`,
# хвост-int парсится диспетчером. _PREFIX_RAW — `op:aud:` / `op:opadd:`
# / `op:setkey:`: handler сам разбирает payload и сам делает ack
# (ack делегирован внутрь run_*-функций).

async def _op_reply_cancel(event) -> None:
    # ack делегирован внутрь run_reply_cancel — здесь не акаем.
    await admin_commands.run_reply_cancel(event)


# Все coro_factory — lambda, резолвящие вызов в рантайме (см.
# docstring _ack_then). Прямые ссылки `admin_commands.run_X` тут
# класть нельзя.
_EXACT: dict[str, ExactHandler] = {
    # broadcast wizard (ack делегирован внутрь broadcast._handle_*)
    "broadcast:confirm": _broadcast_confirm,
    "broadcast:abort": _broadcast_abort,
    "broadcast:edit": _broadcast_edit,
    # operator menu / actions
    "op:menu": _ack_then(lambda e: admin_commands.show_op_menu(e, pin=False)),
    "op:stats_menu": _ack_then(lambda e: admin_commands.run_stats_menu(e)),
    "op:stats_today": _op_stats_today,
    "op:stats_week": _stats_handler("week"),
    "op:stats_month": _stats_handler("month"),
    "op:stats_quarter": _stats_handler("quarter"),
    "op:stats_half_year": _stats_handler("half_year"),
    "op:stats_year": _stats_handler("year"),
    "op:stats_all": _stats_handler("all"),
    "op:open_tickets": _ack_then(lambda e: admin_commands.run_open_tickets(e)),
    "op:diag": _ack_then(lambda e: admin_commands.run_diag(e)),
    "op:backup": _ack_then(lambda e: admin_commands.run_backup(e)),
    "op:broadcast": _ack_then(lambda e: broadcast_handler._start_wizard(e)),
    "op:broadcast_list": _ack_then(
        lambda e: broadcast_handler._list_broadcasts(e)
    ),
    "op:operators": _ack_then(lambda e: admin_commands.run_operators_menu(e)),
    "op:settings": _ack_then(lambda e: admin_commands.run_settings_menu(e)),
    "op:audience": _ack_then(lambda e: admin_commands.run_audience_menu(e)),
    "op:reply_cancel": _op_reply_cancel,
}

# prefix → (handler, нужен ли payload в handler).
# _PREFIX_ID: `op:<verb>:<int>` — диспетчер парсит int-хвост.
_PREFIX_ID: tuple[tuple[str, PrefixHandler], ...] = (
    ("broadcast:stop:", _broadcast_stop),
    ("op:reply:", _op_reply),
    ("op:reopen:", _op_reopen),
    ("op:close:", _op_close),
    ("op:erase:", _op_erase),
    ("op:block:", _op_block),
    ("op:unblock:", _op_unblock),
)

# _PREFIX_RAW: handler получает весь payload и сам делает ack.
# Обёртки (не прямые ссылки) — чтобы admin_commands.X резолвился в
# рантайме, см. docstring _ack_then.
async def _op_aud(event, payload: str) -> None:
    await admin_commands.run_audience_action(event, payload)


async def _op_opadd(event, payload: str) -> None:
    await admin_commands.run_operators_action(event, payload)


async def _op_setkey(event, payload: str) -> None:
    await admin_commands.run_settings_action(event, payload)


_PREFIX_RAW: tuple[tuple[str, Callable[[object, str], Awaitable[None]]], ...] = (
    ("op:aud:", _op_aud),
    ("op:opadd:", _op_opadd),
    ("op:setkey:", _op_setkey),
)


async def dispatch_admin_callback(event, payload: str) -> bool:
    """Обработать admin/operator callback. См. контракт в docstring модуля.

    Возвращает True если payload распознан и обработан, False если это
    не admin-callback — тогда caller продолжает обычный fallthrough.
    """
    handler = _EXACT.get(payload)
    if handler is not None:
        await handler(event)
        return True

    for prefix, id_handler in _PREFIX_ID:
        if payload.startswith(prefix):
            tail = callback_router.parse_int_tail(payload, prefix)
            if tail is None:
                # Stale/битый хвост — ack и стоп, действие не выполняем.
                await ack_callback(event)
                return True
            await id_handler(event, tail)
            return True

    for prefix, raw_handler in _PREFIX_RAW:
        if payload.startswith(prefix):
            await raw_handler(event, payload)
            return True

    # Не admin-callback (или `op:`/`broadcast:` обёртка с неизвестным
    # хвостом). Раньше управление проваливалось из if-обёртки в
    # menu.handle_callback — сохраняем: возвращаем False, caller решает.
    return False
