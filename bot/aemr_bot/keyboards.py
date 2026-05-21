from maxapi.types import (
    CallbackButton,
    LinkButton,
    RequestContactButton,
)
from maxapi.types.attachments.buttons.request_geo_location_button import (
    RequestGeoLocationButton,
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
    *,
    subscribed: bool = False,
):
    """Главное меню жителя.

    Структура (вариант 3, утверждено 2026-05-09):
    - Написать обращение, Мои обращения — горячие действия первыми тапами
    - Подписка/отписка — динамическая кнопка-toggle, текст зависит
      от текущего статуса подписки
    - Приём граждан, Полезная информация, Настройки — три подменю

    Кнопка подписки динамическая: «🔔 Подписаться на рассылку» если
    ещё не подписан, «🔕 Не хочу получать рассылку» если подписан.
    Электронная приёмная (LinkButton) переехала в подменю «Приём
    граждан», чтобы главное меню осталось на 6 кнопках без скролла."""
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
                text="🔕 Не хочу получать рассылку", payload="info:subscribe_off"
            )
        )
    else:
        kb.row(
            CallbackButton(
                text="🔔 Подписаться на рассылку", payload="info:subscribe_on"
            )
        )
    # Электронная приёмная переехала в подменю «Приём граждан» — там она
    # стоит рядом с расписанием очного приёма, и пенсионер видит обе
    # формы обращения в администрацию в одном экране.
    kb.row(CallbackButton(text="🏛 Приём граждан", payload="menu:appointment"))
    kb.row(CallbackButton(text="ℹ️ Полезная информация", payload="menu:useful_info"))
    kb.row(CallbackButton(text="⚙️ Настройки и помощь", payload="menu:settings"))
    return kb.as_markup()


def appointment_keyboard(electronic_reception_url: str | None = None):
    """Подменю «🏛 Приём граждан» — расписание очного приёма + ссылка
    на электронную приёмную (если задана).

    Расписание приходит как текст из `settings.appointment_text`
    (редактируется через /setting). Электронная приёмная — внешняя
    форма на сайте администрации, открывается LinkButton'ом.
    """
    kb = InlineKeyboardBuilder()
    if electronic_reception_url:
        kb.row(LinkButton(text="🌐 Электронная приёмная", url=electronic_reception_url))
    kb.row(CallbackButton(text="↩️ В меню", payload="menu:main"))
    return kb.as_markup()


def settings_menu_keyboard():
    """Подменю «Настройки и помощь». Основные точки входа:

    - «📋 Помощь и команды» — список команд жителя.
    - «📜 Правила пользования» — порядок работы бота и ограничения.
    - «📄 Политика данных» — открыть PDF/ссылку на политику.
    - «👋 Уйти из бота» — A4-сценарий с тремя опциями (отписка,
      прощальный отзыв согласия, полное удаление). Раньше было два
      отдельных пункта — «🔐 Согласие на ПДн» и «🗑 Удалить мои данные» —
      и пенсионер не понимал, чем они отличаются. Теперь одна точка
      входа, внутри — выбор сценария по жизненной ситуации.
    """
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="📋 Помощь и команды", payload="settings:help"))
    kb.row(CallbackButton(text="📜 Правила пользования", payload="settings:rules"))
    kb.row(CallbackButton(text="📄 Политика данных", payload="settings:policy"))
    kb.row(CallbackButton(text="👋 Уйти из бота", payload="settings:goodbye"))
    kb.row(CallbackButton(text="↩️ В меню", payload="menu:main"))
    return kb.as_markup()


def goodbye_keyboard():
    """Экран «👋 Уйти из бота» — три утверждённые жизненные опции.

    Формулировки взяты из утверждённой UX-сессии: каждая фраза описывает
    ситуацию жителя, не технический термин. «Отозвать согласие» / «удалить
    данные» — это IT-язык; «попрощаться, но дождаться ответа» — язык
    человека, который пришёл сюда не из любви к 152-ФЗ.

    Семантика опций:
    - 🔕 «Не хочу получать рассылку» — subscribed_broadcast=false,
      consent_pdn остаётся, обращения работают как и раньше.
    - 👋 «Хочу попрощаться, но дождаться ответа на обращение» — revoke_consent:
      consent_pdn=NULL, consent_revoked_at=now; рассылка off; новые
      обращения нельзя; на уже поданные ДО отзыва оператор отвечает
      «прощальным» ответом (см. _deliver_operator_reply); через 30 дней
      без активности retention-cron автоматически обезличит данные.
    - ❌ «Стереть данные обо мне прямо сейчас» — erase_pdn немедленно:
      имя/телефон стираются, открытые обращения закрываются (закрытые
      и анонимизированные через anonymous-user остаются для статистики),
      запись жителя физически удаляется. Бот «забудет» жителя.
    - ↩️ «Передумал, остаюсь» — назад в Настройки.
    """
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="🔕 Не хочу получать рассылку", payload="goodbye:unsub"))
    kb.row(CallbackButton(text="👋 Хочу попрощаться, но дождаться ответа на обращение", payload="goodbye:revoke_ask"))
    kb.row(CallbackButton(text="❌ Стереть данные обо мне прямо сейчас", payload="goodbye:erase_ask"))
    kb.row(CallbackButton(text="↩️ Передумал, остаюсь", payload="menu:settings"))
    return kb.as_markup()


