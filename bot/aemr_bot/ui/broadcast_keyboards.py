"""Клавиатуры для рассылок (broadcast) и шаблонов.

Включает:
- Кнопку отписки под каждым сообщением рассылки (citizen-facing).
- Wizard оператора: confirm, cancel, stop, cooldown.
- История рассылок и список «не доставлено».
- CRUD шаблонов: list, search, preview, card, delete-confirm, step-2.
"""
from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder


def broadcast_unsubscribe_keyboard():
    """Inline-кнопка под каждым сообщением рассылки — отписка в одно нажатие."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="🔕 Отписаться от рассылки",
            payload="broadcast:unsubscribe",
        )
    )
    kb.row(CallbackButton(text="↩️ В меню", payload="menu:main"))
    return kb.as_markup()


def broadcast_confirm_keyboard():
    """Шаг анкеты: оператор подтверждает, переписывает или отменяет рассылку.

    Кнопка «Изменить текст» возвращает мастер в шаг awaiting_text без
    потери уже введённого. Раньше для исправления опечатки приходилось
    отменять и заново вводить текст с нуля.
    """
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="✅ Разослать", payload="broadcast:confirm"),
        CallbackButton(text="✏️ Изменить текст", payload="broadcast:edit"),
    )
    kb.row(CallbackButton(text="❌ Отмена", payload="broadcast:abort"))
    return kb.as_markup()


def broadcast_cancel_keyboard():
    """Кнопка отмены под промптом «введите текст рассылки». Чтобы оператор
    мог выйти из мастера в один тап вместо набора /cancel."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="❌ Отменить рассылку", payload="broadcast:abort"))
    return kb.as_markup()


def broadcast_stop_keyboard(broadcast_id: int):
    """Кнопка экстренной остановки, видимая всем операторам, пока идёт рассылка."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="⛔ Экстренно остановить",
            payload=f"broadcast:stop:{broadcast_id}",
        )
    )
    kb.row(CallbackButton(text="↩️ В админ-меню", payload="op:menu"))
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
            payload=f"broadcast:cancel-cooldown:{broadcast_id}",
        )
    )
    kb.row(CallbackButton(text="↩️ В админ-меню", payload="op:menu"))
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
                payload=f"op:bc:open:{bc.id}",
            )
        )
    kb.row(CallbackButton(text="↩️ Назад", payload="op:menu"))
    return kb.as_markup()


def broadcast_history_card_keyboard(broadcast_id: int, *, has_failures: bool):
    """Карточка рассылки: «создать на основе», «не доставлено», назад."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="📝 Создать на основе",
            payload=f"op:bc:clone:{broadcast_id}",
        )
    )
    if has_failures:
        kb.row(
            CallbackButton(
                text="👥 Не доставлено",
                payload=f"op:bc:failed:{broadcast_id}",
            )
        )
    kb.row(CallbackButton(text="↩️ К списку", payload="op:broadcast_list"))
    return kb.as_markup()


def broadcast_failed_list_keyboard(broadcast_id: int):
    """Кнопки под списком failed-доставок: назад к карточке."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="↩️ К рассылке",
            payload=f"op:bc:open:{broadcast_id}",
        )
    )
    kb.row(CallbackButton(text="↩️ В админ-меню", payload="op:menu"))
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
        top_row.append(CallbackButton(text="🔍 Найти", payload="op:tmpl:search"))
    if can_create:
        top_row.append(
            CallbackButton(text="➕ Создать шаблон", payload="op:tmpl:new")
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
                payload=f"op:tmpl:open:{tmpl.id}",
            )
        )
    kb.row(CallbackButton(text="↩️ Назад", payload="op:menu"))
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
                payload=f"op:tmpl:open:{tmpl.id}",
            )
        )
    kb.row(
        CallbackButton(text="🔍 Уточнить запрос", payload="op:tmpl:search"),
        CallbackButton(text="↩️ К списку", payload="op:tmpl:list"),
    )
    return kb.as_markup()


def broadcast_template_preview_keyboard(template_id: int | None):
    """Превью шаблона перед сохранением — «✅ Сохранить» / «↩️ Назад».

    template_id=None — превью при создании (шаг 2½ between text и
    save). template_id=<id> — превью при редактировании существующего.
    «↩️ Назад» возвращает на шаг ввода текста."""
    kb = InlineKeyboardBuilder()
    if template_id is None:
        save_payload = "op:tmpl:save_new"
        back_payload = "op:tmpl:back_to_text_new"
    else:
        save_payload = f"op:tmpl:save_edit:{template_id}"
        back_payload = f"op:tmpl:back_to_text_edit:{template_id}"
    kb.row(
        CallbackButton(text="✅ Сохранить", payload=save_payload),
        CallbackButton(text="↩️ Назад исправить", payload=back_payload),
    )
    kb.row(CallbackButton(text="❌ Отменить", payload="op:tmpl:cancel"))
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
            payload=f"op:tmpl:apply:{template_id}",
        )
    )
    kb.row(
        CallbackButton(
            text="📑 Клонировать",
            payload=f"op:tmpl:clone:{template_id}",
        )
    )
    kb.row(
        CallbackButton(
            text="✏️ Переименовать",
            payload=f"op:tmpl:rename:{template_id}",
        ),
        CallbackButton(
            text="📝 Изменить текст",
            payload=f"op:tmpl:edit:{template_id}",
        ),
    )
    kb.row(
        CallbackButton(
            text="🗑 Удалить шаблон",
            payload=f"op:tmpl:delete:{template_id}",
        )
    )
    kb.row(CallbackButton(text="↩️ К списку шаблонов", payload="op:tmpl:list"))
    return kb.as_markup()


def broadcast_template_delete_confirm_keyboard(template_id: int):
    """Подтверждение удаления шаблона."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="🗑 Да, удалить",
            payload=f"op:tmpl:delete_ok:{template_id}",
        ),
        CallbackButton(
            text="↩️ Назад",
            payload=f"op:tmpl:open:{template_id}",
        ),
    )
    return kb.as_markup()


def broadcast_template_cancel_keyboard():
    """Отмена ввода в wizard'е шаблона (имя/текст/переименование)."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="❌ Отменить", payload="op:tmpl:cancel"))
    return kb.as_markup()


def broadcast_template_step2_keyboard():
    """Клавиатура на шаге 2 (ввод текста+картинок). Кроме «❌ Отменить»
    показывает «↩️ Изменить название» — чтобы оператор мог вернуться
    на шаг 1, если опечатался."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="↩️ Изменить название",
            payload="op:tmpl:back_to_name",
        )
    )
    kb.row(CallbackButton(text="❌ Отменить", payload="op:tmpl:cancel"))
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
