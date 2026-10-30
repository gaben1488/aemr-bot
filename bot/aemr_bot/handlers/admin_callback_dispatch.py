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
from aemr_bot.db.models import OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.handlers import admin_commands, admin_panel, callback_router
from aemr_bot.handlers import broadcast as broadcast_handler
from aemr_bot.handlers import broadcast_templates as broadcast_templates_handler
from aemr_bot.handlers._auth import ensure_operator, get_operator
from aemr_bot.services import admin_relay, appeal_history, card_format, consent_history
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


async def _op_open_page(event, page: int) -> None:
    await ack_callback(event)
    operator = await get_operator(event)
    if operator is None:
        return
    await admin_commands._do_open_tickets(event, page=page)


async def _op_open_card(event, appeal_id: int) -> None:
    """Открыть редактируемый экран просмотра без новой записи события."""

    operator = await get_operator(event)
    if operator is None:
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
    if appeal.user is None:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id, text="Данные обращения недоступны.",
            attachments=[kbds.op_back_to_menu_keyboard()],
        )
        return
    # This is a mutable shared navigation screen, not a reply target. Strip
    # every marker, including user-supplied ones, so a swipe on an edited
    # screen cannot route a reply to another resident.
    text = card_format.admin_card(appeal, appeal.user).replace("🆔 №", "🆔 \u200b№")
    text = text.replace(
        "✉️ Ответ — кнопкой ниже либо потяните карточку влево.",
        "✉️ Ответ — кнопкой ниже.",
    )
    periods = getattr(appeal, "sla_history", None) or []
    assessed = [period for period in periods if isinstance(period, dict)
                and period.get("assessment") == "at_reopen"
                and period.get("first_response") and isinstance(period.get("overdue"), bool)]
    breaches = sum(1 for period in assessed if period["overdue"])
    if periods:
        text += (f"\n\n⏱ Прошлых периодов: {len(periods)}, с просрочкой: {breaches}; "
                 f"без достоверной оценки: {len(periods) - len(assessed)}.")
    text += "\n\nДля ответа используйте кнопку под карточкой."
    keyboard = kbds.appeal_admin_actions(
        appeal.id, appeal.status, is_it=operator.role == OperatorRole.IT.value,
        user_blocked=bool(appeal.user.is_blocked),
        closed_due_to_revoke=bool(getattr(appeal, "closed_due_to_revoke", False)),
        attachment_count=len(admin_relay.collect_appeal_attachments(appeal)),
    )
    await send_or_edit_screen(event, chat_id=cfg.admin_group_id, text=text, attachments=[keyboard])


async def _op_history(event, payload: str) -> None:
    await ack_callback(event)
    if not await ensure_operator(event):
        return
    try:
        appeal_id, page = map(int, payload.removeprefix("op:history:").split(":"))
        if appeal_id <= 0 or page < 0:
            return
    except ValueError:
        return
    async with session_scope() as session:
        appeal = await appeals_service.get_by_id_with_messages(session, appeal_id)
    if not appeal:
        await send_or_edit_screen(event, chat_id=cfg.admin_group_id, text="Обращение не найдено.",
                                  attachments=[kbds.op_back_to_menu_keyboard()])
        return
    pages = appeal_history.pages_for(appeal, operator=True)
    page = min(page, len(pages) - 1)
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=f"{pages[page].render(appeal_id)}\n\nСтраница {page + 1} из {len(pages)}",
        attachments=[kbds.appeal_history_keyboard(
            appeal_id, page, len(pages), operator=True,
            attachments=bool(admin_relay.collect_appeal_attachments(appeal)),
            message_id=pages[page].message_id if pages[page].attachments else None,
        )],
    )


