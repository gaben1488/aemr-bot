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

from aemr_bot import keyboards as kbds
from aemr_bot import texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.handlers import admin_commands
from aemr_bot.handlers import broadcast as broadcast_handler
from aemr_bot.handlers import broadcast_templates as broadcast_templates_handler
from aemr_bot.handlers import callback_router
from aemr_bot.handlers._auth import ensure_operator
from aemr_bot.services import admin_card as admin_card_service
from aemr_bot.services import appeals as appeals_service
from aemr_bot.utils.event import ack_callback, send_or_edit_screen

# Сигнатуры handler'ов:
#   exact-handler:  async def (event) -> None
#   prefix-handler: async def (event, tail_id: int) -> None
#     (целочисленный хвост уже распарсен; None-хвост обрабатывается
#      диспетчером — ack + стоп, без вызова handler'а)
ExactHandler = Callable[[object], Awaitable[None]]
PrefixHandler = Callable[[object, int], Awaitable[None]]


# ---- op:* exact -------------------------------------------------------------
#
# Большинство op-кнопок: ack_callback + один вызов admin_commands.run_*.
# Однотипные сворачиваем фабриками, простой проброс живёт lambda'ой
# прямо в таблице маршрутов; отдельными функциями остаются только те,
# кто реально что-то делает сверх вызова.


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


async def _op_reply_intermediate(event, appeal_id: int) -> None:
    """Промежуточный ответ — не закрывает обращение."""
    await admin_commands.run_reply_intent(event, appeal_id, is_final=False)


async def _op_open_card(event, appeal_id: int) -> None:
    """Открыть полную карточку обращения с timeline (история переписки).

    Шаг 2-в от 2026-05-26: listing «Открытые обращения» компактный
    (одна кнопка на обращение), история раскрывается явным кликом
    через эту команду. Используем `admin_card.render(force_new=True)`
    — карточка появится новой записью внизу чата, не редактирует
    listing-сообщение (сохраняет sacred-инвариант).
    """

    if not await ensure_operator(event):
        return
    async with session_scope() as session:
        appeal = await appeals_service.get_by_id_with_messages(
            session, appeal_id
        )
    if appeal is None:
        await ack_callback(event, "Не найдено")
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=f"⚠️ Обращение #{appeal_id} не найдено (возможно, удалено).",
            attachments=[kbds.op_back_to_menu_keyboard()],
        )
        return
    await ack_callback(event, "Открываю…")
    await admin_card_service.render(event.bot, appeal, force_new=True)


# ---- Таблицы маршрутов ------------------------------------------------------
#
# _EXACT — точное совпадение payload. _PREFIX_ID — `op:<verb>:<id>`,
# хвост-int парсится диспетчером. _PREFIX_RAW — `op:aud:` / `op:opadd:`
# / `op:setkey:`: handler сам разбирает payload и сам делает ack
# (ack делегирован внутрь run_*-функций).

# Все вызовы в таблицах — lambda, резолвящие цель в рантайме (см.
# docstring _ack_then). Прямые ссылки `admin_commands.run_X` тут
# класть нельзя.
_EXACT: dict[str, ExactHandler] = {
    # broadcast wizard (ack делегирован внутрь broadcast._handle_*)
    "broadcast:confirm": lambda e: broadcast_handler._handle_confirm(e),
    "broadcast:abort": lambda e: broadcast_handler._handle_abort(e),
    "broadcast:edit": lambda e: broadcast_handler._handle_edit(e),
    # Меню оператора / действия
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
    "op:help_full": _ack_then(lambda e: _show_op_help_full(e)),
    "op:help_security": _ack_then(lambda e: _show_op_help_security(e)),
    # ack делегирован внутрь run_reply_cancel — здесь не акаем.
    "op:reply_cancel": lambda e: admin_commands.run_reply_cancel(e),
}


async def _show_op_help_full(event) -> None:
    """Подменю «📋 Памятка оператора» (главный экран).

    Команды, рассылки, ответы по обращениям. Кнопка → переход на
    второй экран `🛡️ Безопасность и антифишинг`.

    Раньше один экран OP_HELP_FULL_LEGACY содержал всё — ~8230 char,
    превышал MAX-API limit 4000, при тапе кнопки падал с
    `ValueError: text должен быть меньше 4000 символов`. Разбит на 2.
    """

    if not await ensure_operator(event):
        return
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=texts.OP_HELP_MAIN.format(answer_limit=cfg.answer_max_chars),
        attachments=[kbds.op_help_main_keyboard()],
    )


async def _show_op_help_security(event) -> None:
    """Подменю «🛡️ Безопасность и антифишинг» — второй экран памятки.

    Антифишинг, реакция на скам, компрометация аккаунта, ключевые
    документы. Кнопка → возврат к главному экрану памятки.
    """

    if not await ensure_operator(event):
        return
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=texts.OP_HELP_SECURITY,
        attachments=[kbds.op_help_security_keyboard()],
    )

