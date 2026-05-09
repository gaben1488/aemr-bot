from maxapi.types import (
    CallbackButton,
    LinkButton,
    RequestContactButton,
)
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder


def blocked_user_menu(electronic_reception_url: str | None = None):
    """Урезанное меню для заблокированного жителя. После /forget или
    ручной блокировки оператором у жителя is_blocked=true: подавать
    обращения и подписываться нельзя. Но «Полезная информация» —
    публичные контакты экстренных служб — остаётся доступна, потому
    что это статика, не привязанная к ПДн жителя.
    """
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="📚 Полезная информация", payload="menu:useful_info"))
    if electronic_reception_url:
        kb.row(LinkButton(text="🌐 Электронная приёмная", url=electronic_reception_url))
    return kb.as_markup()


def main_menu(
    electronic_reception_url: str | None = None,
    *,
    subscribed: bool = False,
):
    """Главное меню жителя.

    Кнопка подписки на рассылку вынесена сюда из подменю «Полезная
    информация»: задача «не пропустить ЧС или объявление округа» —
    одна из ключевых, и закапывать её в подменю значит терять
    подписчиков. Текст кнопки отражает текущее состояние подписки,
    чтобы один и тот же тап работал и как «подписаться», и как
    «отписаться»."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="📝 Написать обращение", payload="menu:new_appeal"))
    kb.row(CallbackButton(text="📂 Мои обращения", payload="menu:my_appeals"))
    # Идемпотентные payload'ы вместо toggle: чтобы кнопка из «старого»
    # сообщения, где состояние уже изменилось, не перевернула подписку
    # обратно. Если житель жмёт «Подписаться», бот не отписывает его в
    # ответ, а просто говорит «уже подписаны».
    if subscribed:
        kb.row(
            CallbackButton(
                text="🔕 Отписаться от рассылки", payload="info:subscribe_off"
            )
        )
    else:
        kb.row(
            CallbackButton(
                text="🔔 Подписаться на рассылку", payload="info:subscribe_on"
            )
        )
    if electronic_reception_url:
        kb.row(LinkButton(text="🌐 Электронная приёмная", url=electronic_reception_url))
    kb.row(CallbackButton(text="📋 Приём граждан", payload="menu:appointment"))
    kb.row(CallbackButton(text="📚 Полезная информация", payload="menu:useful_info"))
    kb.row(CallbackButton(text="⚙️ Настройки и помощь", payload="menu:settings"))
    return kb.as_markup()


def settings_menu_keyboard():
    """Подменю «Настройки и помощь». Кнопки покрывают /help, /policy,
    /forget плюс отдельную точку для согласия на ПДн — отзыв и просмотр.

    Блок «Согласие на ПДн» намеренно отдельной кнопкой, не внутри
    «Удалить мои данные». Пользователь может хотеть отозвать согласие,
    но сохранить историю обращений в архиве; «удалить» — это полное
    обезличивание плюс блокировка.
    """
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="🆘 Помощь и команды", payload="settings:help"))
    kb.row(CallbackButton(text="📄 Политика данных", payload="settings:policy"))
    kb.row(
        CallbackButton(text="🔐 Согласие на ПДн", payload="settings:consent_status"),
    )
    kb.row(
        CallbackButton(text="🗑️ Удалить мои данные", payload="settings:forget_ask")
    )
    kb.row(CallbackButton(text="↩️ В меню", payload="menu:main"))
    return kb.as_markup()


def consent_status_keyboard(*, consent_active: bool):
    """Кнопки на экране «Согласие на ПДн». Если согласие активно — даём
    «Отозвать» и «Открыть политику». Если отозвано или не давалось —
    «Дать согласие» (запускает воронку с шагом согласия)."""
    kb = InlineKeyboardBuilder()
    if consent_active:
        kb.row(
            CallbackButton(text="❌ Отозвать согласие", payload="settings:consent_revoke_ask"),
        )
    else:
        kb.row(
            CallbackButton(text="✅ Дать согласие", payload="settings:consent_give"),
        )
    kb.row(CallbackButton(text="📄 Политика данных", payload="settings:policy"))
    kb.row(CallbackButton(text="↩️ Назад", payload="menu:settings"))
    return kb.as_markup()


def consent_revoke_confirm_keyboard():
    """Подтверждение отзыва согласия. Кнопка «Не отзывать» возвращает
    в карточку согласия, а не в главное меню — чтобы человек видел
    свой текущий статус сразу после отказа."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="✅ Да, отозвать", payload="settings:consent_revoke_yes"),
        CallbackButton(text="❌ Не отзывать", payload="settings:consent_status"),
    )
    return kb.as_markup()