async def _op_reply_attempt(event, payload: str) -> None:
    """Display the saved response without sending or changing its state."""
    from maxapi.types import CallbackButton
    from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

    from aemr_bot.db.models import OperatorReplyAttempt
    from aemr_bot.services import privacy_epoch
    from aemr_bot.utils.text_pages import split_text_units
    from aemr_bot.utils.url_defang import defang_for_admin

    await ack_callback(event)
    if not await ensure_operator(event):
        return
    try:
        attempt_id, page = map(int, payload.removeprefix("op:reply_attempt:").split(":"))
        if attempt_id <= 0 or page < 0:
            return
    except ValueError:
        return
    async with session_scope() as barrier:
        await privacy_epoch.hold_delivery(barrier)
        async with session_scope() as session:
            attempt = await session.get(OperatorReplyAttempt, attempt_id)
        if not attempt or attempt.state not in {"sending", "unknown", "confirmed", "not_delivered"}:
            await send_or_edit_screen(event, chat_id=cfg.admin_group_id,
                                     text="Ответ уже обработан или недоступен.",
                                     attachments=[kbds.op_back_to_menu_keyboard()])
            return
        safe_text = (defang_for_admin(attempt.text or "") or "Без текста").replace("🆔 №", "🆔 \u200b№")
        pages = split_text_units(safe_text, 2600)
        page = min(page, len(pages) - 1)
        status = ("MAX подтвердил отправку; запись истории восстанавливается."
                  if attempt.state == "confirmed" else
                  "Сотрудник отметил, что ответ не доставлен. Можно подготовить новый ответ."
                  if attempt.state == "not_delivered" else
                  "Результат отправки неизвестен. Проверьте переписку перед повтором.")
        transport = list(attempt.delivery_parts or [])
        confirmed_parts = sum(1 for part in transport if part.get("state") == "confirmed")
        status += f"\nТранспортных частей подтверждено: {confirmed_parts}; начато: {len(transport)}."
        if transport and transport[-1].get("state") != "confirmed":
            status += f" Последняя часть № {len(transport)} требует сверки."
        keyboard = InlineKeyboardBuilder()
        nav = []
        if page:
            nav.append(CallbackButton(text="◀️ Назад", payload=f"op:reply_attempt:{attempt_id}:{page - 1}"))
        if page + 1 < len(pages):
            nav.append(CallbackButton(text="Далее ▶️", payload=f"op:reply_attempt:{attempt_id}:{page + 1}"))
        if nav:
            keyboard.row(*nav)
        if attempt.attachments:
            keyboard.row(CallbackButton(text="📎 Вложения", payload=f"op:reply_attempt_atts:{attempt_id}"))
        if attempt.state in {"sending", "unknown"}:
            keyboard.row(CallbackButton(text="✅ Доставлен", payload=f"op:reply_resolve:{attempt_id}:delivered"),
                         CallbackButton(text="❌ Не доставлен", payload=f"op:reply_resolve:{attempt_id}:not_delivered"))
        if attempt.state == "not_delivered":
            keyboard.row(CallbackButton(text="🗑 Удалить черновик", payload=f"op:reply_resolve:{attempt_id}:discard"))
        keyboard.row(CallbackButton(text="📂 Карточка", payload=f"op:open_card:{attempt.appeal_id}"))
        keyboard.row(CallbackButton(text="↩️ К списку", payload="op:reply_issues:0"))
        await send_or_edit_screen(event, chat_id=cfg.admin_group_id,
            text=(f"✉️ Ответ по обращению № {attempt.appeal_id} · часть {page + 1}/{len(pages)}\n"
                  f"{status}\n📎 Вложений: {len(attempt.attachments or [])}\n\n{pages[page]}"),
            attachments=[keyboard.as_markup()])


async def _op_reply_attempt_atts(event, attempt_id: int) -> None:
    """Explicitly show the attachment references saved with an attempt."""
    from aemr_bot.db.models import OperatorReplyAttempt
    from aemr_bot.services import privacy_epoch

    await ack_callback(event)
    if not await ensure_operator(event):
        return
    async with session_scope() as barrier:
        await privacy_epoch.hold_delivery(barrier)
        async with session_scope() as session:
            attempt = await session.get(OperatorReplyAttempt, attempt_id)
            appeal = (await appeals_service.get_by_id_with_messages(session, attempt.appeal_id)
                      if attempt else None)
        if not attempt or not appeal or attempt.state not in {"sending", "unknown", "confirmed", "not_delivered"}:
            await send_or_edit_screen(event, chat_id=cfg.admin_group_id, text="Вложения недоступны.",
                                      attachments=[kbds.op_back_to_menu_keyboard()])
            return
        await admin_relay.render_appeal_attachments(
            event.bot, chat_id=cfg.admin_group_id, user_id=None, appeal=appeal,
            header_template="Вложения ответа по обращению #{appeal_id}",
            attachments_override=attempt.attachments,
        )


