"""Клавиатуры подсистемы «⚙️ Настройки бота» (admin-only).

Включает:
- `op_settings_menu_keyboard` — главное меню (тексты, URL, тематики,
  населённые пункты, экстренные службы, диспетчеры, автор коммитов,
  тихий режим, PR-flow, expert).
- Подменю texts/urls.
- CRUD-карточки для текстов, list (topics/localities) и obj
  (emergency/transport).
- `op_settings_quiet_*` — тихий режим (toggle + wizard для часов).
- `op_settings_author_*` — автор коммитов.
- `op_settings_pr_*` — PR-flow.
- `op_settings_expert_*` — экспертный flat-list ключей.
"""
from maxapi.types import CallbackButton, LinkButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder


def op_settings_menu_keyboard(dirty_count: int = 0):
    """Главное меню «⚙️ Настройки бота» — иерархическая навигация по
    категориям. dirty_count — число изменённых ключей, не выгруженных
    в репо. Если > 0 — показываем счётчик возле кнопки PR."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="📢 Тексты для жителей", payload="op:set:cat:texts"))
    kb.row(CallbackButton(text="🔗 Внешние ссылки", payload="op:set:cat:urls"))
    kb.row(CallbackButton(text="🏷 Тематики обращений", payload="op:set:list:topics"))
    kb.row(CallbackButton(text="📍 Населённые пункты", payload="op:set:list:localities"))
    kb.row(CallbackButton(text="🆘 Экстренные службы", payload="op:set:obj:emergency_contacts"))
    kb.row(CallbackButton(text="🚌 Диспетчерские транспорта", payload="op:set:obj:transport_dispatcher_contacts"))
    kb.row(CallbackButton(text="👤 Автор коммитов от бота", payload="op:set:author"))
    kb.row(CallbackButton(text="🌙 Тихий режим в админ-чате", payload="op:set:quiet"))
    pr_label = "💾 Создать PR с изменениями"
    if dirty_count > 0:
        pr_label = f"💾 Создать PR ({dirty_count} изм.)"
    kb.row(CallbackButton(text=pr_label, payload="op:set:pr:start"))
    kb.row(CallbackButton(text="📥 Проверить расхождения с репо", payload="op:set:pr:diff"))
    kb.row(CallbackButton(text="⌨️ Все ключи (для эксперта)", payload="op:set:expert"))
    kb.row(CallbackButton(text="↩️ Назад", payload="op:menu"))
    return kb.as_markup()


def op_settings_texts_keyboard():
    """Подменю «📢 Тексты для жителей»."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="👋 Приветствие", payload="op:set:text:welcome_text"))
    kb.row(CallbackButton(text="🔐 Текст согласия на ПДн", payload="op:set:text:consent_text"))
    kb.row(CallbackButton(text="🏛 Расписание приёма граждан", payload="op:set:text:appointment_text"))
    kb.row(CallbackButton(text="↩️ Назад", payload="op:settings"))
    return kb.as_markup()


def op_settings_urls_keyboard():
    """Подменю «🔗 Внешние ссылки»."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="🌐 Электронная приёмная", payload="op:set:url:electronic_reception_url"))
    kb.row(CallbackButton(text="📄 Политика ПДн (ссылка)", payload="op:set:url:policy_url"))
    kb.row(CallbackButton(text="🚌 Пригородные автобусы (УДТХ)", payload="op:set:url:udth_schedule_url"))
    kb.row(CallbackButton(text="🚍 Межмуниципальные маршруты", payload="op:set:url:udth_schedule_intermunicipal_url"))
    kb.row(CallbackButton(text="↩️ Назад", payload="op:settings"))
    return kb.as_markup()


def op_settings_text_actions_keyboard(key: str):
    """Карточка текстового ключа — «Изменить» / «Назад»."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="✏️ Изменить", payload=f"op:set:edit:{key}"))
    kb.row(CallbackButton(text="↩️ Назад", payload="op:settings"))
    return kb.as_markup()


def op_settings_text_cancel_keyboard(key: str):
    """Кнопка отмены при ожидании текстового ввода для ключа."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="❌ Отмена", payload=f"op:set:cancel:{key}"))
    return kb.as_markup()


def op_settings_list_keyboard(key: str, items: list[str]):
    """CRUD-меню для строкового списка (topics, localities). Сам список
    показывается в тексте, кнопки — действия над ним."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="➕ Добавить", payload=f"op:set:list_add:{key}"))
    if items:
        # Показываем до 30 элементов по одной кнопке — больше MAX обрежет
        for i, item in enumerate(items[:30]):
            label = item if len(item) <= 45 else item[:42] + "…"
            kb.row(
                CallbackButton(
                    text=f"🗑 {i+1}. {label}",
                    payload=f"op:set:list_del:{key}:{i}",
                )
            )
    kb.row(CallbackButton(text="↩️ Назад", payload="op:settings"))
    return kb.as_markup()


