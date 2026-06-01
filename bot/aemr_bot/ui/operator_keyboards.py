"""Клавиатуры операторской панели (admin-chat).

Включает:
- `op_help_keyboard` — главное меню оператора.
- `op_help_main_keyboard` / `op_help_security_keyboard` — двухэкранная
  памятка (после удаления `OP_HELP_FULL_LEGACY` overflow).
- `open_tickets_listing_keyboard` — listing открытых обращений.
- `op_stats_menu_keyboard` — выбор периода для XLSX.
- `op_audience_menu_keyboard` + `op_audience_user_actions` — управление
  жителями (block/unblock/erase).
- `appeal_admin_actions` — кнопки под карточкой обращения.
- `cancel_reply_intent_keyboard` — отмена ответа оператора.
- Утилитарные `op_back_to_*_keyboard` для возврата.
"""
from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from aemr_bot.db.models import AppealStatus
from aemr_bot.handlers import callback_payloads as cp


def cancel_reply_intent_keyboard():
    """Кнопка «❌ Отменить» под подсказкой ввода ответа. Без неё intent
    мог жить 5 минут, и любой следующий текст оператора уходил жителю —
    в т.ч. случайные «окей», текст для другого обращения, ввод wizard'а."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="❌ Отменить ответ", payload=cp.OP_REPLY_CANCEL))
    return kb.as_markup()


def op_back_to_menu_keyboard():
    """Одна кнопка возврата к главной операторской панели."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="↩️ Назад", payload=cp.OP_MENU))
    return kb.as_markup()


def op_help_main_keyboard():
    """Клавиатура главного экрана памятки оператора (📋).

    Содержит переход на второй экран `🛡️ Безопасность и антифишинг`
    + возврат в админ-меню. Разбивка на 2 экрана нужна, потому что
    OP_HELP_FULL_LEGACY (~8230 char) превышал MAX-API limit 4000 char.
    """
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="🛡️ Безопасность и антифишинг",
            payload=cp.OP_HELP_SECURITY,
        )
    )
    kb.row(CallbackButton(text="↩️ В админ-меню", payload=cp.OP_MENU))
    return kb.as_markup()


def op_help_security_keyboard():
    """Клавиатура второго экрана памятки оператора (🛡️ Безопасность).

    Содержит возврат на главный экран памятки + escape в админ-меню.
    """
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="↩️ Назад к памятке",
            payload=cp.OP_HELP_FULL,
        )
    )
    kb.row(CallbackButton(text="↩️ В админ-меню", payload=cp.OP_MENU))
    return kb.as_markup()


def open_tickets_listing_keyboard(items):
    """Listing открытых обращений в одном сообщении.

    Каждое обращение — одна кнопка-строка: «#N · 🆕 · тема (фрагмент)»
    или «#N · 🔄 · ...». Тап → callback `op:open_card:N` → полная
    карточка с timeline через `admin_card.render` (по выбору владельца
    шаг 2-в от 2026-05-26: listing компактный, история открывается
    в новой карточке внизу чата).

    `items` — последовательность `(appeal_id, status, topic_preview)`,
    где `topic_preview` уже обрезан до разумной длины вызывающим.
    Если items пуст — клавиатура только с кнопкой «↩️ В меню».
    """
    kb = InlineKeyboardBuilder()
    status_emoji = {
        AppealStatus.NEW.value: "🆕",
        AppealStatus.IN_PROGRESS.value: "🔄",
        AppealStatus.ANSWERED.value: "✅",
        AppealStatus.CLOSED.value: "⛔",
    }
    for appeal_id, status, topic_preview in items:
        emoji = status_emoji.get(status, "•")
        text = f"#{appeal_id} · {emoji} · {topic_preview}"
        kb.row(
            CallbackButton(
                text=text,
                payload=cp.op_open_card(appeal_id),
            )
        )
    kb.row(CallbackButton(text="↩️ В админ-меню", payload=cp.OP_MENU))
    return kb.as_markup()


def op_back_to_operators_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="↩️ К операторам", payload=cp.OP_OPERATORS))
    kb.row(CallbackButton(text="↩️ В админ-меню", payload=cp.OP_MENU))
    return kb.as_markup()


def op_back_to_settings_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="↩️ К настройкам", payload=cp.OP_SETTINGS))
    kb.row(CallbackButton(text="↩️ В админ-меню", payload=cp.OP_MENU))
    return kb.as_markup()


def op_back_to_audience_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="↩️ К аудитории", payload=cp.OP_AUDIENCE))
    kb.row(CallbackButton(text="↩️ В админ-меню", payload=cp.OP_MENU))
    return kb.as_markup()