# prefix → (handler, нужен ли payload в handler).
# _PREFIX_ID: `op:<verb>:<int>` — диспетчер парсит int-хвост.
_PREFIX_ID: tuple[tuple[str, PrefixHandler], ...] = (
    ("broadcast:stop:", lambda e, bid: broadcast_handler._handle_stop(e, bid)),
    # C2: оператор отменил рассылку, пока она ждёт в cooldown'е.
    (
        "broadcast:cancel-cooldown:",
        lambda e, bid: broadcast_handler._handle_cancel_cooldown(e, bid),
    ),
    ("op:reply:", lambda e, aid: admin_commands.run_reply_intent(e, aid)),
    ("op:replyint:", _op_reply_intermediate),
    ("op:reopen:", lambda e, aid: admin_commands.run_reopen(e, aid)),
    ("op:close:", lambda e, aid: admin_commands.run_close(e, aid)),
    ("op:erase:", lambda e, aid: admin_commands.run_erase_for_appeal(e, aid)),
    (
        "op:block:",
        lambda e, aid: admin_commands.run_block_for_appeal(e, aid, blocked=True),
    ),
    (
        "op:unblock:",
        lambda e, aid: admin_commands.run_block_for_appeal(e, aid, blocked=False),
    ),
    ("op:atts:", lambda e, aid: admin_commands.run_show_attachments(e, aid)),
    ("op:open_card:", _op_open_card),
    # PR G: история рассылок — карточка / клон / failed-deliveries.
    ("op:bc:open:", lambda e, bid: broadcast_handler._open_broadcast(e, bid)),
    ("op:bc:clone:", lambda e, bid: broadcast_handler._clone_broadcast(e, bid)),
    (
        "op:bc:failed:",
        lambda e, bid: broadcast_handler._list_failed_deliveries(e, bid),
    ),
)

# _PREFIX_RAW: handler получает весь payload и сам делает ack.
# Вызовы — lambda (не прямые ссылки), чтобы admin_commands.X
# резолвился в рантайме, см. docstring _ack_then.
#
# Все callback'и иерархического меню «Настройки» и карточки оператора
# идут через те же два диспетчера (run_settings_action / run_operators_action),
# которые внутри сами разбирают конкретные суффиксы. Это минимизирует
# изменения в admin_callback_dispatch и сохраняет один-в-один маппинг
# «префикс → run-функция».
#
# `_op_operators_action` остаётся именованной функцией: на неё смотрят
# сразу семь префиксов, семь одинаковых lambda были бы хуже.
async def _op_operators_action(event, payload: str) -> None:
    await admin_commands.run_operators_action(event, payload)


# Возврат — `Awaitable[object]`, а не `Awaitable[None]`: диспетчер
# результат не читает, а часть целевых функций его всё же отдаёт
# (`broadcast_templates.handle_callback` → bool). Узкий `None` заставлял
# бы оборачивать такие цели пустой обёрткой ради типа — ровно те
# обёртки, которые здесь и убирали.
_PREFIX_RAW: tuple[tuple[str, Callable[[object, str], Awaitable[object]]], ...] = (
    ("op:aud:", lambda e, p: admin_commands.run_audience_action(e, p)),
    # Операторы — единое семейство:
    ("op:opadd:", _op_operators_action),
    ("op:opcard:", _op_operators_action),
    ("op:oprole:", _op_operators_action),
    ("op:opchrole:", _op_operators_action),
    ("op:opdeact:", _op_operators_action),
    ("op:opdeact_ok:", _op_operators_action),
    ("op:opreact:", _op_operators_action),
    # Настройки — старый экспертный и новый иерархический:
    ("op:setkey:", lambda e, p: admin_commands.run_settings_action(e, p)),
    ("op:set:", lambda e, p: admin_commands.run_settings_action(e, p)),
    # Шаблоны рассылок (PR H) — list/new/open:<id>/apply:<id>/rename:<id>/
    # edit:<id>/delete:<id>/delete_ok:<id>/cancel. Внутри handle_callback
    # сам делает ack.
    ("op:tmpl:", lambda e, p: broadcast_templates_handler.handle_callback(e, p)),
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
            # Часть целей отвечает bool: False = «payload мой по префиксу,
            # но хвост я не узнал» (`broadcast_templates.handle_callback`).
            # Такой случай нельзя выдавать за обработанный: оператор
            # тапнул устаревшую кнопку и не получил ни ack (спиннер
            # крутится), ни текста, ни падения в menu.handle_callback.
            # Симметрично ветке `_PREFIX_ID` выше: ack и стоп.
            handled = await raw_handler(event, payload)
            if handled is False:
                await ack_callback(event)
            return True

    # Не admin-callback (или `op:`/`broadcast:` обёртка с неизвестным
    # хвостом). Раньше управление проваливалось из if-обёртки в
    # menu.handle_callback — сохраняем: возвращаем False, caller решает.
    return False