async def _op_reply_resolve(event, payload: str) -> None:
    """Confirm a human's explicit reconciliation, never send automatically."""
    from maxapi.types import CallbackButton
    from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

    from aemr_bot.db.models import OperatorReplyAttempt
    from aemr_bot.services import appeal_locks, operator_reply_attempts, privacy_epoch
    from aemr_bot.utils.event import get_user_id

    await ack_callback(event)
    if not await ensure_operator(event):
        return
    bits = payload.split(":")
    if len(bits) != 4 or not bits[2].isdigit() or bits[3] not in {"delivered", "not_delivered", "discard"}:
        return
    attempt_id, choice = int(bits[2]), bits[3]
    if bits[1] == "reply_resolve":
        keyboard = InlineKeyboardBuilder()
        keyboard.row(CallbackButton(text="✅ Подтвердить", payload=f"op:reply_confirm:{attempt_id}:{choice}"))
        keyboard.row(CallbackButton(text="↩️ Назад", payload=f"op:reply_attempt:{attempt_id}:0"))
        conclusion = ("ответ действительно доставлен жителю" if choice == "delivered"
                      else "ответ жителю не доставлен")
        prompt = ("Удалить сохранённый черновик недоставленного ответа?"
                  if choice == "discard" else
                  f"Вы проверили переписку и установили, что {conclusion}?\n\n"
                  "Подтверждайте только после проверки. Бот не отправит ответ повторно.")
        await send_or_edit_screen(event, chat_id=cfg.admin_group_id,
            text=prompt,
            attachments=[keyboard.as_markup()])
        return
    actor = get_user_id(event)
    if actor is None:
        return
    async with session_scope() as barrier:
        await privacy_epoch.hold_delivery(barrier)
        async with session_scope() as session:
            attempt = await session.get(OperatorReplyAttempt, attempt_id)
            appeal_id = attempt.appeal_id if attempt else None
        if appeal_id is None:
            changed = False
        else:
            await appeal_locks.hold(barrier, appeal_id)
            async with session_scope() as session:
                if choice == "discard":
                    changed = await operator_reply_attempts.discard_not_delivered(
                        session, attempt_id, actor_max_id=actor,
                    )
                else:
                    changed = await operator_reply_attempts.resolve_unknown(
                        session, attempt_id, delivered=choice == "delivered", actor_max_id=actor,
                    )
        text = ("✅ Отметка сохранена. Повторной отправки не было." if changed else
                "Состояние уже изменилось или ответ недоступен. Обновите список.")
        await send_or_edit_screen(event, chat_id=cfg.admin_group_id, text=text,
                                  attachments=[kbds.op_back_to_menu_keyboard()])


async def _op_consent(event, payload: str) -> None:
    """Show only captured evidence; older consent text may be unavailable."""
    from maxapi.types import CallbackButton
    from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

    await ack_callback(event)
    if not await ensure_operator(event):
        return
    try:
        appeal_id, page = map(int, payload.removeprefix("op:consent:").split(":"))
        if appeal_id <= 0 or page < 0:
            return
    except ValueError:
        return
    async with session_scope() as session:
        appeal = await appeals_service.get_by_id_with_messages(session, appeal_id)
    if not appeal or not appeal.user:
        await send_or_edit_screen(event, chat_id=cfg.admin_group_id,
                                  text="Данные жителя недоступны.",
                                  attachments=[kbds.op_back_to_menu_keyboard()])
        return
    pages = consent_history.pages_for(getattr(appeal.user, "consent_records", None) or [])
    keyboard = InlineKeyboardBuilder()
    if not pages:
        text = ("🔐 Согласие жителя\n\nТекст ранее данного согласия не сохранился. "
                "Восстановить его по текущим настройкам нельзя.")
    else:
        page = min(page, len(pages) - 1)
        item = pages[page]
        text = (f"🔐 Согласие жителя · редакция {item.edition} из {item.editions}\n"
                f"Подтверждено: {item.date}\nЧасть {item.part} из {item.parts}\n\n"
                f"{item.text.replace('🆔 №', '🆔 \u200b№')}")
        nav = []
        if page:
            nav.append(CallbackButton(text="◀️ Назад", payload=f"op:consent:{appeal_id}:{page - 1}"))
        if page + 1 < len(pages):
            nav.append(CallbackButton(text="Далее ▶️", payload=f"op:consent:{appeal_id}:{page + 1}"))
        if nav:
            keyboard.row(*nav)
    keyboard.row(CallbackButton(text="↩️ К обращению", payload=f"op:open_card:{appeal_id}"))
    await send_or_edit_screen(event, chat_id=cfg.admin_group_id, text=text,
                              attachments=[keyboard.as_markup()])