def op_stats_menu_keyboard():
    """Подменю «📊 Статистика» — выбор периода. По одной кнопке в ряд:
    длинные подписи («За полгода», «За всё время») в две колонки
    обрезаются на узких экранах MAX. После клика по периоду бот
    отправляет XLSX и возвращает оператору главную панель /op_help."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="📊 За сегодня", payload=cp.OP_STATS_TODAY))
    kb.row(CallbackButton(text="📊 За неделю", payload=cp.OP_STATS_WEEK))
    kb.row(CallbackButton(text="📊 За месяц", payload=cp.OP_STATS_MONTH))
    kb.row(CallbackButton(text="📊 За квартал", payload=cp.OP_STATS_QUARTER))
    kb.row(CallbackButton(text="📊 За полгода", payload=cp.OP_STATS_HALF_YEAR))
    kb.row(CallbackButton(text="📊 За год", payload=cp.OP_STATS_YEAR))
    kb.row(CallbackButton(text="📊 За всё время", payload=cp.OP_STATS_ALL))
    kb.row(CallbackButton(text="↩️ Назад", payload=cp.OP_MENU))
    return kb.as_markup()


def op_audience_menu_keyboard():
    """Меню «📊 Аудитория и согласия» в админ-панели для роли it.
    Три выборки: подписчики, давшие согласие, заблокированные.
    Каждая открывается отдельным сообщением со списком до 20 записей."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="📩 Подписчики", payload=cp.op_aud("subs")))
    kb.row(CallbackButton(text="🔐 Дали согласие", payload=cp.op_aud("consent")))
    kb.row(CallbackButton(text="🚫 Заблокированные", payload=cp.op_aud("blocked")))
    kb.row(CallbackButton(text="↩️ Назад", payload=cp.OP_MENU))
    return kb.as_markup()


def op_audience_user_actions(max_user_id: int, *, blocked: bool):
    """Кнопки действий рядом с конкретным жителем в выводе «Аудитория».
    Минимальный набор: разблок/блок и удаление ПДн. Подписку можно
    отозвать через `/setting` или попросить жителя отписаться."""
    kb = InlineKeyboardBuilder()
    if blocked:
        kb.row(
            CallbackButton(
                text="✅ Разблокировать", payload=cp.op_aud(f"unblock:{max_user_id}")
            ),
        )
    else:
        kb.row(
            CallbackButton(
                text="🚫 Заблокировать", payload=cp.op_aud(f"block:{max_user_id}")
            ),
        )
    kb.row(
        CallbackButton(
            text="🗑 Удалить ПДн", payload=cp.op_aud(f"erase:{max_user_id}")
        ),
    )
    kb.row(CallbackButton(text="↩️ К аудитории", payload=cp.OP_AUDIENCE))
    kb.row(CallbackButton(text="↩️ В админ-меню", payload=cp.OP_MENU))
    return kb.as_markup()


def op_audience_paginated_list_keyboard(
    category: str,
    rows: list[tuple[int, str]],
    *,
    page: int = 1,
    total_pages: int = 1,
):
    """Master-listing аудитории: один сообщение с кликабельными
    строками + пагинация + bulk-dump + search.

    `category` ∈ {`subs`, `consent`, `blocked`}.
    `rows` — список (max_user_id, label) где label уже отформатирован
    вызывающим (`#max_id · Имя · +7***1234 · 🔔✅`). Каждый row —
    кликабельная кнопка, открывает карточку конкретного жителя через
    callback `op:aud:show:<max_user_id>`.

    Pagination кнопки появляются только если total_pages > 1.

    Кнопка «📤 Выдать всех» — отдельный bulk-dump (10 individual
    карточек). Кнопка «🔍 Поиск» — intent flow по имени/телефону/id.
    """
    kb = InlineKeyboardBuilder()
    for max_user_id, label in rows:
        kb.row(
            CallbackButton(
                text=label,
                payload=cp.op_aud(f"show:{max_user_id}"),
            )
        )
    # Pagination row — показываем только при >1 странице.
    if total_pages > 1:
        nav: list[CallbackButton] = []
        if page > 1:
            nav.append(
                CallbackButton(
                    text="⬅️",
                    payload=cp.op_aud(f"page:{category}:{page - 1}"),
                )
            )
        nav.append(
            CallbackButton(
                text=f"{page} / {total_pages}",
                payload=cp.op_aud(f"page:{category}:noop"),
            )
        )
        if page < total_pages:
            nav.append(
                CallbackButton(
                    text="➡️",
                    payload=cp.op_aud(f"page:{category}:{page + 1}"),
                )
            )
        kb.row(*nav)
    # Bulk + search: только при непустых rows. Поиск тоже доступен —
    # удобно когда оператор открыл list и хочет сразу найти кого-то
    # конкретного.
    if rows:
        kb.row(
            CallbackButton(
                text=f"📤 Выдать всех на странице ({len(rows)})",
                payload=cp.op_aud(f"dump:{category}:{page}"),
            )
        )
    kb.row(
        CallbackButton(
            text="🔍 Найти жителя",
            payload=cp.op_aud(f"search:{category}"),
        )
    )
    kb.row(CallbackButton(text="↩️ К аудитории", payload=cp.OP_AUDIENCE))
    kb.row(CallbackButton(text="↩️ В админ-меню", payload=cp.OP_MENU))
    return kb.as_markup()


