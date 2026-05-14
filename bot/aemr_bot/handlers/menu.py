import logging
from collections.abc import Callable
from typing import Any, NamedTuple

from aemr_bot import keyboards, texts
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._common import current_user
from aemr_bot.services import admin_events
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import broadcasts as broadcasts_service
from aemr_bot.services import card_format
from aemr_bot.services import settings_store
from aemr_bot.services import users as users_service
from aemr_bot.utils.event import ack_callback, get_chat_id, get_user_id

log = logging.getLogger(__name__)


def _callback_mid(event) -> str | None:
    """mid сообщения, на котором нажали кнопку.

    MAX позволяет редактировать именно это сообщение. Для обычных команд
    callback отсутствует — тогда меню отправляется новым сообщением.
    """
    if getattr(event, "callback", None) is None:
        return None
    body = getattr(getattr(event, "message", None), "body", None)
    mid = getattr(body, "mid", None)
    return str(mid) if mid else None


async def _send_or_edit_menu(
    event,
    *,
    text: str,
    attachments: list | None = None,
    force_new_message: bool = False,
) -> None:
    """Показать экран меню.

    Если экран открыт нажатием кнопки, редактируем текущую карточку — как
    в воронке подачи обращения. Если это команда или редактирование не
    удалось, отправляем новое сообщение.
    """
    attachments = attachments or []
    mid = None if force_new_message else _callback_mid(event)
    if mid and hasattr(event.bot, "edit_message"):
        try:
            await event.bot.edit_message(
                message_id=mid,
                text=text,
                attachments=attachments,
            )
            return
        except Exception:
            log.info(
                "menu: edit_message %s failed, fallback to send",
                mid,
                exc_info=False,
            )

    chat_id = get_chat_id(event)
    user_id = None if chat_id is not None else get_user_id(event)
    await event.bot.send_message(
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        attachments=attachments,
    )


async def open_main_menu(event):
    """Главное меню жителя. Кнопка подписки рендерится по актуальному
    состоянию: если житель уже подписан — «Отписаться», иначе
    «Подписаться». Без этого кнопка из «↩️ В меню» всегда показывала
    «Подписаться», даже если житель только что подписался.

    Заблокированному жителю отдаём урезанное меню: только «Полезная
    информация» и приёмная. Остальные кнопки всё равно ведут к
    блокировочным сообщениям, проще их не показывать.
    """
    max_user_id = get_user_id(event)
    is_blocked = False
    subscribed = False
    recep_url = None
    # Сессию открываем только когда есть кого искать: анонимному
    # событию (max_user_id is None) меню рендерится по дефолтам.
    if max_user_id is not None:
        async with current_user(max_user_id) as (session, user):
            is_blocked = user.is_blocked
            if not is_blocked:
                subscribed = await broadcasts_service.is_subscribed(session, max_user_id)
            else:
                # Заблокированному оставляем электронную приёмную как
                # запасной канал — это сохранение прав, не привилегия.
                recep_url = await settings_store.get(session, "electronic_reception_url")

    if is_blocked:
        await _send_or_edit_menu(
            event,
            text=(
                "Ваш аккаунт заблокирован — подача обращений и подписка "
                "недоступны. Доступные разделы — ниже. Если блокировка "
                "ошибочна, обратитесь к координатору Администрации."
            ),
            attachments=[keyboards.blocked_user_menu(recep_url)],
        )
        return

    await _send_or_edit_menu(
        event,
        text=texts.WELCOME,
        attachments=[keyboards.main_menu(subscribed=subscribed)],
    )


MY_APPEALS_PAGE_SIZE = 5