async def _op_word(event, appeal_id: int) -> None:
    """Export one appeal only after checking the actor's operator role."""
    import asyncio

    from aemr_bot.services import admin_bus, appeal_word, uploads
    from aemr_bot.utils.resident_lifecycle import guard

    await ack_callback(event, "Готовлю документ…")
    if not await ensure_operator(event):
        return
    async with session_scope() as session:
        appeal = await appeals_service.get_by_id_with_messages(session, appeal_id)
    if not appeal:
        await send_or_edit_screen(event, chat_id=cfg.admin_group_id,
                                  text="Обращение не найдено.",
                                  attachments=[kbds.op_back_to_menu_keyboard()])
        return
    resident_id = getattr(appeal.user, "max_user_id", None)
    async with guard(resident_id):
        # Erasure shares this guard. Re-read after acquiring it, and do not
        # send a document built from a snapshot taken before deletion.
        async with session_scope() as session:
            current = await appeals_service.get_by_id_with_messages(session, appeal_id)
        if not current or getattr(current.user, "max_user_id", None) != resident_id:
            await send_or_edit_screen(event, chat_id=cfg.admin_group_id,
                                      text="Данные обращения изменились. Откройте карточку заново.",
                                      attachments=[kbds.op_back_to_menu_keyboard()])
            return
        document = await asyncio.to_thread(appeal_word.build_appeal_docx, current)
        token = await uploads.upload_bytes(event.bot, document, suffix=".docx")
        if not token:
            await send_or_edit_screen(event, chat_id=cfg.admin_group_id,
                                      text="Не удалось загрузить документ. Попробуйте позже.",
                                      attachments=[kbds.op_back_to_menu_keyboard()])
            return
        sent = await admin_bus.send(event.bot, text=f"📄 История обращения № {appeal_id} · Word",
                                    attachments=[uploads.file_attachment(token)], critical=True)
        if not sent:
            await send_or_edit_screen(
                event, chat_id=cfg.admin_group_id,
                text="Не удалось подтвердить отправку документа. Проверьте чат перед повтором.",
                attachments=[kbds.op_back_to_menu_keyboard()],
            )


async def _op_record_attachments(event, payload: str) -> None:
    await ack_callback(event)
    if not await ensure_operator(event):
        return
    try:
        appeal_id, message_id = map(int, payload.removeprefix("op:record_atts:").split(":"))
        if appeal_id <= 0 or message_id < 0:
            return
    except ValueError:
        return
    async with session_scope() as session:
        appeal = await appeals_service.get_by_id_with_messages(session, appeal_id)
    attachments = appeal_history.attachments_for(appeal, message_id, operator=True) if appeal else None
    if not attachments:
        await send_or_edit_screen(event, chat_id=cfg.admin_group_id,
                                  text="Вложения этой записи недоступны.",
                                  attachments=[kbds.op_back_to_menu_keyboard()])
        return
    await admin_relay.render_appeal_attachments(
        event.bot, chat_id=cfg.admin_group_id, user_id=None, appeal=appeal,
        header_template="📎 Вложения записи обращения #{appeal_id}",
        attachments_override=attachments,
    )


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
    ("op:delivery_issues:", lambda e, page: admin_panel.run_delivery_issues(e, page)),
    ("op:reply_issues:", lambda e, page: admin_panel.run_reply_issues(e, page)),
    ("op:reply_attempt_atts:", _op_reply_attempt_atts),
    ("op:open_page:", _op_open_page),
    ("op:open_card:", _op_open_card),
    ("op:word:", _op_word),
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
async def _op_reply_draft(event, payload: str):
    from aemr_bot.handlers import operator_draft
    await operator_draft.callback(event, payload)


_PREFIX_RAW: tuple[tuple[str, Callable[[object, str], Awaitable[object]]], ...] = (
    ("op:replydraft:", _op_reply_draft),
    ("op:reply_cancel:", lambda e, p: admin_commands.run_reply_cancel(e, p.removeprefix("op:reply_cancel:"))),
    ("op:reply_resolve:", _op_reply_resolve),
    ("op:reply_confirm:", _op_reply_resolve),
    ("op:reply_attempt:", _op_reply_attempt),
    ("op:consent:", _op_consent),
    ("op:history:", _op_history),
    ("op:record_atts:", _op_record_attachments),
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