def op_audience_search_cancel_keyboard(category: str | None = None):
    """Кнопка отмены под подсказкой ввода поиска. Возврат — в исходную
    категорию если задана, иначе в корневое меню аудитории."""
    kb = InlineKeyboardBuilder()
    if category in {"subs", "consent", "blocked"}:
        kb.row(
            CallbackButton(
                text="↩️ К списку",
                payload=cp.op_aud(category),
            )
        )
    kb.row(CallbackButton(text="↩️ К аудитории", payload=cp.OP_AUDIENCE))
    kb.row(CallbackButton(text="↩️ В админ-меню", payload=cp.OP_MENU))
    return kb.as_markup()


def op_audience_user_card_keyboard(max_user_id: int, *, blocked: bool, category: str | None = None):
    """Карточка отдельного жителя из аудиторий — расширенная версия
    `op_audience_user_actions` с возвратом в исходный paginated
    listing вместо короткого «↩️ К аудитории».

    `category` — категория откуда открыли карточку (`subs`/`consent`/
    `blocked`); если задано, кнопка возврата вернёт на страницу
    list'инга, а не в корневое меню аудитории.
    """
    kb = InlineKeyboardBuilder()
    if blocked:
        kb.row(
            CallbackButton(
                text="✅ Разблокировать",
                payload=cp.op_aud(f"unblock:{max_user_id}"),
            )
        )
    else:
        kb.row(
            CallbackButton(
                text="🚫 Заблокировать",
                payload=cp.op_aud(f"block:{max_user_id}"),
            )
        )
    kb.row(
        CallbackButton(
            text="🗑 Удалить ПДн",
            payload=cp.op_aud(f"erase:{max_user_id}"),
        )
    )
    if category in {"subs", "consent", "blocked"}:
        kb.row(
            CallbackButton(
                text="↩️ К списку",
                payload=cp.op_aud(category),
            )
        )
    kb.row(CallbackButton(text="↩️ К аудитории", payload=cp.OP_AUDIENCE))
    kb.row(CallbackButton(text="↩️ В админ-меню", payload=cp.OP_MENU))
    return kb.as_markup()


def appeal_admin_actions(
    appeal_id: int,
    status: str,
    *,
    is_it: bool = False,
    user_blocked: bool = False,
    closed_due_to_revoke: bool = False,
    attachment_count: int = 0,
):
    """Кнопки действий под карточкой обращения в админ-группе.

    Набор кнопок зависит от статуса:
    - new / in_progress: «✉️ Ответить», «⛔ Закрыть без ответа»
    - answered / closed: «🔁 Возобновить»
    Для роли it дополнительно: «🚫 Заблокировать жителя» (или
    «✅ Разблокировать», если уже заблокирован) и «🗑 Удалить ПДн жителя».

    closed_due_to_revoke=True — обращение закрыто из-за отзыва согласия
    или удаления данных жителем. Возобновлять бессмысленно: гард
    доставки в `_deliver_operator_reply` всё равно откажет (consent
    отозван). Поэтому кнопку «🔁 Возобновить» не показываем — экономим
    оператору время на тыкание в неработающую кнопку.

    attachment_count>0 — у обращения есть вложения, добавляем кнопку
    «📎 Вложения (N)». Тап → callback `op:atts:<id>` → переотправка
    всех вложений рядом с карточкой. ДО PR #47 это происходило
    автоматически при listing'е и приводило к hang'у — теперь только
    по явному тапу.
    """
    kb = InlineKeyboardBuilder()
    open_states = {AppealStatus.NEW.value, AppealStatus.IN_PROGRESS.value}
    closed_states = {AppealStatus.ANSWERED.value, AppealStatus.CLOSED.value}
    if status in open_states:
        # Две кнопки reply в РАЗНЫХ строках — узкий экран MAX обрезает
        # длинные надписи в одной строке, оператор не видел кнопку
        # «Промежуточный». Финальный лейбл явно говорит «и закрыть»,
        # промежуточный — «без закрытия».
        kb.row(
            CallbackButton(
                text="✉️ Ответить и закрыть",
                payload=cp.op_reply(appeal_id),
            ),
        )
        kb.row(
            CallbackButton(
                text="💬 Ответить промежуточно (не закрывая)",
                payload=cp.op_replyint(appeal_id),
            ),
        )
        kb.row(
            CallbackButton(
                text="⛔ Закрыть без ответа", payload=cp.op_close(appeal_id)
            ),
        )
    elif status in closed_states and not closed_due_to_revoke:
        kb.row(
            CallbackButton(
                text="🔁 Возобновить", payload=cp.op_reopen(appeal_id)
            ),
        )
    if attachment_count > 0:
        kb.row(
            CallbackButton(
                text=f"📎 Вложения ({attachment_count})",
                payload=cp.op_atts(appeal_id),
            ),
        )
    if is_it:
        block_label = (
            "✅ Разблокировать" if user_blocked else "🚫 Заблокировать"
        )
        block_payload = (
            cp.op_unblock(appeal_id) if user_blocked else cp.op_block(appeal_id)
        )
        kb.row(
            CallbackButton(text=block_label, payload=block_payload),
            CallbackButton(text="🗑 Удалить ПДн", payload=cp.op_erase(appeal_id)),
        )
    kb.row(CallbackButton(text="↩️ В админ-меню", payload=cp.OP_MENU))
    return kb.as_markup()


