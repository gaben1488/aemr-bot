"""Клавиатуры подсистемы «⚙️ Настройки бота» (admin-only).

Включает:
- `op_settings_menu_keyboard` — главное меню (тексты, URL, тематики,
  населённые пункты, экстренные службы, диспетчеры, автор коммитов,
  тихий режим, PR-flow, expert).
- Подменю texts/urls.
- CRUD-карточки для текстов, list (topics/localities) и obj
  (emergency/transport).
- `op_settings_quiet_*` — тихий режим (toggle + wizard для часов).
- `op_settings_notify_*` — модульные тумблеры служебных уведомлений.
- `op_settings_author_*` — автор коммитов.
- `op_settings_pr_*` — PR-flow.
- `op_settings_expert_*` — экспертный flat-list ключей.
"""
from maxapi.types import CallbackButton, LinkButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from aemr_bot.handlers import callback_payloads as cp


def op_settings_menu_keyboard(dirty_count: int = 0):
    """Главное меню «⚙️ Настройки бота» — иерархическая навигация по
    категориям. dirty_count — число изменённых ключей, не выгруженных
    в репо. Если > 0 — показываем счётчик возле кнопки PR."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="📢 Тексты для жителей", payload=cp.op_set("cat:texts")))
    kb.row(CallbackButton(text="🔗 Внешние ссылки", payload=cp.op_set("cat:urls")))
    kb.row(CallbackButton(text="🏷 Тематики обращений", payload=cp.op_set("list:topics")))
    kb.row(CallbackButton(text="📍 Населённые пункты", payload=cp.op_set("list:localities")))
    kb.row(CallbackButton(text="🆘 Экстренные службы", payload=cp.op_set("obj:emergency_contacts")))
    kb.row(CallbackButton(text="🚌 Диспетчерские транспорта", payload=cp.op_set("obj:transport_dispatcher_contacts")))
    kb.row(CallbackButton(text="👤 Автор коммитов от бота", payload=cp.op_set("author")))
    kb.row(CallbackButton(text="🌙 Тихий режим в админ-чате", payload=cp.op_set("quiet")))
    kb.row(CallbackButton(text="🔔 Уведомления", payload=cp.op_set("notify")))
    pr_label = "💾 Создать PR с изменениями"
    if dirty_count > 0:
        pr_label = f"💾 Создать PR ({dirty_count} изм.)"
    kb.row(CallbackButton(text=pr_label, payload=cp.op_set("pr:start")))
    kb.row(CallbackButton(text="📥 Проверить расхождения с репо", payload=cp.op_set("pr:diff")))
    kb.row(CallbackButton(text="⌨️ Все ключи (для эксперта)", payload=cp.op_set("expert")))
    kb.row(CallbackButton(text="↩️ Назад", payload=cp.OP_MENU))
    return kb.as_markup()


def op_settings_texts_keyboard():
    """Подменю «📢 Тексты для жителей»."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="👋 Приветствие", payload=cp.op_set("text:welcome_text")))
    kb.row(CallbackButton(text="🔐 Текст согласия на ПДн", payload=cp.op_set("text:consent_text")))
    kb.row(CallbackButton(text="🏛 Расписание приёма граждан", payload=cp.op_set("text:appointment_text")))
    kb.row(CallbackButton(text="↩️ Назад", payload=cp.OP_SETTINGS))
    return kb.as_markup()


def op_settings_urls_keyboard():
    """Подменю «🔗 Внешние ссылки»."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="🌐 Электронная приёмная", payload=cp.op_set("url:electronic_reception_url")))
    kb.row(CallbackButton(text="📄 Политика ПДн (ссылка)", payload=cp.op_set("url:policy_url")))
    kb.row(CallbackButton(text="🚌 Пригородные автобусы (УДТХ)", payload=cp.op_set("url:udth_schedule_url")))
    kb.row(CallbackButton(text="🚍 Межмуниципальные маршруты", payload=cp.op_set("url:udth_schedule_intermunicipal_url")))
    kb.row(CallbackButton(text="↩️ Назад", payload=cp.OP_SETTINGS))
    return kb.as_markup()


def op_settings_text_actions_keyboard(key: str):
    """Карточка текстового ключа — «Изменить» / «Назад»."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="✏️ Изменить", payload=cp.op_set(f"edit:{key}")))
    kb.row(CallbackButton(text="↩️ Назад", payload=cp.OP_SETTINGS))
    return kb.as_markup()


def op_settings_text_cancel_keyboard(key: str):
    """Кнопка отмены при ожидании текстового ввода для ключа."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="❌ Отмена", payload=cp.op_set(f"cancel:{key}")))
    return kb.as_markup()


def op_settings_list_keyboard(key: str, items: list[str]):
    """CRUD-меню для строкового списка (topics, localities). Сам список
    показывается в тексте, кнопки — действия над ним."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="➕ Добавить", payload=cp.op_set(f"list_add:{key}")))
    if items:
        # Показываем до 30 элементов по одной кнопке — больше MAX обрежет
        for i, item in enumerate(items[:30]):
            label = item if len(item) <= 45 else item[:42] + "…"
            kb.row(
                CallbackButton(
                    text=f"🗑 {i+1}. {label}",
                    payload=cp.op_set(f"list_del:{key}:{i}"),
                )
            )
    kb.row(CallbackButton(text="↩️ Назад", payload=cp.OP_SETTINGS))
    return kb.as_markup()


