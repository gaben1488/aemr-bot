"""Клавиатуры для рассылок (broadcast) и шаблонов.

Включает:
- Кнопку отписки под каждым сообщением рассылки (citizen-facing).
- Wizard оператора: confirm, cancel, stop, cooldown.
- История рассылок и список «не доставлено».
- CRUD шаблонов: list, search, preview, card, delete-confirm, step-2.
"""
from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from aemr_bot.handlers import callback_payloads as cp


def broadcast_unsubscribe_keyboard():
    """Inline-кнопка под каждым сообщением рассылки — отписка в одно нажатие."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="🔕 Отписаться от рассылки",
            payload=cp.BROADCAST_UNSUBSCRIBE,
        )
    )
    kb.row(CallbackButton(text="↩️ В меню", payload=cp.MENU_MAIN))
    return kb.as_markup()


def broadcast_confirm_keyboard():
    """Шаг анкеты: оператор подтверждает, переписывает или отменяет рассылку.

    Кнопка «Изменить текст» возвращает мастер в шаг awaiting_text без
    потери уже введённого. Раньше для исправления опечатки приходилось
    отменять и заново вводить текст с нуля.
    """
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="✅ Разослать", payload=cp.BROADCAST_CONFIRM),
        CallbackButton(text="✏️ Изменить текст", payload=cp.BROADCAST_EDIT),
    )
    kb.row(CallbackButton(text="❌ Отмена", payload=cp.BROADCAST_ABORT))
    return kb.as_markup()


def broadcast_cancel_keyboard():
    """Кнопка отмены под промптом «введите текст рассылки». Чтобы оператор
    мог выйти из мастера в один тап вместо набора /cancel."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="❌ Отменить рассылку", payload=cp.BROADCAST_ABORT))
    return kb.as_markup()


def broadcast_stop_keyboard(broadcast_id: int):
    """Кнопка экстренной остановки, видимая всем операторам, пока идёт рассылка."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="⛔ Экстренно остановить",
            payload=cp.broadcast_stop(broadcast_id),
        )
    )
    kb.row(CallbackButton(text="↩️ В админ-меню", payload=cp.OP_MENU))
    return kb.as_markup()


def broadcast_cooldown_keyboard(broadcast_id: int):
    """Кнопка отмены рассылки во время cooldown'а (SECURITY_REVIEW C2).

    Между confirm и реальной отправкой — окно ~5 минут, во время
    которого оператор может передумать (увидел опечатку, понял что
    текст не тот). Эта кнопка останавливает отложенный таск и
    помечает broadcast как cancelled."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="❌ Отменить отправку",
            payload=cp.broadcast_cancel_cooldown(broadcast_id),
        )
    )
    kb.row(CallbackButton(text="↩️ В админ-меню", payload=cp.OP_MENU))
    return kb.as_markup()


def broadcast_history_list_keyboard(items):
    """Список последних рассылок (PR G) — каждая строка кликабельна.

    Нажатие открывает карточку рассылки (`op:bc:open:<id>`) с
    текстом, картинками и действиями «📝 Создать на основе» /
    «👥 Не доставлено».
    """
    kb = InlineKeyboardBuilder()
    for bc in items:
        # status emoji подсказывает «есть проблемы / завершено».
        status = (bc.status or "").lower()
        if status == "done":
            mark = "✅"
        elif status in {"failed", "cancelled"}:
            mark = "⚠️"
        elif status == "sending":
            mark = "▶️"
        else:
            mark = "•"
        kb.row(
            CallbackButton(
                text=f"{mark} #{bc.id} · {bc.delivered_count}/{bc.subscriber_count_at_start}",
                payload=cp.op_bc("open", bc.id),
            )
        )
    kb.row(CallbackButton(text="↩️ Назад", payload=cp.OP_MENU))
    return kb.as_markup()


def broadcast_history_card_keyboard(broadcast_id: int, *, has_failures: bool):
    """Карточка рассылки: «создать на основе», «не доставлено», назад."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="📝 Создать на основе",
            payload=cp.op_bc("clone", broadcast_id),
        )
    )
    if has_failures:
        kb.row(
            CallbackButton(
                text="👥 Не доставлено",
                payload=cp.op_bc("failed", broadcast_id),
            )
        )
    kb.row(CallbackButton(text="↩️ К списку", payload=cp.OP_BROADCAST_LIST))
    return kb.as_markup()


def broadcast_failed_list_keyboard(broadcast_id: int):
    """Кнопки под списком failed-доставок: назад к карточке."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="↩️ К рассылке",
            payload=cp.op_bc("open", broadcast_id),
        )
    )
    kb.row(CallbackButton(text="↩️ В админ-меню", payload=cp.OP_MENU))
    return kb.as_markup()


def broadcast_templates_list_keyboard(
    templates: list,
    *,
    can_create: bool = True,
    show_search: bool = True,
):
    """Список шаблонов рассылок (PR H + PR template-editor-upgrade).

    Каждая строка — кнопка-открытие карточки шаблона: payload
    `op:tmpl:open:<id>`. Сверху — «🔍 Найти» (если show_search) и
    «➕ Создать шаблон». Внизу — возврат в админ-меню.
    """
    kb = InlineKeyboardBuilder()
    top_row: list = []
    if show_search:
        top_row.append(CallbackButton(text="🔍 Найти", payload=cp.op_tmpl("search")))
    if can_create:
        top_row.append(
            CallbackButton(text="➕ Создать шаблон", payload=cp.op_tmpl("new"))
        )
    if top_row:
        kb.row(*top_row)
    for tmpl in templates:
        # Префикс «📋» компактно намекает, что это шаблон, а не история
        # рассылок (там «📜»). Имя короткое (≤64 симв); прибавляем
        # компактный индикатор use_count, если шаблон использовали ≥1
        # раз — оператор видит «горячие» сразу.
        use_count = getattr(tmpl, "use_count", 0) or 0
        label = f"📋 {tmpl.name}"
        if use_count > 0:
            label = f"📋 {tmpl.name} · ×{use_count}"
        kb.row(
            CallbackButton(
                text=label,
                payload=cp.op_tmpl(f"open:{tmpl.id}"),
            )
        )
    kb.row(CallbackButton(text="↩️ Назад", payload=cp.OP_MENU))
    return kb.as_markup()