def op_help_keyboard(
    *,
    open_count: int | None = None,
    is_it: bool = False,
    can_broadcast: bool = False,
):
    """Клавиатура быстрых действий, закреплённая в админ-чате: ближайший
    аналог telegram-кнопки меню, который есть в MAX. Каждое нажатие
    запускает соответствующий сценарий без ввода команды.

    Цель — свести к минимуму команды, которые приходится набирать
    руками. Команды с обязательными аргументами для роли it (/erase,
    /setting, /add_operators) проводятся через кнопочный wizard.

    open_count — число открытых обращений; если задано, показывается
    рядом с кнопкой «Открытые обращения», чтобы координатор сразу
    видел нагрузку.

    is_it — если оператор IT, показываем дополнительный ряд админ-
    кнопок (управление операторами, настройки, удалить ПДн, бэкап).

    can_broadcast — IT и COORDINATOR могут запускать рассылки. Для
    AEMR/EGP кнопки рассылок и истории не показываем — они всё равно
    получили бы отказ от _ensure_role и плодили бы шум в чате.
    """
    # Все кнопки по одной в строку — длинные русские подписи
    # («📜 История рассылок», «👥 Операторы», «📊 Аудитория и согласия»)
    # в две колонки на узких экранах MAX обрезаются до «...». Один ряд —
    # один смысл, ничего не теряется.
    kb = InlineKeyboardBuilder()
    open_label = "📋 Открытые обращения"
    if open_count is not None:
        open_label = f"📋 Открытые обращения ({open_count})"
    kb.row(CallbackButton(text=open_label, payload=cp.OP_OPEN_TICKETS))
    kb.row(CallbackButton(text="📊 Статистика", payload=cp.OP_STATS_MENU))
    if can_broadcast:
        kb.row(CallbackButton(text="📢 Сделать рассылку", payload=cp.OP_BROADCAST))
        kb.row(CallbackButton(text="📜 История рассылок", payload=cp.OP_BROADCAST_LIST))
        kb.row(CallbackButton(text="📋 Шаблоны рассылок", payload=cp.op_tmpl("list")))
    kb.row(CallbackButton(text="🛠 Диагностика", payload=cp.OP_DIAG))
    if is_it:
        kb.row(CallbackButton(text="💾 Снять бэкап", payload=cp.OP_BACKUP))
        kb.row(CallbackButton(text="👥 Операторы", payload=cp.OP_OPERATORS))
        kb.row(CallbackButton(text="⚙️ Настройки бота", payload=cp.OP_SETTINGS))
        kb.row(CallbackButton(text="📊 Аудитория и согласия", payload=cp.OP_AUDIENCE))
    # «📋 Памятка оператора» — полная инструкция в отдельном подменю
    # (раньше она печаталась простыней в каждом вызове админ-меню,
    # перегружая чат). Доступна любой роли.
    kb.row(CallbackButton(text="📋 Памятка оператора", payload=cp.OP_HELP_FULL))
    return kb.as_markup()


__all__ = [
    "cancel_reply_intent_keyboard",
    "op_back_to_menu_keyboard",
    "op_help_main_keyboard",
    "op_help_security_keyboard",
    "open_tickets_listing_keyboard",
    "op_back_to_operators_keyboard",
    "op_back_to_settings_keyboard",
    "op_back_to_audience_keyboard",
    "op_stats_menu_keyboard",
    "op_audience_menu_keyboard",
    "op_audience_user_actions",
    "op_audience_paginated_list_keyboard",
    "op_audience_user_card_keyboard",
    "op_audience_search_cancel_keyboard",
    "appeal_admin_actions",
    "op_help_keyboard",
]
