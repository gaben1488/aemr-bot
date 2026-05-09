from maxapi import Dispatcher

from aemr_bot import keyboards, texts
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import broadcasts as broadcasts_service
from aemr_bot.services import card_format
from aemr_bot.services import settings_store
from aemr_bot.services import users as users_service
from aemr_bot.utils.event import ack_callback, get_chat_id, get_user_id


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
    async with session_scope() as session:
        recep_url = await settings_store.get(session, "electronic_reception_url")
        is_blocked = False
        subscribed = False
        if max_user_id is not None:
            user = await users_service.get_or_create(session, max_user_id=max_user_id)
            is_blocked = user.is_blocked
            if not is_blocked:
                subscribed = await broadcasts_service.is_subscribed(session, max_user_id)

    if is_blocked:
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=(
                "Ваш аккаунт заблокирован — подача обращений и подписка "
                "недоступны. Доступные разделы — ниже. Если блокировка "
                "ошибочна, обратитесь к координатору Администрации."
            ),
            attachments=[keyboards.blocked_user_menu(recep_url)],
        )
        return

    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.WELCOME,
        attachments=[keyboards.main_menu(recep_url, subscribed=subscribed)],
    )


MY_APPEALS_PAGE_SIZE = 5


async def open_my_appeals(event, max_user_id: int, page: int = 1):
    page = max(1, page)
    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        total = await appeals_service.count_for_user(session, user.id)
        if total == 0:
            await event.bot.send_message(
                chat_id=get_chat_id(event),
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
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=header,
        attachments=[
            keyboards.my_appeals_list_keyboard(items, page=page, total_pages=total_pages)
        ],
    )