def goodbye_revoke_confirm_keyboard():
    """Подтверждение «прощального» отзыва согласия. Возврат — в экран
    «Уйти из бота», чтобы человек, передумавший на этом шаге, видел все
    три опции, а не сразу «Назад» в Настройки."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="✅ Да, попрощаться", payload="goodbye:revoke_yes"),
        CallbackButton(text="❌ Не отзывать", payload="settings:goodbye"),
    )
    return kb.as_markup()


def goodbye_erase_confirm_keyboard():
    """Подтверждение полного стирания. Действие необратимо в смысле
    «бот не узнает вас при возврате», поэтому шаг подтверждения отдельный.
    Возврат на отказ — в «Уйти из бота», логика та же что у revoke."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="✅ Да, стереть", payload="goodbye:erase_yes"),
        CallbackButton(text="❌ Не стирать", payload="settings:goodbye"),
    )
    return kb.as_markup()


# Алиасы для callback'ов из handlers/menu.py — settings:forget_ask /
# settings:consent_status / settings:consent_revoke_ask. Семантически
# идентичны goodbye_erase / goodbye_revoke (тот же необратимый отзыв
# и стирание), но callback-payload'ы разные. Чтобы не плодить дубли
# логики — переиспользуем уже отлаженные клавиатуры.
def forget_confirm_keyboard():
    """settings:forget_ask — экран подтверждения /forget от жителя.
    Эквивалент goodbye_erase_confirm_keyboard."""
    return goodbye_erase_confirm_keyboard()


def consent_revoke_confirm_keyboard():
    """settings:consent_revoke_ask — экран подтверждения отзыва
    согласия из карточки «Согласие на ПДн». Эквивалент
    goodbye_revoke_confirm_keyboard."""
    return goodbye_revoke_confirm_keyboard()


def consent_status_keyboard(*, consent_active: bool):
    """Кнопки под карточкой статуса согласия (settings:consent_status).

    Если согласие активно — показываем кнопку «👋 Уйти из бота» —
    житель может отозвать или стереть данные через утверждённую
    воронку goodbye. Если согласия нет (отозвано или никогда не давалось)
    — только «↩️ В меню», потому что отзывать нечего, а дать согласие
    можно только через воронку «📝 Написать обращение».
    """
    kb = InlineKeyboardBuilder()
    if consent_active:
        kb.row(CallbackButton(text="👋 Уйти из бота", payload="settings:goodbye"))
    kb.row(CallbackButton(text="↩️ В меню", payload="menu:main"))
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
    kb.row(CallbackButton(text="❌ Отмена", payload="cancel"))
    return kb.as_markup()


def cancel_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="❌ Отмена", payload="cancel"))
    return kb.as_markup()