def forget_confirm_keyboard():
    """Подтверждение удаления ПДн жителя. Действие необратимо: записывает
    «Удалено» в first_name, обнуляет phone и поднимает is_blocked. Поэтому
    отдельный шаг подтверждения, чтобы случайный тап не стёр данные."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="✅ Да, удалить", payload="settings:forget_yes"),
        CallbackButton(text="❌ Не удалять", payload="menu:settings"),
    )
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


def topics_keyboard(topics: list[str]):
    """Тематики обращения. По одной кнопке в ряд: иначе MAX обрезает
    длинные названия в стиле «Управляющие компани…». У Солодова такой же
    одностолбчатый макет — текст всегда читается полностью."""
    kb = InlineKeyboardBuilder()
    for idx, topic in enumerate(topics):
        kb.row(CallbackButton(text=topic, payload=f"topic:{idx}"))
    kb.row(CallbackButton(text="Отмена", payload="cancel"))
    return kb.as_markup()


def reuse_address_keyboard():
    """Кнопки «использовать тот же адрес / указать новый» в первом шаге
    воронки, если у жителя уже есть прошлое обращение с заполненным
    населённым пунктом и адресом. Экономит два шага FSM."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="✅ Тот же адрес", payload="addr:reuse"))
    kb.row(CallbackButton(text="📍 Указать новый", payload="addr:new"))
    kb.row(CallbackButton(text="❌ Отмена", payload="cancel"))
    return kb.as_markup()


def localities_keyboard(localities: list[str]):
    """Населённые пункты Елизовского муниципального округа. По одной кнопке
    в ряд по той же причине, что и тематики: длинные названия вроде
    «Раздольненское сельское поселение» не помещаются в две колонки."""
    kb = InlineKeyboardBuilder()
    for idx, locality in enumerate(localities):
        kb.row(CallbackButton(text=locality, payload=f"locality:{idx}"))
    kb.row(CallbackButton(text="Отмена", payload="cancel"))
    return kb.as_markup()


def user_appeal_card_keyboard(appeal_id: int, status: str):
    """Кнопки под карточкой обращения у жителя.

    NEW/IN_PROGRESS/ANSWERED — «📎 Дополнить»: явный путь пришить
    дополнение к обращению (раньше работала «магия» — любое сообщение
    в IDLE автоматически пришивалось, без подтверждения; пенсионеры
    путались).

    CLOSED — «🔁 Подать похожее»: новая воронка с тем же адресом и
    тематикой, житель только пишет суть. Сценарий «опять не вывозят
    мусор по тому же адресу».
    """
    from aemr_bot.db.models import AppealStatus

    kb = InlineKeyboardBuilder()
    if status in {
        AppealStatus.NEW.value,
        AppealStatus.IN_PROGRESS.value,
        AppealStatus.ANSWERED.value,
    }:
        kb.row(
            CallbackButton(
                text="📎 Дополнить", payload=f"appeal:followup:{appeal_id}"
            )
        )
    elif status == AppealStatus.CLOSED.value:
        kb.row(
            CallbackButton(
                text="🔁 Подать похожее", payload=f"appeal:repeat:{appeal_id}"
            )
        )
    kb.row(CallbackButton(text="↩️ В меню", payload="menu:main"))
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


