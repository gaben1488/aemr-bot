"""Клавиатуры экранов жителя (citizen-facing).

Главное меню, подменю «Приём граждан», «Полезная информация»,
«Настройки и помощь», «Уйти из бота» (goodbye), согласие на ПДн,
воронка нового обращения (тематики, населённые пункты, геопозиция,
адрес), «Мои обращения» (список + карточка), плюс утилитарные
кнопки возврата.
"""
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
    # Кнопка «Защита от мошенников» — отдельным пунктом главного меню
    # вместо footer'а в каждом ответе оператора. Так житель находит
    # перечень «бот никогда не запрашивает» в один тап в любой момент
    # переписки, а официальный ответ оператора не перегружен меморандумом.
    kb.row(CallbackButton(text="🛡️ Защита от мошенников", payload="menu:security"))
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
    # A4.1 (2026-05-27): `🗑` вместо `❌` — UI_BRAND_CONCEPT B.7
    # требует разводить отмену (❌) и необратимое разрушительное
    # действие (🗑/🚫/⛔). Стирание данных — destructive,
    # эмодзи мусорной корзины ясно сигнализирует это пенсионеру.
    kb.row(CallbackButton(text="🗑 Стереть данные обо мне прямо сейчас", payload="goodbye:erase_ask"))
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
    # A4.1 (2026-05-27): `↩️` вместо `🔙` — UI_BRAND_CONCEPT B.7
    # canonical эмодзи возврата. `🔙` редко используется в боте и
    # ломает консистентность с pagination/useful_info/settings.
    kb.row(CallbackButton(text="↩️ Другой населённый пункт", payload="geo:other_locality"))
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

    attachment_count>0 — кнопка «🗂 Показать вложения (N)»: явный
    показ переотправки. Раньше происходила автоматически при каждом
    открытии карточки и создавала задержку в личке (PR-fix-hang).
    A4.2 (2026-05-27): иконка `🗂` вместо `📎`, чтобы не дублировать
    «📎 Дополнить» в той же клавиатуре — оператор/житель могли
    спутать действие.
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
                text=f"🗂 Показать вложения ({attachment_count})",
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
    # A4.1 (2026-05-27): унифицировано с pagination / useful_info /
    # settings — везде `↩️ В меню`. Раньше один-единственный
    # `🏠 Главное меню` ломал консистентность UI_BRAND_CONCEPT B.7
    # (canonical эмодзи возврата — `↩️`).
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="↩️ В меню", payload="menu:main"))
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
    # A4.1 (2026-05-27): подпись «🔕 Не хочу получать рассылку»
    # синхронизирована с main_menu. Раньше тут было «🔕 Отписаться
    # от рассылки» — двойной copy, пенсионер не сопоставлял с тем,
    # что видит на главном экране.
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
    kb.row(CallbackButton(text="↩️ В меню", payload="menu:main"))
    return kb.as_markup()


__all__ = [
    "blocked_user_menu",
    "main_menu",
    "appointment_keyboard",
    "settings_menu_keyboard",
    "goodbye_keyboard",
    "goodbye_revoke_confirm_keyboard",
    "goodbye_erase_confirm_keyboard",
    "forget_confirm_keyboard",
    "consent_revoke_confirm_keyboard",
    "consent_status_keyboard",
    "consent_keyboard",
    "contact_request_keyboard",
    "cancel_keyboard",
    "topics_keyboard",
    "reuse_address_keyboard",
    "localities_keyboard",
    "geo_confirm_keyboard",
    "user_appeal_card_keyboard",
    "my_appeals_list_keyboard",
    "back_to_menu_keyboard",
    "back_to_settings_keyboard",
    "back_to_useful_info_keyboard",
    "subscribe_mini_consent_keyboard",
    "useful_info_keyboard",
]