def broadcast_templates_search_results_keyboard(items, query: str):
    """Результаты поиска: те же кнопки-открытия, плюс «🔍 Уточнить» и
    «↩️ К списку»."""
    kb = InlineKeyboardBuilder()
    for tmpl in items:
        use_count = getattr(tmpl, "use_count", 0) or 0
        label = f"📋 {tmpl.name}"
        if use_count > 0:
            label = f"📋 {tmpl.name} · ×{use_count}"
        kb.row(
            CallbackButton(
                text=label,
                payload=cp.op_tmpl(f"open:{tmpl.id}"),
            )
        )
    kb.row(
        CallbackButton(text="🔍 Уточнить запрос", payload=cp.op_tmpl("search")),
        CallbackButton(text="↩️ К списку", payload=cp.op_tmpl("list")),
    )
    return kb.as_markup()


def broadcast_template_preview_keyboard(template_id: int | None):
    """Превью шаблона перед сохранением — «✅ Сохранить» / «↩️ Назад».

    template_id=None — превью при создании (шаг 2½ between text и
    save). template_id=<id> — превью при редактировании существующего.
    «↩️ Назад» возвращает на шаг ввода текста."""
    kb = InlineKeyboardBuilder()
    if template_id is None:
        save_payload = cp.op_tmpl("save_new")
        back_payload = cp.op_tmpl("back_to_text_new")
    else:
        save_payload = cp.op_tmpl(f"save_edit:{template_id}")
        back_payload = cp.op_tmpl(f"back_to_text_edit:{template_id}")
    kb.row(
        CallbackButton(text="✅ Сохранить", payload=save_payload),
        CallbackButton(text="↩️ Назад исправить", payload=back_payload),
    )
    kb.row(CallbackButton(text="❌ Отменить", payload=cp.op_tmpl("cancel")))
    return kb.as_markup()


def broadcast_template_card_keyboard(template_id: int):
    """Карточка шаблона: применить / клонировать / переименовать /
    изменить текст / удалить / назад к списку.

    Кнопка «📨 Отправить как рассылку» — главная цель пула, поэтому
    отдельной строкой сверху. «📑 Клонировать» рядом — частый паттерн
    «у меня есть Отключение воды, нужен ещё один такой же для другого
    района». Удаление и редактирование — отдельным рядом, чтобы
    случайно не нажать.
    """
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="📨 Отправить как рассылку",
            payload=cp.op_tmpl(f"apply:{template_id}"),
        )
    )
    kb.row(
        CallbackButton(
            text="📑 Клонировать",
            payload=cp.op_tmpl(f"clone:{template_id}"),
        )
    )
    kb.row(
        CallbackButton(
            text="✏️ Переименовать",
            payload=cp.op_tmpl(f"rename:{template_id}"),
        ),
        CallbackButton(
            text="📝 Изменить текст",
            payload=cp.op_tmpl(f"edit:{template_id}"),
        ),
    )
    kb.row(
        CallbackButton(
            text="🗑 Удалить шаблон",
            payload=cp.op_tmpl(f"delete:{template_id}"),
        )
    )
    kb.row(CallbackButton(text="↩️ К списку шаблонов", payload=cp.op_tmpl("list")))
    return kb.as_markup()


def broadcast_template_delete_confirm_keyboard(template_id: int):
    """Подтверждение удаления шаблона."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="🗑 Да, удалить",
            payload=cp.op_tmpl(f"delete_ok:{template_id}"),
        ),
        CallbackButton(
            text="↩️ Назад",
            payload=cp.op_tmpl(f"open:{template_id}"),
        ),
    )
    return kb.as_markup()


def broadcast_template_cancel_keyboard():
    """Отмена ввода в wizard'е шаблона (имя/текст/переименование)."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="❌ Отменить", payload=cp.op_tmpl("cancel")))
    return kb.as_markup()


def broadcast_template_step2_keyboard():
    """Клавиатура на шаге 2 (ввод текста+картинок). Кроме «❌ Отменить»
    показывает «↩️ Изменить название» — чтобы оператор мог вернуться
    на шаг 1, если опечатался."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="↩️ Изменить название",
            payload=cp.op_tmpl("back_to_name"),
        )
    )
    kb.row(CallbackButton(text="❌ Отменить", payload=cp.op_tmpl("cancel")))
    return kb.as_markup()


__all__ = [
    "broadcast_unsubscribe_keyboard",
    "broadcast_confirm_keyboard",
    "broadcast_cancel_keyboard",
    "broadcast_stop_keyboard",
    "broadcast_cooldown_keyboard",
    "broadcast_history_list_keyboard",
    "broadcast_history_card_keyboard",
    "broadcast_failed_list_keyboard",
    "broadcast_templates_list_keyboard",
    "broadcast_templates_search_results_keyboard",
    "broadcast_template_preview_keyboard",
    "broadcast_template_card_keyboard",
    "broadcast_template_delete_confirm_keyboard",
    "broadcast_template_cancel_keyboard",
    "broadcast_template_step2_keyboard",
]