def cancel_reply_intent_keyboard():
    """Кнопка «❌ Отменить» под подсказкой ввода ответа. Без неё intent
    мог жить 5 минут, и любой следующий текст оператора уходил жителю —
    в т.ч. случайные «окей», текст для другого обращения, ввод wizard'а."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="❌ Отменить ответ", payload="op:reply_cancel"))
    return kb.as_markup()


def subscribe_mini_consent_keyboard():
    """Экран мини-согласия на рассылку. Два варианта: подтвердить и
    отменить. После подтверждения тап «✅ Подписаться» проставляет
    consent_broadcast_at и subscribed_broadcast=True (без воронки
    телефона/имени, потому что для рассылки это не нужно)."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="✅ Подписаться", payload="subscribe:confirm"))
    kb.row(CallbackButton(text="↩️ Отмена", payload="menu:main"))
    return kb.as_markup()


def useful_info_keyboard(
    udth_schedule_url: str | None = None,
    udth_schedule_intermunicipal_url: str | None = None,
    *,
    subscribed: bool = False,
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
    if subscribed:
        kb.row(
            CallbackButton(
                text="🔕 Отписаться от рассылки", payload="info:subscribe_off"
            )
        )
    else:
        kb.row(
            CallbackButton(
                text="🔔 Подписаться на рассылку", payload="info:subscribe_on"
            )
        )
    kb.row(CallbackButton(text="↩️ В меню", payload="menu:main"))
    return kb.as_markup()


def broadcast_unsubscribe_keyboard():
    """Inline-кнопка под каждым сообщением рассылки — отписка в одно нажатие."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(
            text="🔕 Отписаться от рассылки",
            payload="broadcast:unsubscribe",
        )
    )
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
    return kb.as_markup()


def op_stats_menu_keyboard():
    """Подменю «📊 Статистика» — выбор периода. По одной кнопке в ряд:
    длинные подписи («За полгода», «За всё время») в две колонки
    обрезаются на узких экранах MAX. После клика по периоду бот
    отправляет XLSX и возвращает оператору главную панель /op_help."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="📊 За сегодня", payload="op:stats_today"))
    kb.row(CallbackButton(text="📊 За неделю", payload="op:stats_week"))
    kb.row(CallbackButton(text="📊 За месяц", payload="op:stats_month"))
    kb.row(CallbackButton(text="📊 За квартал", payload="op:stats_quarter"))
    kb.row(CallbackButton(text="📊 За полгода", payload="op:stats_half_year"))
    kb.row(CallbackButton(text="📊 За год", payload="op:stats_year"))
    kb.row(CallbackButton(text="📊 За всё время", payload="op:stats_all"))
    kb.row(CallbackButton(text="↩️ Отмена", payload="op:menu"))
    return kb.as_markup()


def op_operators_menu_keyboard():
    """Меню «👥 Операторы» в админ-панели для роли it."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="➕ Добавить", payload="op:opadd:start"))
    kb.row(CallbackButton(text="📋 Список", payload="op:opadd:list"))
    kb.row(CallbackButton(text="❌ Отмена", payload="op:opadd:cancel"))
    return kb.as_markup()


def op_role_picker_keyboard():
    """Шаг 2 wizard'а добавления оператора — выбор роли. Четыре кнопки
    в одну строку: it, coordinator, aemr, egp. Самомодификация (попытка
    выдать it самому себе) ловится в обработчике."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="it", payload="op:opadd:role:it"),
        CallbackButton(text="coordinator", payload="op:opadd:role:coordinator"),
    )
    kb.row(
        CallbackButton(text="aemr", payload="op:opadd:role:aemr"),
        CallbackButton(text="egp", payload="op:opadd:role:egp"),
    )
    kb.row(CallbackButton(text="❌ Отмена", payload="op:opadd:cancel"))
    return kb.as_markup()