async def start_appeal_followup(event, appeal_id: int, max_user_id: int):
    """Кнопка «📎 Дополнить» под карточкой обращения у жителя.

    Ставит state в AWAITING_FOLLOWUP_TEXT, сохраняет appeal_id в
    dialog_data. Следующее сообщение жителя (текст и/или вложения)
    пришивается к этому обращению через `_on_awaiting_followup_text`
    в appeal.py.
    """
    from aemr_bot.db.models import AppealStatus, DialogState

    async with session_scope() as session:
        appeal = await appeals_service.get_by_id(session, appeal_id)
        if not appeal or not appeal.user or appeal.user.max_user_id != max_user_id:
            await event.bot.send_message(
                chat_id=get_chat_id(event),
                text="Обращение не найдено.",
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        # Дополнять можно только живые обращения. CLOSED — отдельный
        # путь «Подать похожее», сюда не должен попадать (кнопки нет
        # в карточке закрытого), но защищаемся на случай гонки.
        if appeal.status == AppealStatus.CLOSED.value:
            await event.bot.send_message(
                chat_id=get_chat_id(event),
                text=(
                    "Обращение уже закрыто. Если ситуация повторилась — "
                    "кнопка «🔁 Подать похожее» создаст новое с тем же адресом."
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
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=(
            f"Опишите дополнение к обращению #{appeal_id} одним сообщением. "
            f"Можно приложить фото, видео или файл."
        ),
        attachments=[keyboards.cancel_keyboard()],
    )


async def start_appeal_repeat(event, appeal_id: int, max_user_id: int):
    """Кнопка «🔁 Подать похожее» под карточкой закрытого обращения.

    Запускает воронку нового обращения с уже заполненными locality,
    address и topic из старого. Жителю остаётся только написать суть
    проблемы — это сценарий «опять не вывозят мусор по тому же адресу»,
    когда переписывать всё с нуля раздражает.
    """
    from aemr_bot.db.models import DialogState

    async with session_scope() as session:
        appeal = await appeals_service.get_by_id(session, appeal_id)
        if not appeal or not appeal.user or appeal.user.max_user_id != max_user_id:
            await event.bot.send_message(
                chat_id=get_chat_id(event),
                text="Обращение не найдено.",
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        if not (appeal.locality and appeal.address):
            # Старое обращение без адреса (теоретически может случиться,
            # если оно было создано до миграции с шагом локалити). Тогда
            # просто запускаем обычную воронку.
            from aemr_bot.handlers.appeal import _start_appeal_flow

            await _start_appeal_flow(event, max_user_id)
            return
        await users_service.set_state(
            session,
            max_user_id,
            DialogState.AWAITING_SUMMARY,
            data={
                "locality": appeal.locality,
                "address": appeal.address,
                "topic": appeal.topic,
            },
        )
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=(
            f"Подаём новое обращение с теми же данными:\n"
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
    - NEW / IN_PROGRESS / ANSWERED — «📎 Дополнить» (явный путь
      пришить уточнение к открытому/недавно отвеченному обращению).
    - CLOSED — «🔁 Подать похожее» (создать новое с тем же адресом
      и тематикой; раньше нужно было пройти всю воронку с нуля).
    - Везде «↩ В меню».
    """
    async with session_scope() as session:
        appeal = await appeals_service.get_by_id(session, appeal_id)
        if not appeal or not appeal.user or appeal.user.max_user_id != max_user_id:
            await event.bot.send_message(
                chat_id=get_chat_id(event),
                text="Обращение не найдено.",
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        text = card_format.user_card(appeal)
        status = appeal.status
    await event.bot.send_message(
        chat_id=get_chat_id(event),
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
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.USEFUL_INFO_TITLE,
        attachments=[
            keyboards.useful_info_keyboard(udth, udth_inter, subscribed=subscribed)
        ],
    )


async def do_subscribe(event, max_user_id: int) -> None:
    """Идемпотентная подписка через кнопку «🔔 Подписаться».

    Если житель уже подписан — отвечаем «уже подписаны», не trying to
    сменить состояние. Это закрывает баг старого toggle-варианта, где
    кнопка из устаревшего меню могла отписать жителя в ответ на тап
    «Подписаться».

    Если согласие отозвано или не давалось — даём кнопку «Дать согласие»
    рядом с сообщением, чтобы не отправлять жителя кружным путём через
    «Настройки → Согласие на ПДн → Дать согласие».
    """
    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        if user.is_blocked:
            await event.bot.send_message(
                chat_id=get_chat_id(event),
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
            await event.bot.send_message(
                chat_id=get_chat_id(event),
                text=texts.SUBSCRIBE_MINI_CONSENT,
                attachments=[keyboards.subscribe_mini_consent_keyboard()],
            )
            return
        already = await broadcasts_service.is_subscribed(session, max_user_id)
        if already:
            await event.bot.send_message(
                chat_id=get_chat_id(event),
                text=texts.SUBSCRIBE_ALREADY_ON,
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        await broadcasts_service.set_subscription(session, max_user_id, True)
    await event.bot.send_message(
        chat_id=get_chat_id(event),
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

    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        if user.is_blocked:
            await event.bot.send_message(
                chat_id=get_chat_id(event),
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
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.SUBSCRIBE_CONFIRMED,
        attachments=[keyboards.back_to_menu_keyboard()],
    )


async def do_unsubscribe(event, max_user_id: int) -> None:
    """Идемпотентная отписка через кнопку «🔕 Отписаться»."""
    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        # Заблокированному отписка тоже не нужна — он уже не получает
        # рассылку. Но на всякий случай отметим subscribed=false.
        if user.is_blocked:
            await broadcasts_service.set_subscription(session, max_user_id, False)
            await event.bot.send_message(
                chat_id=get_chat_id(event),
                text=texts.UNSUBSCRIBE_CONFIRMED,
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        already = await broadcasts_service.is_subscribed(session, max_user_id)
        if not already:
            await event.bot.send_message(
                chat_id=get_chat_id(event),
                text=texts.UNSUBSCRIBE_ALREADY_OFF,
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        await broadcasts_service.set_subscription(session, max_user_id, False)
    await event.bot.send_message(
        chat_id=get_chat_id(event),
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
            await event.bot.send_message(
                chat_id=get_chat_id(event),
                text=texts.UNSUBSCRIBE_ALREADY_OFF,
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        await broadcasts_service.set_subscription(session, max_user_id, False)
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.UNSUBSCRIBE_CONFIRMED,
        attachments=[keyboards.back_to_menu_keyboard()],
    )


async def open_settings(event):
    """Подменю «Настройки и помощь» с кнопками-дубликатами для команд /help,
    /policy, /forget. Цель — чтобы житель не запоминал команды."""
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.SETTINGS_MENU_TITLE,
        attachments=[keyboards.settings_menu_keyboard()],
    )


async def open_help(event):
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.HELP_USER,
        attachments=[keyboards.back_to_menu_keyboard()],
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
        async with session_scope() as session:
            user = await users_service.get_or_create(session, max_user_id=max_user_id)
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

    await event.bot.send_message(
        chat_id=get_chat_id(event),
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
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=text,
        attachments=[keyboards.consent_status_keyboard(consent_active=consent_active)],
    )


async def ask_consent_revoke_confirm(event):
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.CONSENT_REVOKE_CONFIRM,
        attachments=[keyboards.consent_revoke_confirm_keyboard()],
    )


async def do_consent_revoke(event, max_user_id: int):
    """Мягкий отзыв согласия через кнопку. Подписка отключается,
    дальнейшие новые обращения требуют дать согласие заново.
    Открытые на момент отзыва обращения остаются в работе.

    Параллельно отправляем плашку в служебную группу: оператор должен
    знать, что по конкретному обращению согласие отозвано — иначе он
    напишет ответ в обычном порядке, попадёт в гард доставки и
    получит «не могу доставить, согласие отозвано» уже постфактум.
    """
    from aemr_bot.config import settings as cfg
    from aemr_bot.services import appeals as appeals_service
    from aemr_bot.services import operators as ops_service

    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        active = await appeals_service.list_unanswered(session)
        my_open = [a for a in active if a.user_id == user.id]
        await users_service.revoke_consent(session, max_user_id)
        await ops_service.write_audit(
            session,
            operator_max_user_id=max_user_id,
            action="self_consent_revoke",
            target=f"user max_id={max_user_id}",
        )
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.CONSENT_REVOKED_OK,
        attachments=[keyboards.back_to_menu_keyboard()],
    )
    # Уведомляем админ-группу про отзыв с конкретным списком открытых
    # обращений — чтобы оператор не тратил время на подготовку ответа
    # для жителя, который отозвался.
    if my_open and cfg.admin_group_id:
        ids = ", ".join(f"#{a.id}" for a in my_open)
        try:
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text=(
                    f"⚠️ Житель отозвал согласие на ПДн.\n"
                    f"Открытые обращения этого жителя: {ids}.\n"
                    f"Доставка ответов жителю по ним заблокирована — "
                    f"свяжитесь по телефону, если он сохранён, или "
                    f"закройте обращения через карточку."
                ),
            )
        except Exception:
            pass


async def do_forget(event, max_user_id: int):
    """Кнопочный аналог /forget. Логика та же, что в start.cmd_forget,
    но без необходимости набирать команду.

    Перед обнулением считаем открытые обращения, чтобы потом сказать
    админ-группе, какие карточки можно убирать из работы.
    """
    from aemr_bot.config import settings as cfg
    from aemr_bot.services import appeals as appeals_service
    from aemr_bot.services import operators as ops_service

    closed_ids: list[int] = []
    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        active = await appeals_service.list_unanswered(session)
        closed_ids = [a.id for a in active if a.user_id == user.id]
        await users_service.erase_pdn(session, max_user_id)
        await ops_service.write_audit(
            session,
            operator_max_user_id=max_user_id,
            action="self_erase",
            target=f"user max_id={max_user_id}",
        )
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.ERASE_REQUESTED,
        attachments=[keyboards.back_to_menu_keyboard()],
    )
    # Сообщаем админ-группе, что карточки этого жителя в работе можно
    # убрать — обращения уже CLOSED, отвечать некому.
    if closed_ids and cfg.admin_group_id:
        ids = ", ".join(f"#{i}" for i in closed_ids)
        try:
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text=(
                    f"🗑 Житель удалил данные. Закрыто без ответа: {ids}.\n"
                    f"Карточки в чате устарели — отвечать не требуется."
                ),
            )
        except Exception:
            pass


async def open_appointment(event):
    async with session_scope() as session:
        text = await settings_store.get(session, "appointment_text")
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=text or "Информация скоро появится.",
        attachments=[keyboards.back_to_menu_keyboard()],
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
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=body,
        attachments=[keyboards.back_to_menu_keyboard()],
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
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=body,
        attachments=[keyboards.back_to_menu_keyboard()],
    )


async def handle_callback(event, payload: str, max_user_id: int | None) -> bool:
    """Пробует обработать нажатие меню, контактов или показа обращения. Возвращает True, если обработано."""
    if payload == "menu:main":
        await ack_callback(event)
        await open_main_menu(event)
        return True

    if payload == "menu:my_appeals":
        if max_user_id is None:
            return True
        await ack_callback(event)
        await open_my_appeals(event, max_user_id)
        return True

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

    if payload == "menu:useful_info":
        await ack_callback(event)
        await open_useful_info(event)
        return True

    if payload == "menu:appointment":
        await ack_callback(event)
        await open_appointment(event)
        return True

    if payload == "menu:settings":
        await ack_callback(event)
        await open_settings(event)
        return True

    if payload == "settings:help":
        await ack_callback(event)
        await open_help(event)
        return True

    if payload == "settings:policy":
        await ack_callback(event)
        from aemr_bot.handlers.start import cmd_policy

        await cmd_policy(event)
        return True

    if payload == "settings:forget_ask":
        await ack_callback(event)
        await ask_forget_confirm(event)
        return True

    if payload == "settings:forget_yes" and max_user_id is not None:
        await ack_callback(event)
        await do_forget(event, max_user_id)
        return True

    if payload == "settings:consent_status" and max_user_id is not None:
        await ack_callback(event)
        await show_consent_status(event, max_user_id)
        return True

    if payload == "settings:consent_revoke_ask":
        await ack_callback(event)
        await ask_consent_revoke_confirm(event)
        return True

    if payload == "settings:consent_revoke_yes" and max_user_id is not None:
        await ack_callback(event)
        await do_consent_revoke(event, max_user_id)
        return True

    if payload == "settings:consent_give" and max_user_id is not None:
        # Запускаем воронку обращения — она сама на первом шаге попросит
        # согласие, потому что consent_pdn_at пуст после отзыва.
        await ack_callback(event)
        from aemr_bot.handlers.appeal import _start_appeal_flow

        await _start_appeal_flow(event, max_user_id)
        return True

    if payload == "info:emergency":
        await ack_callback(event)
        await open_emergency(event)
        return True

    if payload == "info:dispatchers":
        await ack_callback(event)
        await open_dispatchers(event)
        return True

    if payload == "info:subscribe_on" and max_user_id is not None:
        await ack_callback(event)
        await do_subscribe(event, max_user_id)
        return True

    if payload == "subscribe:confirm" and max_user_id is not None:
        await ack_callback(event)
        await do_subscribe_confirm(event, max_user_id)
        return True

    if payload == "info:subscribe_off" and max_user_id is not None:
        await ack_callback(event)
        await do_unsubscribe(event, max_user_id)
        return True

    # Совместимость со старыми меню в чатах: кнопка с payload
    # `info:subscribe_toggle` уйдёт сама собой при обновлении меню,
    # но если житель прямо сейчас тапнет на старое сообщение — не
    # бросаем тап молча. Маршрутизируем в идемпотентный subscribe_on:
    # если уже подписан, увидит «уже подписаны», ни одно состояние
    # не перевернётся.
    if payload == "info:subscribe_toggle" and max_user_id is not None:
        await ack_callback(event)
        await do_subscribe(event, max_user_id)
        return True

    if payload == "broadcast:unsubscribe" and max_user_id is not None:
        await ack_callback(event)
        await handle_broadcast_unsubscribe(event, max_user_id)
        return True

    if payload.startswith("appeal:show:") and max_user_id is not None:
        try:
            appeal_id = int(payload.split(":")[2])
        except (IndexError, ValueError):
            return True
        await ack_callback(event)
        await show_appeal(event, appeal_id, max_user_id)
        return True

    if payload.startswith("appeal:followup:") and max_user_id is not None:
        try:
            appeal_id = int(payload.split(":")[2])
        except (IndexError, ValueError):
            return True
        await ack_callback(event)
        await start_appeal_followup(event, appeal_id, max_user_id)
        return True

    if payload.startswith("appeal:repeat:") and max_user_id is not None:
        try:
            appeal_id = int(payload.split(":")[2])
        except (IndexError, ValueError):
            return True
        await ack_callback(event)
        await start_appeal_repeat(event, appeal_id, max_user_id)
        return True

    return False


def register(dp: Dispatcher) -> None:
    """Заглушка: маршрутизацией нажатий владеет handlers/appeal.py."""
    return None