def op_settings_obj_keyboard(key: str, items: list[dict]):
    """CRUD-меню для списка объектов (emergency_contacts, transport_dispatcher_contacts).
    Каждый объект — кнопка с краткой подписью; тап откроет действия."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="➕ Добавить", payload=f"op:set:obj_add:{key}"))
    for i, item in enumerate(items[:20]):
        # Подпись зависит от типа: для emergency — name+phone, для
        # transport — routes+phone. Берём первое непустое поле для
        # отображения.
        name = item.get("name") or item.get("routes") or "?"
        phone = item.get("phone") or ""
        label = f"{name} — {phone}" if phone else str(name)
        if len(label) > 45:
            label = label[:42] + "…"
        kb.row(
            CallbackButton(
                text=f"{i+1}. {label}",
                payload=f"op:set:obj_view:{key}:{i}",
            )
        )
    kb.row(CallbackButton(text="↩️ Назад", payload="op:settings"))
    return kb.as_markup()


def op_settings_obj_item_keyboard(key: str, index: int):
    """Карточка одного объекта — удалить / назад."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="🗑 Удалить запись", payload=f"op:set:obj_del:{key}:{index}"))
    kb.row(CallbackButton(text="↩️ Назад", payload=f"op:set:obj:{key}"))
    return kb.as_markup()


def op_settings_quiet_keyboard(*, enabled: bool):
    """Карточка «🌙 Тихий режим» — toggle + wizard для часов start/end.

    Edit start/end через стандартный intent flow (как для текстовых
    ключей): оператор тапает «✏️ Изменить начало» → бот просит ввести
    число 0–23 → следующее сообщение оператора сохраняется + cache
    обновляется + карточка перерисовывается с новым значением.
    """
    kb = InlineKeyboardBuilder()
    toggle_label = (
        "🔕 Выключить тихий режим" if enabled
        else "🔔 Включить тихий режим"
    )
    kb.row(CallbackButton(text=toggle_label, payload="op:set:quiet:toggle"))
    kb.row(CallbackButton(text="✏️ Изменить начало (час)", payload="op:set:quiet:edit:start"))
    kb.row(CallbackButton(text="✏️ Изменить конец (час)", payload="op:set:quiet:edit:end"))
    kb.row(CallbackButton(text="↩️ К настройкам", payload="op:settings"))
    kb.row(CallbackButton(text="↩️ В админ-меню", payload="op:menu"))
    return kb.as_markup()


def op_settings_quiet_input_cancel_keyboard():
    """Кнопка «❌ Отмена» под подсказкой ввода часа тихого режима —
    возврат на карточку без сохранения."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="❌ Отмена", payload="op:set:quiet"))
    return kb.as_markup()


def op_settings_author_keyboard():
    """Меню «👤 Автор коммитов от бота»."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="✏️ Изменить ФИО", payload="op:set:edit:commit_author_name"))
    kb.row(CallbackButton(text="✏️ Изменить email", payload="op:set:edit:commit_author_email"))
    kb.row(CallbackButton(text="↩️ Назад", payload="op:settings"))
    return kb.as_markup()


def op_settings_pr_confirm_keyboard():
    """Подтверждение «Создать PR с изменениями»."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="✅ Создать PR", payload="op:set:pr:confirm"))
    kb.row(CallbackButton(text="❌ Отмена", payload="op:settings"))
    return kb.as_markup()


def op_settings_pr_done_keyboard(pr_url: str | None):
    """После создания PR — кнопка-ссылка на PR + возврат."""
    kb = InlineKeyboardBuilder()
    if pr_url:
        kb.row(LinkButton(text="🔗 Открыть PR в браузере", url=pr_url))
    kb.row(CallbackButton(text="📋 К настройкам", payload="op:settings"))
    kb.row(CallbackButton(text="↩️ В админ-меню", payload="op:menu"))
    return kb.as_markup()


def op_settings_expert_keyboard(keys: list[str]):
    """Старый «экспертный» список ключей — оставляем как fallback для
    редких случаев и для совместимости."""
    kb = InlineKeyboardBuilder()
    for key in keys:
        kb.row(CallbackButton(text=key, payload=f"op:setkey:{key}"))
    kb.row(CallbackButton(text="↩️ Назад", payload="op:settings"))
    return kb.as_markup()


def op_settings_keys_keyboard(keys: list[str]):
    """Совместимость: старая клавиатура «список ключей». Перенаправляем
    на новую expert-карточку."""
    return op_settings_expert_keyboard(keys)


__all__ = [
    "op_settings_menu_keyboard",
    "op_settings_texts_keyboard",
    "op_settings_urls_keyboard",
    "op_settings_text_actions_keyboard",
    "op_settings_text_cancel_keyboard",
    "op_settings_list_keyboard",
    "op_settings_obj_keyboard",
    "op_settings_obj_item_keyboard",
    "op_settings_quiet_keyboard",
    "op_settings_quiet_input_cancel_keyboard",
    "op_settings_author_keyboard",
    "op_settings_pr_confirm_keyboard",
    "op_settings_pr_done_keyboard",
    "op_settings_expert_keyboard",
    "op_settings_keys_keyboard",
]