def op_settings_keys_keyboard(keys: list[str]):
    """Список ключей /setting — по одной кнопке на строку (длинные имена).
    Тап → текущее значение и шаблон команды редактирования."""
    kb = InlineKeyboardBuilder()
    for key in keys:
        kb.row(CallbackButton(text=key, payload=f"op:setkey:{key}"))
    return kb.as_markup()


def op_audience_menu_keyboard():
    """Меню «📊 Аудитория и согласия» в админ-панели для роли it.
    Три выборки: подписчики, давшие согласие, заблокированные.
    Каждая открывается отдельным сообщением со списком до 20 записей."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="📩 Подписчики", payload="op:aud:subs"))
    kb.row(CallbackButton(text="🔐 Дали согласие", payload="op:aud:consent"))
    kb.row(CallbackButton(text="🚫 Заблокированные", payload="op:aud:blocked"))
    return kb.as_markup()


def op_audience_user_actions(max_user_id: int, *, blocked: bool):
    """Кнопки действий рядом с конкретным жителем в выводе «Аудитория».
    Минимальный набор: разблок/блок и удаление ПДн. Подписку можно
    отозвать через `/setting` или попросить жителя отписаться."""
    kb = InlineKeyboardBuilder()
    if blocked:
        kb.row(
            CallbackButton(
                text="✅ Разблокировать", payload=f"op:aud:unblock:{max_user_id}"
            ),
        )
    else:
        kb.row(
            CallbackButton(
                text="🚫 Заблокировать", payload=f"op:aud:block:{max_user_id}"
            ),
        )
    kb.row(
        CallbackButton(
            text="🗑 Удалить ПДн", payload=f"op:aud:erase:{max_user_id}"
        ),
    )
    return kb.as_markup()


def appeal_admin_actions(
    appeal_id: int,
    status: str,
    *,
    is_it: bool = False,
    user_blocked: bool = False,
    closed_due_to_revoke: bool = False,
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
    """
    from aemr_bot.db.models import AppealStatus

    kb = InlineKeyboardBuilder()
    open_states = {AppealStatus.NEW.value, AppealStatus.IN_PROGRESS.value}
    closed_states = {AppealStatus.ANSWERED.value, AppealStatus.CLOSED.value}
    if status in open_states:
        kb.row(
            CallbackButton(text="✉️ Ответить", payload=f"op:reply:{appeal_id}"),
        )
        kb.row(
            CallbackButton(
                text="⛔ Закрыть без ответа", payload=f"op:close:{appeal_id}"
            ),
        )
    elif status in closed_states and not closed_due_to_revoke:
        kb.row(
            CallbackButton(
                text="🔁 Возобновить", payload=f"op:reopen:{appeal_id}"
            ),
        )
    if is_it:
        block_label = (
            "✅ Разблокировать" if user_blocked else "🚫 Заблокировать"
        )
        block_payload = (
            f"op:unblock:{appeal_id}" if user_blocked else f"op:block:{appeal_id}"
        )
        kb.row(
            CallbackButton(text=block_label, payload=block_payload),
            CallbackButton(text="🗑 Удалить ПДн", payload=f"op:erase:{appeal_id}"),
        )
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
    kb.row(CallbackButton(text=open_label, payload="op:open_tickets"))
    kb.row(CallbackButton(text="📊 Статистика", payload="op:stats_menu"))
    if can_broadcast:
        kb.row(CallbackButton(text="📢 Сделать рассылку", payload="op:broadcast"))
        kb.row(CallbackButton(text="📜 История рассылок", payload="op:broadcast_list"))
    kb.row(CallbackButton(text="🛠 Диагностика", payload="op:diag"))
    if is_it:
        kb.row(CallbackButton(text="💾 Снять бэкап", payload="op:backup"))
        kb.row(CallbackButton(text="👥 Операторы", payload="op:operators"))
        kb.row(CallbackButton(text="⚙️ Настройки бота", payload="op:settings"))
        kb.row(CallbackButton(text="📊 Аудитория и согласия", payload="op:audience"))
    return kb.as_markup()