def topics_keyboard(topics: list[str]):
    """Темы обращения. По одной кнопке в ряд: иначе MAX обрезает
    длинные названия в стиле «Управляющие компани…». У Солодова такой же
    одностолбчатый макет — текст всегда читается полностью."""
    kb = InlineKeyboardBuilder()
    for idx, topic in enumerate(topics):
        kb.row(CallbackButton(text=topic, payload=f"topic:{idx}"))
    kb.row(CallbackButton(text="❌ Отмена", payload="cancel"))
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
    «Раздольненское сельское поселение» не помещаются в две колонки.

    Сверху — кнопка «📍 Поделиться геолокацией»: бот определит поселение
    и адрес автоматически по координатам через локальную базу OSM
    (см. `services/geo.py`). Без интернет-зависимости от внешних
    геокодеров. Если житель тапнет — попадёт в `AWAITING_GEO_CONFIRM`.
    """
    kb = InlineKeyboardBuilder()
    kb.row(RequestGeoLocationButton(text="📍 Поделиться геолокацией", quick=False))
    for idx, locality in enumerate(localities):
        kb.row(CallbackButton(text=locality, payload=f"locality:{idx}"))
    kb.row(CallbackButton(text="❌ Отмена", payload="cancel"))
    return kb.as_markup()


def geo_confirm_keyboard():
    """Подтверждение определённого по геолокации адреса.

    Три варианта:
    - ✅ всё правильно — продолжаем воронку с автоадресом
    - ✏️ исправить — пропускаем автоадрес, переходим к ручному вводу адреса
    - 🔙 другой населённый пункт — возврат к выбору поселения
    """
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="✅ Всё правильно", payload="geo:confirm"))
    kb.row(CallbackButton(text="✏️ Исправить адрес", payload="geo:edit_address"))
    kb.row(CallbackButton(text="🔙 Другой населённый пункт", payload="geo:other_locality"))
    kb.row(CallbackButton(text="❌ Отмена", payload="cancel"))
    return kb.as_markup()


def user_appeal_card_keyboard(
    appeal_id: int,
    status: str,
    *,
    attachment_count: int = 0,
):
    """Кнопки под карточкой обращения у жителя.

    NEW/IN_PROGRESS — «📎 Дополнить»: явный путь уточнить открытое
    обращение. Любое сообщение в IDLE не пришивается автоматически.

    ANSWERED/CLOSED — «🔁 Подать похожее»: новая воронка с тем же
    адресом и тематикой. Новое обращение помечается как связанное с
    отвеченным или закрытым вопросом.

    attachment_count>0 — кнопка «📎 Показать вложения (N)»: явный
    показ переотправки. Раньше происходила автоматически при каждом
    открытии карточки и создавала задержку в личке (PR-fix-hang).
    """
    from aemr_bot.db.models import AppealStatus

    kb = InlineKeyboardBuilder()
    if status in {
        AppealStatus.NEW.value,
        AppealStatus.IN_PROGRESS.value,
    }:
        kb.row(
            CallbackButton(
                text="📎 Дополнить", payload=f"appeal:followup:{appeal_id}"
            )
        )
    elif status in {AppealStatus.ANSWERED.value, AppealStatus.CLOSED.value}:
        kb.row(
            CallbackButton(
                text="🔁 Подать похожее", payload=f"appeal:repeat:{appeal_id}"
            )
        )
    if attachment_count > 0:
        kb.row(
            CallbackButton(
                text=f"📎 Показать вложения ({attachment_count})",
                payload=f"appeal:atts:{appeal_id}",
            )
        )
    kb.row(CallbackButton(text="↩️ К моим обращениям", payload="menu:my_appeals"))
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
    kb.row(CallbackButton(text="🏠 Главное меню", payload="menu:main"))
    return kb.as_markup()


def back_to_settings_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="↩️ К настройкам", payload="menu:settings"))
    kb.row(CallbackButton(text="↩️ В меню", payload="menu:main"))
    return kb.as_markup()


def back_to_useful_info_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="↩️ К полезной информации", payload="menu:useful_info"))
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
    kb.row(CallbackButton(text="❌ Отмена", payload="menu:main"))
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
    kb.row(CallbackButton(text="🏠 В админ-меню", payload="op:menu"))
    return kb.as_markup()


def op_back_to_menu_keyboard():
    """Одна кнопка возврата к главной операторской панели."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="↩️ Назад", payload="op:menu"))
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
    kb.row(CallbackButton(text="🏠 В админ-меню", payload="op:menu"))
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


def op_back_to_operators_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="↩️ К операторам", payload="op:operators"))
    kb.row(CallbackButton(text="🏠 В админ-меню", payload="op:menu"))
    return kb.as_markup()


def op_back_to_settings_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="↩️ К настройкам", payload="op:settings"))
    kb.row(CallbackButton(text="🏠 В админ-меню", payload="op:menu"))
    return kb.as_markup()


def op_back_to_audience_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="↩️ К аудитории", payload="op:audience"))
    kb.row(CallbackButton(text="🏠 В админ-меню", payload="op:menu"))
    return kb.as_markup()


def op_add_cancel_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="❌ Отменить добавление", payload="op:opadd:cancel"))
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
    kb.row(CallbackButton(text="↩️ Назад", payload="op:menu"))
    return kb.as_markup()


