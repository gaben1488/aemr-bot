from datetime import datetime, timedelta, timezone

from dateutil.relativedelta import relativedelta
from sqlalchemy import desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from aemr_bot.db.models import Appeal, AppealStatus, Message, MessageDirection, User


async def create_appeal(
    session: AsyncSession,
    user: User,
    address: str,
    topic: str,
    summary: str,
    attachments: list,
    locality: str | None = None,
) -> Appeal:
    appeal = Appeal(
        user_id=user.id,
        status=AppealStatus.NEW.value,
        locality=locality,
        address=address,
        topic=topic,
        summary=summary,
        attachments=attachments,
    )
    session.add(appeal)
    await session.flush()
    return appeal


async def add_user_message(
    session: AsyncSession,
    appeal: Appeal,
    text: str | None,
    attachments: list | None = None,
    max_message_id: str | None = None,
) -> Message:
    msg = Message(
        appeal_id=appeal.id,
        direction=MessageDirection.FROM_USER.value,
        text=text,
        attachments=attachments or [],
        max_message_id=max_message_id,
    )
    session.add(msg)
    await session.flush()
    return msg


async def add_operator_message(
    session: AsyncSession,
    appeal: Appeal,
    text: str,
    operator_id: int | None,
    max_message_id: str | None,
) -> Message:
    """Сохранить ответ оператора и перевести обращение в ANSWERED.

    Если обращение уже CLOSED — не «оживляем» его молчком: статус
    не трогаем, чтобы было видно «оператор ответил по закрытому
    обращению» (исторически фиксируется через message-запись плюс
    audit_log от вызывающего кода). Без этой защиты повторный клик
    «✉️ Ответить» под старой карточкой закрытого обращения переписал
    бы статус CLOSED→ANSWERED де-факто переоткрытие без аудита.
    """
    msg = Message(
        appeal_id=appeal.id,
        direction=MessageDirection.FROM_OPERATOR.value,
        text=text,
        max_message_id=max_message_id,
        operator_id=operator_id,
    )
    session.add(msg)
    if appeal.status != AppealStatus.CLOSED.value:
        appeal.status = AppealStatus.ANSWERED.value
        appeal.answered_at = datetime.now(timezone.utc)
    if operator_id:
        appeal.assigned_operator_id = operator_id
    await session.flush()
    return msg


async def get_by_id(session: AsyncSession, appeal_id: int) -> Appeal | None:
    return await session.scalar(
        select(Appeal).options(selectinload(Appeal.user)).where(Appeal.id == appeal_id)
    )


async def get_by_admin_message_id(session: AsyncSession, admin_message_id: str) -> Appeal | None:
    return await session.scalar(
        select(Appeal)
        .options(selectinload(Appeal.user))
        .where(Appeal.admin_message_id == admin_message_id)
    )


async def list_for_user(
    session: AsyncSession,
    user_id: int,
    limit: int = 20,
    offset: int = 0,
) -> list[Appeal]:
    """Список обращений жителя для экрана «📂 Мои обращения».

    Сортировка: открытые сверху, завершённые внизу. В рамках каждой
    группы — по дате создания, новые первыми. Это удобнее простого
    «по дате»: житель сразу видит то, по чему ещё ждёт ответа.

    Если житель отзывал согласие — обращения, поданные до точки
    отзыва, в списке не показываем. После /forget человек
    концептуально новый, ему незачем видеть свою прошлую жизнь.
    Записи в БД сохраняются для статистики и аудита.
    """
    from sqlalchemy import case

    user = await session.scalar(select(User).where(User.id == user_id))
    query = select(Appeal).where(Appeal.user_id == user_id)
    if user is not None and user.consent_revoked_at is not None:
        query = query.where(Appeal.created_at > user.consent_revoked_at)
    # Приоритет статусов: открытые (NEW, IN_PROGRESS) — 0, ANSWERED — 1,
    # CLOSED — 2. ORDER BY priority ASC, created_at DESC.
    status_priority = case(
        (Appeal.status == AppealStatus.NEW.value, 0),
        (Appeal.status == AppealStatus.IN_PROGRESS.value, 0),
        (Appeal.status == AppealStatus.ANSWERED.value, 1),
        else_=2,
    )
    res = await session.scalars(
        query.order_by(status_priority, desc(Appeal.created_at))
        .limit(limit)
        .offset(offset)
    )
    return list(res)


async def count_recent_for_user(
    session: AsyncSession, user_id: int, *, hours: int = 1
) -> int:
    """Сколько обращений жителя за последние `hours` часов.

    Используется как rate-limit при создании нового обращения: если
    житель прислал 3+ обращения за час, отказываемся принимать
    четвёртое и предлагаем дополнить уже открытое. Защита от
    случайного спама и от злоупотреблений.
    """
    threshold = datetime.now(timezone.utc) - timedelta(hours=hours)
    return (
        await session.scalar(
            select(func.count())
            .select_from(Appeal)
            .where(Appeal.user_id == user_id, Appeal.created_at >= threshold)
        )
    ) or 0