async def open_my_appeals(event, max_user_id: int, page: int = 1):
    page = max(1, page)
    async with current_user(max_user_id) as (session, user):
        total = await appeals_service.count_for_user(session, user.id)
        if total == 0:
            await _send_or_edit_menu(
                event,
                text=texts.APPEAL_LIST_EMPTY,
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        total_pages = max(1, (total + MY_APPEALS_PAGE_SIZE - 1) // MY_APPEALS_PAGE_SIZE)
        page = min(page, total_pages)
        offset = (page - 1) * MY_APPEALS_PAGE_SIZE
        appeals = await appeals_service.list_for_user(
            session, user.id, limit=MY_APPEALS_PAGE_SIZE, offset=offset
        )
    items = [(a.id, card_format.appeal_list_label(a)) for a in appeals]
    header = (
        f"Ваши обращения (стр. {page}/{total_pages}, всего {total}):"
        if total_pages > 1
        else f"Ваши обращения (всего {total}):"
    )
    await _send_or_edit_menu(
        event,
        text=header,
        attachments=[
            keyboards.my_appeals_list_keyboard(items, page=page, total_pages=total_pages)
        ],
    )


async def start_appeal_followup(event, appeal_id: int, max_user_id: int):
    """Кнопка «📎 Дополнить» под карточкой обращения у жителя.

    Ставит state в AWAITING_FOLLOWUP_TEXT, сохраняет appeal_id в
    dialog_data. Следующее сообщение жителя (текст и/или вложения)
    пришивается только к открытому обращению. По отвеченному или
    закрытому вопросу создаём новое связанное обращение через
    «🔁 Подать похожее».
    """
    from aemr_bot.db.models import AppealStatus, DialogState

    async with session_scope() as session:
        appeal = await appeals_service.get_by_id(session, appeal_id)
        if not appeal or not appeal.user or appeal.user.max_user_id != max_user_id:
            await _send_or_edit_menu(
                event,
                text="Обращение не найдено.",
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        # Дополнять можно только неотвеченные обращения. ANSWERED/CLOSED
        # считаются завершёнными: повтор по ним — новое связанное
        # обращение, чтобы история не «оживала» задним числом.
        if appeal.status in {AppealStatus.ANSWERED.value, AppealStatus.CLOSED.value}:
            await _send_or_edit_menu(
                event,
                text=(
                    "Это обращение уже завершено. Если вопрос повторился "
                    "или ответ нужно обсудить отдельно, откройте карточку "
                    "и нажмите «🔁 Подать похожее» — бот создаст новое "
                    "связанное обращение."
                ),
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        await users_service.set_state(
            session,
            max_user_id,
            DialogState.AWAITING_FOLLOWUP_TEXT,
            data={"appeal_id": appeal_id},
        )
    await _send_or_edit_menu(
        event,
        text=(
            f"Опишите дополнение к обращению #{appeal_id} одним сообщением. "
            f"Можно приложить фото, видео или файл."
        ),
        attachments=[keyboards.cancel_keyboard()],
    )


async def start_appeal_repeat(event, appeal_id: int, max_user_id: int):
    """Кнопка «🔁 Подать похожее» под карточкой завершённого обращения.

    Запускает воронку нового обращения с уже заполненными locality,
    address и topic из старого. Жителю остаётся только написать суть
    проблемы. Если старое обращение было ANSWERED/CLOSED, новое
    обращение получит пометку связи с отвеченным или закрытым вопросом.
    """
    from aemr_bot.db.models import AppealStatus, DialogState

    async with session_scope() as session:
        appeal = await appeals_service.get_by_id(session, appeal_id)
        if not appeal or not appeal.user or appeal.user.max_user_id != max_user_id:
            await _send_or_edit_menu(
                event,
                text="Обращение не найдено.",
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        if not (appeal.locality and appeal.address):
            # Старое обращение без адреса (теоретически может случиться,
            # если оно было создано до миграции с шагом локалити). Тогда
            # просто запускаем обычную воронку.
            from aemr_bot.handlers.appeal_funnel import start_appeal_flow as _start_appeal_flow

            await _start_appeal_flow(event, max_user_id)
            return
        data: dict[str, Any] = {
            "locality": appeal.locality,
            "address": appeal.address,
            "topic": appeal.topic,
        }
        if appeal.status in {AppealStatus.ANSWERED.value, AppealStatus.CLOSED.value}:
            data.update(
                {
                    "repeat_source_appeal_id": appeal.id,
                    "repeat_source_status": appeal.status,
                    "repeat_source_topic": appeal.topic,
                }
            )
        await users_service.set_state(
            session,
            max_user_id,
            DialogState.AWAITING_SUMMARY,
            data=data,
        )
    if appeal.status == AppealStatus.ANSWERED.value:
        context = "по уже отвеченному вопросу"
    elif appeal.status == AppealStatus.CLOSED.value:
        context = "по закрытому вопросу"
    else:
        context = "с теми же данными"
    await _send_or_edit_menu(
        event,
        text=(
            f"Подаём новое обращение {context}:\n"
            f"📍 {appeal.locality}, {appeal.address}\n"
            f"🏷 {appeal.topic or '—'}\n\n"
            f"Опишите суть одним сообщением. Можно приложить фото, видео "
            f"или файл."
        ),
        attachments=[keyboards.cancel_keyboard()],
    )


async def show_appeal(event, appeal_id: int, max_user_id: int):
    """Карточка обращения у жителя.

    Кнопки зависят от статуса:
    - NEW / IN_PROGRESS — «📎 Дополнить» для уточнения открытого обращения.
    - ANSWERED / CLOSED — «🔁 Подать похожее» для нового связанного обращения.
    - Везде «↩ В меню».
    """
    async with session_scope() as session:
        appeal = await appeals_service.get_by_id(session, appeal_id)
        if not appeal or not appeal.user or appeal.user.max_user_id != max_user_id:
            await _send_or_edit_menu(
                event,
                text="Обращение не найдено.",
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        text = card_format.user_card(appeal)
        status = appeal.status
    await _send_or_edit_menu(
        event,
        text=text,
        attachments=[keyboards.user_appeal_card_keyboard(appeal_id, status)],
    )


async def open_useful_info(event):
    max_user_id = get_user_id(event)
    async with session_scope() as session:
        udth = await settings_store.get(session, "udth_schedule_url")
        udth_inter = await settings_store.get(session, "udth_schedule_intermunicipal_url")
        subscribed = (
            await broadcasts_service.is_subscribed(session, max_user_id)
            if max_user_id is not None
            else False
        )
    await _send_or_edit_menu(
        event,
        text=texts.USEFUL_INFO_TITLE,
        attachments=[
            keyboards.useful_info_keyboard(udth, udth_inter, subscribed=subscribed)
        ],
    )


async def do_subscribe(event, max_user_id: int) -> None:
    """Идемпотентная подписка через кнопку «🔔 Подписаться».

    Если житель уже подписан — отвечаем «уже подписаны», не меняя
    состояние. Это закрывает баг старого toggle-варианта, где кнопка
    из устаревшего меню могла отписать жителя в ответ на тап
    «Подписаться».

    Если согласие отозвано или не давалось — даём кнопку «Дать согласие»
    рядом с сообщением, чтобы не отправлять жителя кружным путём через
    «Настройки → Согласие на ПДн → Дать согласие».
    """
    async with current_user(max_user_id) as (session, user):
        if user.is_blocked:
            await _send_or_edit_menu(
                event,
                text=(
                    "Подписка недоступна: ваш аккаунт заблокирован. "
                    "Если это ошибка — обратитесь к оператору."
                ),
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        # Если мини-согласия на рассылку ещё нет — показываем короткий
        # экран. Раньше тут запрашивалось полное согласие на ПДн через
        # воронку обращения; это было избыточно для цели «отправить
        # рассылку», нарушение ст. 5 ч. 5 (минимизация).
        if not user.consent_broadcast_at:
            await _send_or_edit_menu(
                event,
                text=texts.SUBSCRIBE_MINI_CONSENT,
                attachments=[keyboards.subscribe_mini_consent_keyboard()],
            )
            return
        already = await broadcasts_service.is_subscribed(session, max_user_id)
        if already:
            await _send_or_edit_menu(
                event,
                text=texts.SUBSCRIBE_ALREADY_ON,
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        await broadcasts_service.set_subscription(session, max_user_id, True)
    await admin_events.notify_broadcast_subscribed(event.bot, max_user_id=max_user_id)
    await _send_or_edit_menu(
        event,
        text=texts.SUBSCRIBE_CONFIRMED,
        attachments=[keyboards.back_to_menu_keyboard()],
    )


async def do_subscribe_confirm(event, max_user_id: int) -> None:
    """Тап «✅ Подписаться» на экране мини-согласия. Проставляет
    consent_broadcast_at и subscribed_broadcast=True."""
    from datetime import datetime, timezone

    from aemr_bot.services import operators as ops_service
    from sqlalchemy import update as sql_update

    from aemr_bot.db.models import User

    async with current_user(max_user_id) as (session, user):
        if user.is_blocked:
            await _send_or_edit_menu(
                event,
                text=(
                    "Подписка недоступна: ваш аккаунт заблокирован. "
                    "Если это ошибка — обратитесь к оператору."
                ),
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        await session.execute(
            sql_update(User)
            .where(User.max_user_id == max_user_id)
            .values(
                consent_broadcast_at=datetime.now(timezone.utc),
                subscribed_broadcast=True,
            )
        )
        await ops_service.write_audit(
            session,
            operator_max_user_id=max_user_id,
            action="self_subscribe_broadcast",
            target=f"user max_id={max_user_id}",
        )
    await admin_events.notify_broadcast_subscribed(event.bot, max_user_id=max_user_id)
    await _send_or_edit_menu(
        event,
        text=texts.SUBSCRIBE_CONFIRMED,
        attachments=[keyboards.back_to_menu_keyboard()],
    )


async def do_unsubscribe(event, max_user_id: int) -> None:
    """Идемпотентная отписка через кнопку «🔕 Отписаться»."""
    async with current_user(max_user_id) as (session, user):
        # Заблокированному отписка тоже не нужна — он уже не получает
        # рассылку. Но на всякий случай отметим subscribed=false.
        if user.is_blocked:
            await broadcasts_service.set_subscription(session, max_user_id, False)
            await _send_or_edit_menu(
                event,
                text=texts.UNSUBSCRIBE_CONFIRMED,
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        already = await broadcasts_service.is_subscribed(session, max_user_id)
        if not already:
            await _send_or_edit_menu(
                event,
                text=texts.UNSUBSCRIBE_ALREADY_OFF,
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        await broadcasts_service.set_subscription(session, max_user_id, False)
    await admin_events.notify_broadcast_unsubscribed(
        event.bot,
        max_user_id=max_user_id,
        source="меню",
    )
    await _send_or_edit_menu(
        event,
        text=texts.UNSUBSCRIBE_CONFIRMED,
        attachments=[keyboards.back_to_menu_keyboard()],
    )


async def handle_broadcast_unsubscribe(event, max_user_id: int) -> None:
    """Отписка в одно нажатие через кнопку под каждым сообщением рассылки.

    Если житель уже не подписан (например, отписался ранее по другой
    кнопке, а потом тапнул здесь же на старом сообщении рассылки) —
    отвечаем «вы и так не подписаны», не делая лишний UPDATE.
    """
    async with session_scope() as session:
        already = await broadcasts_service.is_subscribed(session, max_user_id)
        if not already:
            await ack_callback(event, texts.UNSUBSCRIBE_ALREADY_OFF)
            await _send_or_edit_menu(
                event,
                text=texts.UNSUBSCRIBE_ALREADY_OFF,
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        await broadcasts_service.set_subscription(session, max_user_id, False)
    await admin_events.notify_broadcast_unsubscribed(
        event.bot,
        max_user_id=max_user_id,
        source="кнопка под рассылкой",
    )
    await ack_callback(event, texts.UNSUBSCRIBE_CONFIRMED)
    await _send_or_edit_menu(
        event,
        text=texts.UNSUBSCRIBE_CONFIRMED,
        attachments=[keyboards.back_to_menu_keyboard()],
    )


async def open_settings(event):
    """Подменю «Настройки и помощь» с кнопками-дубликатами для команд /help,
    /policy, /forget. Цель — чтобы житель не запоминал команды."""
    await _send_or_edit_menu(
        event,
        text=texts.SETTINGS_MENU_TITLE,
        attachments=[keyboards.settings_menu_keyboard()],
    )


async def open_help(event):
    await _send_or_edit_menu(
        event,
        text=texts.HELP_USER,
        attachments=[keyboards.back_to_settings_keyboard()],
    )


async def open_rules(event):
    await _send_or_edit_menu(
        event,
        text=texts.RULES_TEXT,
        attachments=[keyboards.back_to_settings_keyboard()],
    )


async def open_goodbye(event):
    """Экран A4 «👋 Уйти из бота» — три утверждённые опции в одном шаге.

    Заменяет два прежних entry point'а из Настроек («🔐 Согласие на ПДн»
    и «🗑 Удалить мои данные»). Объяснительный текст в `GOODBYE_PROMPT`
    переводит юридические термины в жизненные ситуации, чтобы пенсионер
    понимал «что мне выбрать», а не «что значит отозвать согласие».
    """
    await _send_or_edit_menu(
        event,
        text=texts.GOODBYE_PROMPT,
        attachments=[keyboards.goodbye_keyboard()],
    )


async def ask_goodbye_revoke_confirm(event):
    """Подтверждение «прощального» отзыва согласия из A4-экрана.

    Текст подтверждения тот же, что в `ask_consent_revoke_confirm`
    (CONSENT_REVOKE_CONFIRM) — он описывает финальный ответ по уже
    открытым обращениям и автоудаление через 30 дней без активности.
    Меняется только клавиатура — возврат на отказ ведёт обратно в
    A4-экран, а не в карточку «Согласие на ПДн» (которой больше нет).
    """
    await _send_or_edit_menu(
        event,
        text=texts.CONSENT_REVOKE_CONFIRM,
        attachments=[keyboards.goodbye_revoke_confirm_keyboard()],
    )


async def ask_goodbye_erase_confirm(event):
    """Подтверждение полного стирания из A4-экрана.

    Текст и список жизненных последствий — тот же `ERASE_CONFIRM`, что
    в старом entry point'е через «🗑 Удалить мои данные». Возврат на
    отказ — в A4-экран, а не в Настройки: жильцу логичнее ещё раз
    рассмотреть две оставшиеся опции, чем уйти на уровень выше.
    """
    from aemr_bot.services import appeals as appeals_service

    max_user_id = get_user_id(event)
    open_lines: list[str] = []
    if max_user_id is not None:
        async with current_user(max_user_id) as (session, user):
            active = await appeals_service.list_unanswered(session)
            mine = [a for a in active if a.user_id == user.id]
            for ap in mine:
                open_lines.append(
                    f"#{ap.id} от {ap.created_at.strftime('%d.%m.%Y') if ap.created_at else '—'} · "
                    f"{ap.topic or 'без темы'}"
                )
    text = texts.ERASE_CONFIRM
    if open_lines:
        text += "\n\nСейчас у вас в работе:\n• " + "\n• ".join(open_lines)
        text += "\n\nПри стирании эти обращения закроются без ответа."
    await _send_or_edit_menu(
        event,
        text=text,
        attachments=[keyboards.goodbye_erase_confirm_keyboard()],
    )


async def ask_forget_confirm(event):
    """Подтверждение удаления данных. Если у жителя есть открытые
    обращения, перечисляем их в подтверждении — без явного списка
    житель не понимает, что именно потеряет (вопрос «обращения будут
    закрыты» он легко прочитает как «решены», а не «выкинуты»).
    """
    from aemr_bot.services import appeals as appeals_service

    max_user_id = get_user_id(event)
    open_lines: list[str] = []
    if max_user_id is not None:
        async with current_user(max_user_id) as (session, user):
            active = await appeals_service.list_unanswered(session)
            mine = [a for a in active if a.user_id == user.id]
        for ap in mine[:5]:
            topic = ap.topic or "—"
            from datetime import datetime
            from zoneinfo import ZoneInfo

            from aemr_bot.config import settings as cfg

            created = ap.created_at.astimezone(ZoneInfo(cfg.timezone)) if ap.created_at else None
            created_str = created.strftime("%d.%m.%Y") if isinstance(created, datetime) else "—"
            open_lines.append(f"• #{ap.id} от {created_str} · {topic}")
        if len(mine) > 5:
            open_lines.append(f"… и ещё {len(mine) - 5}.")

    text = texts.ERASE_CONFIRM
    if open_lines:
        text = (
            f"{text}\n\n"
            f"Сейчас у вас в работе {len(open_lines)} обращ.:\n"
            + "\n".join(open_lines)
            + "\n\nПри удалении они будут закрыты без ответа."
        )

    await _send_or_edit_menu(
        event,
        text=text,
        attachments=[keyboards.forget_confirm_keyboard()],
    )


def _format_dt_local(dt) -> str:
    from zoneinfo import ZoneInfo

    from aemr_bot.config import settings as cfg

    if dt is None:
        return "—"
    return dt.astimezone(ZoneInfo(cfg.timezone)).strftime("%d.%m.%Y %H:%M")


async def show_consent_status(event, max_user_id: int):
    """Карточка состояния согласия. Показывает один из трёх вариантов
    (активно / отозвано / никогда не давалось) и кнопки действий.
    """
    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
    consent_active = user.consent_pdn_at is not None
    if consent_active:
        text = texts.CONSENT_STATUS_ACTIVE.format(
            given_at=_format_dt_local(user.consent_pdn_at)
        )
    elif user.consent_revoked_at is not None:
        text = texts.CONSENT_STATUS_REVOKED.format(
            revoked_at=_format_dt_local(user.consent_revoked_at)
        )
    else:
        text = texts.CONSENT_STATUS_NEVER
    await _send_or_edit_menu(
        event,
        text=text,
        attachments=[keyboards.consent_status_keyboard(consent_active=consent_active)],
    )


async def ask_consent_revoke_confirm(event):
    await _send_or_edit_menu(
        event,
        text=texts.CONSENT_REVOKE_CONFIRM,
        attachments=[keyboards.consent_revoke_confirm_keyboard()],
    )


async def do_consent_revoke(event, max_user_id: int):
    """Отзыв согласия через кнопку.

    Подписка отключается, новые обращения требуют нового согласия.
    Открытые на момент отзыва обращения остаются в работе: оператор
    отправляет по ним финальный ответ через бот, после чего стандартный
    механизм ответа закрывает обращение.

    В служебную группу отправляем уведомление и повторяем карточки
    открытых обращений, чтобы сотрудник не искал их в истории чата.
    """
    from aemr_bot.services import appeals as appeals_service
    from aemr_bot.services import operators as ops_service

    async with current_user(max_user_id) as (session, user):
        active = await appeals_service.list_unanswered(session)
        my_open = [a for a in active if a.user_id == user.id]
        await users_service.revoke_consent(session, max_user_id)
        await ops_service.write_audit(
            session,
            operator_max_user_id=max_user_id,
            action="self_consent_revoke",
            target=f"user max_id={max_user_id}",
        )
    await _send_or_edit_menu(
        event,
        text=texts.CONSENT_REVOKED_OK,
        attachments=[keyboards.back_to_menu_keyboard()],
    )
    await admin_events.notify_consent_revoked(
        event.bot,
        max_user_id=max_user_id,
        open_appeal_ids=[a.id for a in my_open],
    )
    if my_open:
        from aemr_bot.handlers.appeal_runtime import send_to_admin_card

        for appeal in my_open:
            await send_to_admin_card(
                event.bot,
                card_format.admin_card(appeal, user),
                appeal_id=appeal.id,
                status=appeal.status,
                user_blocked=user.is_blocked,
            )


async def do_forget(event, max_user_id: int):
    """Кнопочный аналог /forget. Логика та же, что в start.cmd_forget,
    но без необходимости набирать команду.

    Перед обнулением считаем открытые обращения, чтобы потом сказать
    админ-группе, какие карточки можно убирать из работы.
    """
    from aemr_bot.services import appeals as appeals_service
    from aemr_bot.services import operators as ops_service

    closed_ids: list[int] = []
    async with current_user(max_user_id) as (session, user):
        active = await appeals_service.list_unanswered(session)
        closed_ids = [a.id for a in active if a.user_id == user.id]
        await users_service.erase_pdn(session, max_user_id)
        await ops_service.write_audit(
            session,
            operator_max_user_id=max_user_id,
            action="self_erase",
            target=f"user max_id={max_user_id}",
        )
    await _send_or_edit_menu(
        event,
        text=texts.ERASE_REQUESTED,
        attachments=[keyboards.back_to_menu_keyboard()],
    )
    await admin_events.notify_data_erased(
        event.bot,
        max_user_id=max_user_id,
        closed_appeal_ids=closed_ids,
    )


async def open_appointment(event):
    """Подменю «🏛 Приём граждан» — расписание + электронная приёмная.

    Электронная приёмная (LinkButton на сайт администрации) переехала
    сюда из главного меню. Логика: житель видит обе формы обращения
    в одном месте — записаться на очный приём или сразу отправить
    запрос через электронную форму.
    """
    async with session_scope() as session:
        text = await settings_store.get(session, "appointment_text")
        recep_url = await settings_store.get(session, "electronic_reception_url")
    await _send_or_edit_menu(
        event,
        text=text or "Информация скоро появится.",
        attachments=[keyboards.appointment_keyboard(recep_url)],
    )


async def open_emergency(event):
    async with session_scope() as session:
        contacts = await settings_store.get(session, "emergency_contacts") or []
    if not contacts:
        body = "Список контактов скоро появится."
    else:
        # Группируем по полю 'section', если оно есть; элементы без секции
        # попадают в «Прочее», чтобы старые seed-данные без секций
        # продолжали отображаться.
        grouped: dict[str, list[dict]] = {}
        order: list[str] = []
        for item in contacts:
            section = item.get("section") or "Прочее"
            if section not in grouped:
                grouped[section] = []
                order.append(section)
            grouped[section].append(item)
        blocks: list[str] = ["☎️ Телефоны экстренных и аварийных служб"]
        for section in order:
            blocks.append(f"\n{section}:")
            for item in grouped[section]:
                name = item.get("name", "—")
                phone = item.get("phone", "—")
                blocks.append(f"• {name} — {phone}")
        body = "\n".join(blocks)
    await _send_or_edit_menu(
        event,
        text=body,
        attachments=[keyboards.back_to_useful_info_keyboard()],
    )


async def open_dispatchers(event):
    async with session_scope() as session:
        items = await settings_store.get(session, "transport_dispatcher_contacts") or []
    if not items:
        body = "Список диспетчерских скоро появится."
    else:
        body = "📞 Диспетчерские автотранспорта:\n\n" + "\n\n".join(
            f"• {item.get('routes', '—')}\n  {item.get('phone', '—')}"
            for item in items
        )
    await _send_or_edit_menu(
        event,
        text=body,
        attachments=[keyboards.back_to_useful_info_keyboard()],
    )


# ============================================================================
# handle_callback — dispatch-таблица callback'ов меню жителя
# ============================================================================
# Раньше — ~196-строчная if-elif лестница `if payload == "...": ack;
# handler; return True`. Теперь payload'ы описаны декларативно в _EXACT
# (точное совпадение) и _PREFIX_APPEAL_ID (префикс + числовой id
# обращения), а handle_callback — тонкий диспетчер.
#
# Lambda-обёртки вокруг handler'ов обязательны: они резолвят имя функции
# в момент вызова, а не на этапе построения таблицы. Прямые ссылки
# заморозили бы функцию на импорте и сломали бы patch в тестах и
# возможный hot-reload (тот же урок, что в admin_callback_dispatch).


async def _lazy_cmd_policy(event) -> None:
    """settings:policy → start.cmd_policy. Импорт ленивый: start.py
    лениво импортирует menu в обратную сторону, прямой импорт на уровне
    модуля замкнул бы цикл."""
    from aemr_bot.handlers.start import cmd_policy

    await cmd_policy(event)


async def _lazy_start_appeal_flow(event, max_user_id: int) -> None:
    """settings:consent_give → appeal_funnel.start_appeal_flow. Воронка
    сама на первом шаге попросит согласие (consent_pdn_at пуст после
    отзыва). Импорт ленивый — appeal_funnel тянет тяжёлую цепочку."""
    from aemr_bot.handlers.appeal_funnel import start_appeal_flow

    await start_appeal_flow(event, max_user_id)


class _MenuRoute(NamedTuple):
    """Описание exact-маршрута меню жителя.

    handler — всегда вызывается как ``handler(event, max_user_id)``;
      для no-user маршрутов lambda просто игнорирует второй аргумент.
    requires_user — маршрут осмыслен только при идентифицированном
      жителе.
    ack — вызвать ack_callback перед handler'ом (broadcast:unsubscribe
      акает сам внутри handler'а — ему ack=False).
    consume_on_no_user — поведение при requires_user и max_user_id None:
      True → return True (тап «съеден»), False → return False
      (управление проваливается дальше). Сохраняет историческое
      расхождение: menu:my_appeals «съедал» тап, остальные user-маршруты
      проваливались.
    """

    handler: Callable
    requires_user: bool = False
    ack: bool = True
    consume_on_no_user: bool = False


_EXACT: dict[str, _MenuRoute] = {
    "menu:main": _MenuRoute(lambda e, u: open_main_menu(e)),
    "menu:my_appeals": _MenuRoute(
        lambda e, u: open_my_appeals(e, u),
        requires_user=True,
        consume_on_no_user=True,
    ),
    "menu:useful_info": _MenuRoute(lambda e, u: open_useful_info(e)),
    "menu:appointment": _MenuRoute(lambda e, u: open_appointment(e)),
    "menu:settings": _MenuRoute(lambda e, u: open_settings(e)),
    "settings:help": _MenuRoute(lambda e, u: open_help(e)),
    "settings:rules": _MenuRoute(lambda e, u: open_rules(e)),
    "settings:policy": _MenuRoute(lambda e, u: _lazy_cmd_policy(e)),
    "settings:forget_ask": _MenuRoute(lambda e, u: ask_forget_confirm(e)),
    "settings:forget_yes": _MenuRoute(
        lambda e, u: do_forget(e, u), requires_user=True
    ),
    "settings:consent_status": _MenuRoute(
        lambda e, u: show_consent_status(e, u), requires_user=True
    ),
    "settings:consent_revoke_ask": _MenuRoute(
        lambda e, u: ask_consent_revoke_confirm(e)
    ),
    "settings:consent_revoke_yes": _MenuRoute(
        lambda e, u: do_consent_revoke(e, u), requires_user=True
    ),
    "settings:consent_give": _MenuRoute(
        lambda e, u: _lazy_start_appeal_flow(e, u), requires_user=True
    ),
    # A4 «👋 Уйти из бота» — три жизненных опции в одном экране. Старые
    # цепочки settings:consent_revoke_ask / settings:forget_ask остались
    # для совместимости с уже отправленными сообщениями; новые точки
    # входа — через goodbye:*.
    "settings:goodbye": _MenuRoute(lambda e, u: open_goodbye(e)),
    "goodbye:unsub": _MenuRoute(
        lambda e, u: do_unsubscribe(e, u), requires_user=True
    ),
    "goodbye:revoke_ask": _MenuRoute(
        lambda e, u: ask_goodbye_revoke_confirm(e)
    ),
    "goodbye:revoke_yes": _MenuRoute(
        lambda e, u: do_consent_revoke(e, u), requires_user=True
    ),
    "goodbye:erase_ask": _MenuRoute(
        lambda e, u: ask_goodbye_erase_confirm(e)
    ),
    "goodbye:erase_yes": _MenuRoute(
        lambda e, u: do_forget(e, u), requires_user=True
    ),
    "info:emergency": _MenuRoute(lambda e, u: open_emergency(e)),
    "info:dispatchers": _MenuRoute(lambda e, u: open_dispatchers(e)),
    "info:subscribe_on": _MenuRoute(
        lambda e, u: do_subscribe(e, u), requires_user=True
    ),
    "subscribe:confirm": _MenuRoute(
        lambda e, u: do_subscribe_confirm(e, u), requires_user=True
    ),
    "info:subscribe_off": _MenuRoute(
        lambda e, u: do_unsubscribe(e, u), requires_user=True
    ),
    # Совместимость со старыми меню: info:subscribe_toggle уйдёт сам при
    # обновлении меню, но тап по старому сообщению маршрутизируем в
    # идемпотентный subscribe_on — ни одно состояние не перевернётся.
    "info:subscribe_toggle": _MenuRoute(
        lambda e, u: do_subscribe(e, u), requires_user=True
    ),
    # broadcast:unsubscribe акает сам внутри handle_broadcast_unsubscribe.
    "broadcast:unsubscribe": _MenuRoute(
        lambda e, u: handle_broadcast_unsubscribe(e, u),
        requires_user=True,
        ack=False,
    ),
}

# Префикс → handler(event, appeal_id, max_user_id). Хвост payload'а —
# числовой id обращения (payload.split(":")[2]). Битый id → тап
# «съедается» молча (return True без ack), как в исходном if-elif.
_PREFIX_APPEAL_ID: tuple[tuple[str, Callable], ...] = (
    ("appeal:show:", lambda e, aid, u: show_appeal(e, aid, u)),
    ("appeal:followup:", lambda e, aid, u: start_appeal_followup(e, aid, u)),
    ("appeal:repeat:", lambda e, aid, u: start_appeal_repeat(e, aid, u)),
)


async def _run_exact_route(
    event, route: _MenuRoute, max_user_id: int | None
) -> bool:
    """Выполнить exact-маршрут меню. Контракт — см. docstring _MenuRoute."""
    if route.requires_user and max_user_id is None:
        return route.consume_on_no_user
    if route.ack:
        await ack_callback(event)
    await route.handler(event, max_user_id)
    return True


async def handle_callback(event, payload: str, max_user_id: int | None) -> bool:
    """Маршрутизатор callback'ов меню жителя. Возвращает True, если
    payload обработан, False — если это не меню-callback и вызывающему
    надо продолжить разбор.
    """
    route = _EXACT.get(payload)
    if route is not None:
        return await _run_exact_route(event, route, max_user_id)

    # appeals:page:<N|noop> — пагинация «Мои обращения». noop = текущая
    # страница, тап только ак'ается. Битый хвост глотается без ак'а.
    if payload.startswith("appeals:page:") and max_user_id is not None:
        suffix = payload.split(":", 2)[2]
        if suffix == "noop":
            await ack_callback(event)
            return True
        try:
            page = int(suffix)
        except ValueError:
            return True
        await ack_callback(event)
        await open_my_appeals(event, max_user_id, page=page)
        return True

    # appeal:show: / appeal:followup: / appeal:repeat: — действие по
    # конкретному обращению жителя.
    if max_user_id is not None:
        for prefix, handler in _PREFIX_APPEAL_ID:
            if payload.startswith(prefix):
                try:
                    appeal_id = int(payload.split(":")[2])
                except (IndexError, ValueError):
                    return True
                await ack_callback(event)
                await handler(event, appeal_id, max_user_id)
                return True

    return False