def op_operators_menu_keyboard():
    """Меню «👥 Операторы» в админ-панели для роли it."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="📋 Список операторов", payload="op:opadd:list"))
    kb.row(CallbackButton(text="➕ Добавить из участников группы", payload="op:opadd:from_group"))
    kb.row(CallbackButton(text="🔢 Добавить по ID вручную", payload="op:opadd:start"))
    kb.row(CallbackButton(text="↩️ Назад", payload="op:menu"))
    return kb.as_markup()


def op_operators_list_keyboard(rows: list[tuple[int, str, str, bool]]):
    """Список операторов как кнопки. rows: (max_user_id, full_name, role,
    is_active). Тап — открывает карточку конкретного оператора. После
    списка — кнопка «Назад в меню операторов».
    Длина подписи ограничена ~50 символами для узких экранов MAX."""
    kb = InlineKeyboardBuilder()
    for max_user_id, full_name, role, is_active in rows:
        marker = "👤" if is_active else "💤"
        suffix = f" · {role}" if is_active else f" · {role} · деактивирован"
        # 40 символов на ФИО — компромисс между «видно полностью» и
        # «помещается на узких экранах MAX»
        name_short = full_name if len(full_name) <= 40 else full_name[:37] + "…"
        kb.row(
            CallbackButton(
                text=f"{marker} {name_short}{suffix}",
                payload=f"op:opcard:{max_user_id}",
            )
        )
    kb.row(CallbackButton(text="↩️ Назад", payload="op:operators"))
    return kb.as_markup()


def op_operator_card_keyboard(
    max_user_id: int,
    *,
    is_active: bool,
    is_self: bool,
    can_deactivate: bool,
):
    """Карточка оператора — действия зависят от состояния:
    - active + не self + can_deactivate → «Сменить роль», «Деактивировать»
    - active + self → «Сменить роль» нельзя, «Деактивировать» нельзя
    - active + единственный IT → «Сменить роль» можно (на любую другую только если есть другие IT — проверка в обработчике), «Деактивировать» нельзя
    - inactive → «Реактивировать»
    """
    kb = InlineKeyboardBuilder()
    if is_active:
        if not is_self:
            kb.row(CallbackButton(text="✏️ Сменить роль", payload=f"op:oprole:{max_user_id}"))
        if can_deactivate and not is_self:
            kb.row(
                CallbackButton(
                    text="🚫 Деактивировать", payload=f"op:opdeact:{max_user_id}"
                )
            )
    else:
        kb.row(
            CallbackButton(
                text="🔄 Реактивировать", payload=f"op:opreact:{max_user_id}"
            )
        )
    kb.row(CallbackButton(text="↩️ К списку", payload="op:opadd:list"))
    kb.row(CallbackButton(text="🏠 В админ-меню", payload="op:menu"))
    return kb.as_markup()


def op_operator_role_change_keyboard(max_user_id: int, current_role: str):
    """Смена роли существующему оператору. Текущую роль показываем
    как заблокированную (без callback'а)."""
    from aemr_bot.db.models import OperatorRole

    kb = InlineKeyboardBuilder()
    roles = [
        (OperatorRole.IT.value, "🛠 it — ИТ, полный доступ"),
        (OperatorRole.COORDINATOR.value, "👤 coordinator — ответы + рассылки"),
        (OperatorRole.AEMR.value, "👤 aemr — рядовой специалист"),
        (OperatorRole.EGP.value, "👤 egp — специалист ЕГП"),
    ]
    for role_value, label in roles:
        if role_value == current_role:
            # Текущая роль — пометка, без активного callback'а
            kb.row(
                CallbackButton(
                    text=f"✓ {label} (текущая)",
                    payload=f"op:opcard:{max_user_id}",
                )
            )
        else:
            kb.row(
                CallbackButton(
                    text=label,
                    payload=f"op:opchrole:{max_user_id}:{role_value}",
                )
            )
    kb.row(CallbackButton(text="❌ Отмена", payload=f"op:opcard:{max_user_id}"))
    return kb.as_markup()


def op_operator_deactivate_confirm_keyboard(max_user_id: int):
    """Подтверждение деактивации — две кнопки в ряд."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="✅ Да, деактивировать", payload=f"op:opdeact_ok:{max_user_id}"),
        CallbackButton(text="❌ Отмена", payload=f"op:opcard:{max_user_id}"),
    )
    return kb.as_markup()


def op_from_group_keyboard(
    candidates: list[tuple[int, str, str | None]],  # (user_id, label, role_hint)
):
    """Кнопки добавления оператора из участников группы. label —
    готовая строка вида «Иванова А.П.» с пометкой [уже оператор: aemr]
    если есть. role_hint=None для добавления, role_hint=<role> для
    уже зарегистрированных (тап открывает их карточку)."""
    kb = InlineKeyboardBuilder()
    for user_id, label, role_hint in candidates:
        # 50 символов на label — место для имени + пометки
        text = label if len(label) <= 50 else label[:47] + "…"
        if role_hint is None:
            kb.row(CallbackButton(text=f"➕ {text}", payload=f"op:opadd:pick:{user_id}"))
        else:
            kb.row(CallbackButton(text=f"👤 {text}", payload=f"op:opcard:{user_id}"))
    kb.row(CallbackButton(text="🔢 Ввести ID вручную", payload="op:opadd:start"))
    kb.row(CallbackButton(text="❌ Отмена", payload="op:operators"))
    return kb.as_markup()


def op_role_picker_keyboard():
    """Шаг 2 wizard'а добавления оператора — выбор роли. По одной
    кнопке в строку с пояснением что значит каждая роль. Самомодификация
    (попытка выдать it самому себе) ловится в обработчике."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="🛠 it — ИТ, полный доступ", payload="op:opadd:role:it"))
    kb.row(CallbackButton(text="👤 coordinator — ответы + рассылки", payload="op:opadd:role:coordinator"))
    kb.row(CallbackButton(text="👤 aemr — рядовой специалист", payload="op:opadd:role:aemr"))
    kb.row(CallbackButton(text="👤 egp — специалист ЕГП", payload="op:opadd:role:egp"))
    kb.row(CallbackButton(text="❌ Отменить добавление", payload="op:opadd:cancel"))
    return kb.as_markup()


def op_add_name_choice_keyboard():
    """Шаг 4 wizard'а добавления — выбор: «сохранить имя из MAX» или
    «указать ФИО полностью текстом»."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="✅ Сохранить как есть", payload="op:opadd:name_keep"))
    kb.row(CallbackButton(text="✏️ Указать ФИО полностью", payload="op:opadd:name_edit"))
    kb.row(CallbackButton(text="❌ Отменить добавление", payload="op:opadd:cancel"))
    return kb.as_markup()


def op_add_confirm_keyboard():
    """Финальное подтверждение перед сохранением — три кнопки."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="✅ Сохранить", payload="op:opadd:confirm"))
    kb.row(CallbackButton(text="✏️ Изменить роль", payload="op:opadd:edit_role"))
    kb.row(CallbackButton(text="❌ Отменить добавление", payload="op:opadd:cancel"))
    return kb.as_markup()


def op_add_done_keyboard():
    """После успешного добавления — «Добавить ещё» / «К списку» / «В меню»."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="➕ Добавить ещё", payload="op:operators"))
    kb.row(CallbackButton(text="📋 К списку операторов", payload="op:opadd:list"))
    kb.row(CallbackButton(text="🏠 В админ-меню", payload="op:menu"))
    return kb.as_markup()


# ──────────────────────────────────────────────────────────────────────
# НАСТРОЙКИ БОТА — новое иерархическое меню
# ──────────────────────────────────────────────────────────────────────


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
    kb.row(CallbackButton(text="🏠 В админ-меню", payload="op:menu"))
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


def op_audience_menu_keyboard():
    """Меню «📊 Аудитория и согласия» в админ-панели для роли it.
    Три выборки: подписчики, давшие согласие, заблокированные.
    Каждая открывается отдельным сообщением со списком до 20 записей."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="📩 Подписчики", payload="op:aud:subs"))
    kb.row(CallbackButton(text="🔐 Дали согласие", payload="op:aud:consent"))
    kb.row(CallbackButton(text="🚫 Заблокированные", payload="op:aud:blocked"))
    kb.row(CallbackButton(text="↩️ Назад", payload="op:menu"))
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
    kb.row(CallbackButton(text="↩️ К аудитории", payload="op:audience"))
    kb.row(CallbackButton(text="🏠 В админ-меню", payload="op:menu"))
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
    if attachment_count > 0:
        kb.row(
            CallbackButton(
                text=f"📎 Вложения ({attachment_count})",
                payload=f"op:atts:{appeal_id}",
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
    kb.row(CallbackButton(text="🏠 В админ-меню", payload="op:menu"))
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
        kb.row(CallbackButton(text="📋 Шаблоны рассылок", payload="op:tmpl:list"))
    kb.row(CallbackButton(text="🛠 Диагностика", payload="op:diag"))
    if is_it:
        kb.row(CallbackButton(text="💾 Снять бэкап", payload="op:backup"))
        kb.row(CallbackButton(text="👥 Операторы", payload="op:operators"))
        kb.row(CallbackButton(text="⚙️ Настройки бота", payload="op:settings"))
        kb.row(CallbackButton(text="📊 Аудитория и согласия", payload="op:audience"))
    return kb.as_markup()