async def count_for_user(session: AsyncSession, user_id: int) -> int:
    """Счётчик обращений жителя для пагинации «📂 Мои обращения».

    Симметрично с list_for_user: после отзыва согласия обращения до
    точки отзыва из счётчика тоже исключаем — иначе пагинация
    показывает «1/3» при пустых видимых страницах.
    """
    user = await session.scalar(select(User).where(User.id == user_id))
    query = select(func.count()).select_from(Appeal).where(Appeal.user_id == user_id)
    if user is not None and user.consent_revoked_at is not None:
        query = query.where(Appeal.created_at > user.consent_revoked_at)
    return (await session.scalar(query)) or 0


async def set_admin_message_id(session: AsyncSession, appeal_id: int, mid: str) -> None:
    await session.execute(
        update(Appeal).where(Appeal.id == appeal_id).values(admin_message_id=mid)
    )


async def reopen(session: AsyncSession, appeal_id: int) -> bool:
    """Возобновить обращение: ANSWERED/CLOSED → IN_PROGRESS.

    Если обращение уже NEW/IN_PROGRESS, ничего не меняем и возвращаем
    False — повторный клик кнопки «🔁 Возобновить» не должен переписывать
    timestamps и плодить ложные записи в audit_log.

    Обращения, закрытые из-за отзыва согласия, удаления ПДн или ручной
    блокировки (`closed_due_to_revoke=true`), не переоткрываются. Иначе
    операторская кнопка могла бы вернуть в работу обращение, по которому
    доставка ответа всё равно запрещена guard'ами ПДн.
    """
    result = await session.execute(
        update(Appeal)
        .where(
            Appeal.id == appeal_id,
            Appeal.closed_due_to_revoke.is_(False),
            Appeal.status.in_(
                [AppealStatus.ANSWERED.value, AppealStatus.CLOSED.value]
            ),
        )
        .values(status=AppealStatus.IN_PROGRESS.value, answered_at=None, closed_at=None)
    )
    return result.rowcount > 0


async def close(session: AsyncSession, appeal_id: int) -> bool:
    """Закрыть обращение без ответа.

    Если обращение уже CLOSED, ничего не меняем — повторный клик
    «⛔ Закрыть» не должен переписывать closed_at.
    """
    result = await session.execute(
        update(Appeal)
        .where(
            Appeal.id == appeal_id,
            Appeal.status != AppealStatus.CLOSED.value,
        )
        .values(status=AppealStatus.CLOSED.value, closed_at=datetime.now(timezone.utc))
    )
    return result.rowcount > 0


async def list_unanswered(
    session: AsyncSession, *, limit: int = 500
) -> list[Appeal]:
    """Все открытые обращения (NEW + IN_PROGRESS) с догруженным user.

    Используется напоминалкой `working_hours_open_reminder`. На входе
    операторской логике делим список в Python на две группы — те, что в
    SLA, и просроченные — отдельным проходом по `sla_response_hours`.
    Один SQL вместо двух.

    LIMIT 500 — защита от лавины: на годовом архиве с тысячами
    открытых обращений (если оператор в отпуске) cron-напоминалка
    иначе вытащит всё разом и в `_format_appeal_lines` обрежется
    до 10, но БД-запрос уже отдал всё. Для обычной работы 500
    заведомо больше реальной очереди.
    """
    res = await session.scalars(
        select(Appeal)
        .options(selectinload(Appeal.user))
        .where(
            Appeal.status.in_(
                [AppealStatus.NEW.value, AppealStatus.IN_PROGRESS.value]
            )
        )
        .order_by(Appeal.created_at)
        .limit(limit)
    )
    return list(res)


async def find_overdue_unanswered(
    session: AsyncSession, sla_hours: int
) -> list[Appeal]:
    """Обращения, которые в работе/новые дольше SLA и пока без ответа.

    Используется часовым SLA-алёртом в `services/cron.py::sla_overdue_check`:
    раз в час смотрим, что висит дольше `sla_response_hours`, и шлём
    оператору список просроченных. Если ничего не висит — алерта нет
    (тишина в группе ценнее, чем «по нулям» каждый час).

    Возвращает результат с догруженным `user`, чтобы вызывающий код
    мог показать имя жителя без N+1.
    """
    threshold = datetime.now(timezone.utc) - timedelta(hours=sla_hours)
    res = await session.scalars(
        select(Appeal)
        .options(selectinload(Appeal.user))
        .where(
            Appeal.status.in_(
                [AppealStatus.NEW.value, AppealStatus.IN_PROGRESS.value]
            ),
            Appeal.created_at <= threshold,
        )
        .order_by(Appeal.created_at)
    )
    return list(res)