def op_settings_obj_keyboard(key: str, items: list[dict]):
    """CRUD-меню для списка объектов (emergency_contacts, transport_dispatcher_contacts).
    Каждый объект — кнопка с краткой подписью; тап откроет действия."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="➕ Добавить", payload=cp.op_set(f"obj_add:{key}")))
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
                payload=cp.op_set(f"obj_view:{key}:{i}"),
            )
        )
    kb.row(CallbackButton(text="↩️ Назад", payload=cp.OP_SETTINGS))
    return kb.as_markup()


def op_settings_obj_item_keyboard(key: str, index: int):
    """Карточка одного объекта — удалить / назад."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="🗑 Удалить запись", payload=cp.op_set(f"obj_del:{key}:{index}")))
    kb.row(CallbackButton(text="↩️ Назад", payload=cp.op_set(f"obj:{key}")))
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
    kb.row(CallbackButton(text=toggle_label, payload=cp.op_set("quiet:toggle")))
    kb.row(CallbackButton(text="✏️ Изменить начало (час)", payload=cp.op_set("quiet:edit:start")))
    kb.row(CallbackButton(text="✏️ Изменить конец (час)", payload=cp.op_set("quiet:edit:end")))
    kb.row(CallbackButton(text="↩️ К настройкам", payload=cp.OP_SETTINGS))
    kb.row(CallbackButton(text="↩️ В админ-меню", payload=cp.OP_MENU))
    return kb.as_markup()


def op_settings_quiet_input_cancel_keyboard():
    """Кнопка «❌ Отмена» под подсказкой ввода часа тихого режима —
    возврат на карточку без сохранения."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="❌ Отмена", payload=cp.op_set("quiet")))
    return kb.as_markup()


# Порядок + подписи тумблеров уведомлений. Единый источник для этой
# клавиатуры И для текста карточки в handlers/admin_settings_notify.py
# (тот модуль импортирует NOTIFY_LABELS отсюда, а не наоборот — так
# зависимость идёт по обычному направлению handlers → ui, без цикла
# ui → handlers, которого нет больше нигде в settings_keyboards.py).
NOTIFY_TOGGLE_KEYS: tuple[str, ...] = (
    "admin_notify_pulse",
    "admin_notify_consent",
    "admin_notify_subscriptions",
    "admin_notify_open_reminder",
    "admin_notify_overdue_reminder",
    "admin_notify_monthly_stats",
)

NOTIFY_LABELS: dict[str, str] = {
    "admin_notify_pulse": "Пульс (heartbeat)",
    "admin_notify_consent": "Согласие на ПДн дано",
    "admin_notify_subscriptions": "Подписки/отписки на рассылку",
    "admin_notify_open_reminder": "Напоминалка: открытые обращения",
    "admin_notify_overdue_reminder": "Напоминалка: просроченные обращения",
    "admin_notify_monthly_stats": "Месячный отчёт (XLSX)",
}


def op_settings_notify_keyboard(values: dict[str, bool]):
    """Карточка «🔔 Уведомления» — шесть независимых тумблеров.

    ``values`` — словарь {key: bool} с текущим состоянием (из БД/кэша).
    Каждая кнопка тапом переключает СВОЙ ключ через
    ``op:set:notify:toggle:<key>`` и перерисовывает карточку.
    """
    kb = InlineKeyboardBuilder()
    for key in NOTIFY_TOGGLE_KEYS:
        enabled = values.get(key, True)
        mark = "✅" if enabled else "⛔"
        kb.row(
            CallbackButton(
                text=f"{mark} {NOTIFY_LABELS[key]}",
                payload=cp.op_set(f"notify:toggle:{key}"),
            )
        )
    kb.row(CallbackButton(text="↩️ К настройкам", payload=cp.OP_SETTINGS))
    kb.row(CallbackButton(text="↩️ В админ-меню", payload=cp.OP_MENU))
    return kb.as_markup()


def op_settings_author_keyboard():
    """Меню «👤 Автор коммитов от бота»."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="✏️ Изменить ФИО", payload=cp.op_set("edit:commit_author_name")))
    kb.row(CallbackButton(text="✏️ Изменить email", payload=cp.op_set("edit:commit_author_email")))
    kb.row(CallbackButton(text="↩️ Назад", payload=cp.OP_SETTINGS))
    return kb.as_markup()


def op_settings_pr_confirm_keyboard():
    """Подтверждение «Создать PR с изменениями»."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="✅ Создать PR", payload=cp.op_set("pr:confirm")))
    kb.row(CallbackButton(text="❌ Отмена", payload=cp.OP_SETTINGS))
    return kb.as_markup()


def op_settings_pr_done_keyboard(pr_url: str | None):
    """После создания PR — кнопка-ссылка на PR + возврат."""
    kb = InlineKeyboardBuilder()
    if pr_url:
        kb.row(LinkButton(text="🔗 Открыть PR в браузере", url=pr_url))
    kb.row(CallbackButton(text="📋 К настройкам", payload=cp.OP_SETTINGS))
    kb.row(CallbackButton(text="↩️ В админ-меню", payload=cp.OP_MENU))
    return kb.as_markup()


def op_settings_expert_keyboard(keys: list[str]):
    """Старый «экспертный» список ключей — оставляем как fallback для
    редких случаев и для совместимости."""
    kb = InlineKeyboardBuilder()
    for key in keys:
        kb.row(CallbackButton(text=key, payload=cp.op_setkey(key)))
    kb.row(CallbackButton(text="↩️ Назад", payload=cp.OP_SETTINGS))
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
    "op_settings_notify_keyboard",
    "NOTIFY_TOGGLE_KEYS",
    "NOTIFY_LABELS",
    "op_settings_author_keyboard",
    "op_settings_pr_confirm_keyboard",
    "op_settings_pr_done_keyboard",
    "op_settings_expert_keyboard",
    "op_settings_keys_keyboard",
]
