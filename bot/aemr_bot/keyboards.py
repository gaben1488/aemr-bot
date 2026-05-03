from maxapi.types import (
    CallbackButton,
    LinkButton,
    RequestContactButton,
)
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder


def main_menu(electronic_reception_url: str | None = None):
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="📝 Написать обращение", payload="menu:new_appeal"))
    kb.row(CallbackButton(text="📂 Мои обращения", payload="menu:my_appeals"))
    if electronic_reception_url:
        kb.row(LinkButton(text="🌐 Электронная приёмная", url=electronic_reception_url))
    kb.row(CallbackButton(text="📋 Приём граждан", payload="menu:appointment"))
    kb.row(CallbackButton(text="📚 Полезная информация", payload="menu:useful_info"))
    return kb.as_markup()


def consent_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="✅ Согласен", payload="consent:yes"),
        CallbackButton(text="❌ Отказаться", payload="consent:no"),
    )
    return kb.as_markup()


def contact_request_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(RequestContactButton(text="📲 Поделиться контактом"))
    kb.row(CallbackButton(text="Отмена", payload="cancel"))
    return kb.as_markup()


def cancel_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="Отмена", payload="cancel"))
    return kb.as_markup()


def submit_or_cancel_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="✅ Отправить", payload="appeal:submit"))
    kb.row(CallbackButton(text="Отмена", payload="cancel"))
    return kb.as_markup()


def topics_keyboard(topics: list[str]):
    kb = InlineKeyboardBuilder()
    pair: list[CallbackButton] = []
    for idx, topic in enumerate(topics):
        pair.append(CallbackButton(text=topic, payload=f"topic:{idx}"))
        if len(pair) == 2:
            kb.row(*pair)
            pair = []
    if pair:
        kb.row(*pair)
    kb.row(CallbackButton(text="Отмена", payload="cancel"))
    return kb.as_markup()


def my_appeals_list_keyboard(
    appeals: list[tuple[int, str]],
    *,
    page: int = 1,
    total_pages: int = 1,
):
    kb = InlineKeyboardBuilder()
    for appeal_id, label in appeals:
        kb.row(CallbackButton(text=label, payload=f"appeal:show:{appeal_id}"))
    if total_pages > 1:
        nav: list[CallbackButton] = []
        if page > 1:
            nav.append(CallbackButton(text="⬅️", payload=f"appeals:page:{page - 1}"))
        nav.append(CallbackButton(text=f"{page}/{total_pages}", payload="appeals:page:noop"))
        if page < total_pages:
            nav.append(CallbackButton(text="➡️", payload=f"appeals:page:{page + 1}"))
        kb.row(*nav)
    kb.row(CallbackButton(text="↩️ В меню", payload="menu:main"))
    return kb.as_markup()


def back_to_menu_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="↩️ В меню", payload="menu:main"))
    return kb.as_markup()


def useful_info_keyboard(
    udth_schedule_url: str | None = None,
    udth_schedule_intermunicipal_url: str | None = None,
    *,
    subscribe_label: str = "🔔 Подписаться на новости",
):
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="☎️ Телефоны экстренных и аварийных служб",
            payload="info:emergency",
        )
    )
    if udth_schedule_url:
        kb.row(LinkButton(text="🚌 Муниципальные маршруты", url=udth_schedule_url))
    if udth_schedule_intermunicipal_url:
        kb.row(
            LinkButton(
                text="🚍 Межмуниципальные маршруты",
                url=udth_schedule_intermunicipal_url,
            )
        )
    kb.row(
        CallbackButton(
            text="📞 Диспетчерские автотранспорта",
            payload="info:dispatchers",
        )
    )
    kb.row(CallbackButton(text=subscribe_label, payload="info:subscribe_toggle"))
    kb.row(CallbackButton(text="↩️ В меню", payload="menu:main"))
    return kb.as_markup()


def broadcast_unsubscribe_keyboard():
    """Inline button under each broadcast message — one-tap unsubscribe."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="🔕 Отписаться от рассылки",
            payload="broadcast:unsubscribe",
        )
    )
    return kb.as_markup()


def broadcast_confirm_keyboard():
    """Wizard step — operator confirms or aborts the prepared broadcast."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="✅ Разослать", payload="broadcast:confirm"),
        CallbackButton(text="❌ Отмена", payload="broadcast:abort"),
    )
    return kb.as_markup()


def broadcast_stop_keyboard(broadcast_id: int):
    """Emergency-stop button visible to all operators while a send is running."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="⛔ Экстренно остановить",
            payload=f"broadcast:stop:{broadcast_id}",
        )
    )
    return kb.as_markup()


def op_help_keyboard():
    """Quick-action keyboard pinned in the admin chat — closest thing MAX has
    to a Telegram-style menu button. Each callback fires the corresponding
    flow without typing a command."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="📊 Статистика за сегодня", payload="op:stats_today")
    )
    kb.row(CallbackButton(text="📢 Сделать рассылку", payload="op:broadcast"))
    kb.row(
        CallbackButton(text="📋 Все команды", payload="op:help_full")
    )
    return kb.as_markup()