async def count_open(session: AsyncSession) -> int:
    """Сколько обращений висит без ответа (NEW + IN_PROGRESS).

    Используется в счётчике на кнопке «📋 Открытые обращения» в
    меню оператора, чтобы координатор сразу видел нагрузку, не нажимая.
    """
    return (
        await session.scalar(
            select(func.count())
            .select_from(Appeal)
            .where(
                Appeal.status.in_(
                    [AppealStatus.NEW.value, AppealStatus.IN_PROGRESS.value]
                )
            )
        )
    ) or 0


async def purge_old_appeals_content(
    session: AsyncSession, *, years: int = 5
) -> tuple[int, int]:
    """Стереть текстовые поля и attachments у обращений, закрытых
    больше N лет назад. Сами строки appeal/messages остаются — это
    нужно для статистики количества и сохранения связи с обезличенным
    жителем. Содержимое (summary, text сообщений, attachments) NULL'ится.

    Срок 5 лет — стандарт делопроизводства в органах власти (по приказу
    Минкультуры о номенклатуре дел) и одновременно соответствует
    152-ФЗ ст. 5 ч. 7 «срок хранения ПДн не должен превышать сроков,
    необходимых для целей обработки».

    Возвращает (purged_appeals, purged_messages).
    """
    # `relativedelta(years=N)` корректно учитывает високосные — иначе
    # `timedelta(days=365*N)` теряет ~1 день за 4 года, и за 5-летний
    # порог retention обращения, поданные ровно 5 лет назад, могут
    # не попасть в первую же ночь. Не критично, но честнее.
    threshold = datetime.now(timezone.utc) - relativedelta(years=years)
    closed_old_appeals = select(Appeal.id).where(
        Appeal.status.in_([AppealStatus.ANSWERED.value, AppealStatus.CLOSED.value]),
        Appeal.closed_at.isnot(None),
        Appeal.closed_at <= threshold,
    )
    appeals_result = await session.execute(
        update(Appeal)
        .where(
            Appeal.id.in_(closed_old_appeals),
            or_(Appeal.summary.isnot(None), Appeal.attachments != []),
        )
        .values(summary=None, attachments=[])
    )
    purged_appeals = appeals_result.rowcount or 0

    # Сообщения переписки (followup жителя, ответ оператора) — обнуляем
    # text/attachments у сообщений закрытых старых обращений. Важно
    # проверять отдельно attachments: после раннего ручного стирания text
    # уже может быть NULL, но file/photo payload ещё не пустой.
    msg_result = await session.execute(
        update(Message)
        .where(
            Message.appeal_id.in_(closed_old_appeals),
            or_(Message.text.isnot(None), Message.attachments != []),
        )
        .values(text=None, attachments=[])
    )
    purged_messages = msg_result.rowcount or 0
    return purged_appeals, purged_messages


async def find_last_address_for_user(
    session: AsyncSession, user_id: int
) -> tuple[str, str] | None:
    """Последний (locality, address), которые житель уже подавал.

    Используется в воронке нового обращения: если предыдущий раз
    житель писал по тому же адресу, бот предложит «использовать тот же
    адрес?» — пропускаются два шага. Берём из последнего обращения с
    заполненными обоими полями (locality и address); если их нет —
    возвращаем None и воронка спрашивает заново.

    Если житель отзывал согласие — обращения старше точки отзыва
    игнорируем. После отзыва человек концептуально «новый», и
    подсовывать ему адрес из прошлой жизни — неправильно. Берём
    только обращения, поданные ПОСЛЕ последнего consent_revoked_at.
    """
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        return None
    query = (
        select(Appeal)
        .where(
            Appeal.user_id == user_id,
            Appeal.locality.isnot(None),
            Appeal.address.isnot(None),
        )
        .order_by(desc(Appeal.created_at))
        .limit(1)
    )
    if user.consent_revoked_at is not None:
        query = query.where(Appeal.created_at > user.consent_revoked_at)
    res = await session.scalar(query)
    if res is None or not res.locality or not res.address:
        return None
    return res.locality, res.address


async def find_active_for_user(session: AsyncSession, user_id: int) -> Appeal | None:
    """Последнее неотвеченное обращение жителя.

    Сюда попадают только NEW и IN_PROGRESS. Если обращение уже ANSWERED
    или CLOSED, повтор по нему оформляется новым связанным обращением,
    а не переоткрытием старого.

    Используется в IDLE-обработчике, чтобы подсказать жителю с активным
    обращением «откройте Мои обращения и нажмите Дополнить», а не
    отвечать общей «не понял».
    """
    return await session.scalar(
        select(Appeal)
        .where(
            Appeal.user_id == user_id,
            Appeal.status.in_(
                [
                    AppealStatus.NEW.value,
                    AppealStatus.IN_PROGRESS.value,
                ]
            ),
        )
        .order_by(desc(Appeal.created_at))
    )
