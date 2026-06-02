from datetime import datetime, timedelta, timezone
from typing import Literal

from dateutil.relativedelta import relativedelta
from sqlalchemy import case, desc, func, or_, select, update
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
    *,
    is_final: bool = True,
) -> Message:
    """Сохранить ответ оператора. Поведение статуса зависит от is_final.

    is_final=True (по умолчанию) — финальный ответ: обращение
    переводится в ANSWERED, answered_at = now. Это закрывает обращение
    в «отвечено», житель в «Мои обращения» видит его в архиве.

    is_final=False — промежуточный ответ (по запросу владельца): для
    диалога/уточнений оператор отправляет ответ, но обращение
    остаётся IN_PROGRESS («в работе»). Житель получает текст, но
    обращение в его «открытых». Поднимает Appeal.status: NEW →
    IN_PROGRESS (NEW → ANSWERED был бы потерей маркера «оператор
    взял в работу»).

    Если обращение уже CLOSED — не «оживляем» его молчком: статус
    не трогаем, чтобы было видно «оператор ответил по закрытому
    обращению» (исторически фиксируется через message-запись плюс
    audit_log от вызывающего кода).
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
        if is_final:
            appeal.status = AppealStatus.ANSWERED.value
            appeal.answered_at = datetime.now(timezone.utc)
        else:
            # Промежуточный: NEW → IN_PROGRESS, остальные оставляем
            # как есть (IN_PROGRESS остаётся IN_PROGRESS; ANSWERED не
            # «откатываем», в этом случае оператор должен сначала
            # reopen, потом отвечать).
            if appeal.status == AppealStatus.NEW.value:
                appeal.status = AppealStatus.IN_PROGRESS.value
    if operator_id:
        appeal.assigned_operator_id = operator_id
    await session.flush()
    return msg


async def mark_in_progress(session: AsyncSession, appeal_id: int) -> bool:
    """Перевести обращение из NEW в IN_PROGRESS. Возвращает True если
    реально перевёл (был NEW), False если уже не NEW.

    Используется при нажатии «✉️ Ответить» — оператор взял в работу,
    житель видит обновлённый статус в «Мои обращения» до того, как
    придёт ответ. Если оператор отменил ввод — статус остаётся
    IN_PROGRESS («начали работать»), не откатывается в NEW.
    """
    result = await session.execute(
        update(Appeal)
        .where(
            Appeal.id == appeal_id,
            Appeal.status == AppealStatus.NEW.value,
        )
        .values(status=AppealStatus.IN_PROGRESS.value)
    )
    return result.rowcount > 0


async def get_by_id(session: AsyncSession, appeal_id: int) -> Appeal | None:
    return await session.scalar(
        select(Appeal).options(selectinload(Appeal.user)).where(Appeal.id == appeal_id)
    )


async def get_by_id_with_messages(
    session: AsyncSession, appeal_id: int
) -> Appeal | None:
    return await session.scalar(
        select(Appeal)
        .options(selectinload(Appeal.user), selectinload(Appeal.messages))
        .where(Appeal.id == appeal_id)
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


async def followup_rate_limit_stats(
    session: AsyncSession, appeal_id: int, *, hours: int = 1
) -> tuple[int, datetime | None]:
    """SEC #5: rate-limit статистика по followup'ам жителя за `hours` часов.

    Возвращает `(recent_count, last_at)` одним SQL-запросом вместо двух
    раздельных (`count_recent_followups_for_appeal` + `last_followup_at_for_appeal`).
    Меньше round-trip'ов под нагрузкой; обе метрики нужны вместе при
    каждом followup-тапе.

    Считаем только `direction=FROM_USER` — operator-ответы не должны
    блокировать жителя слать дополнения. `last_at` — глобальный max
    (не ограничен окном hours), нужен для min-interval-проверки.
    """
    threshold = datetime.now(timezone.utc) - timedelta(hours=hours)
    row = (
        await session.execute(
            select(
                func.count().filter(Message.created_at >= threshold),
                func.max(Message.created_at),
            ).where(
                Message.appeal_id == appeal_id,
                Message.direction == MessageDirection.FROM_USER.value,
            )
        )
    ).one()
    recent_count, last_at = row
    return (recent_count or 0), last_at


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
    """Установить admin_message_id = mid ПЕРВОЙ карточки (finalize).
    После finalize не вызывается."""
    await session.execute(
        update(Appeal).where(Appeal.id == appeal_id).values(admin_message_id=mid)
    )


async def set_last_admin_card_mid(
    session: AsyncSession, appeal_id: int, mid: str
) -> None:
    """Обновить mid последней event-карточки в админ-чате.

    Вызывается при каждом render новой карточки (finalize, followup,
    reply, status-change). Используется для stale-detection: callback
    на старой карточке (mid != last_admin_card_mid) → ack «устарела»
    + send new.
    """
    await session.execute(
        update(Appeal).where(Appeal.id == appeal_id).values(last_admin_card_mid=mid)
    )


async def has_operator_message(
    session: AsyncSession, appeal_id: int
) -> bool:
    """True если хотя бы один ответ оператора уже доставлен по обращению.

    Используется в `run_close` (P2 #23): если оператор отправлял
    промежуточный ответ, потом закрывает «без ответа» — показываем
    подсказку про «✉️ Ответить и закрыть» как более корректный путь.
    """
    return bool(
        await session.scalar(
            select(Message.id)
            .where(
                Message.appeal_id == appeal_id,
                Message.direction == MessageDirection.FROM_OPERATOR.value,
            )
            .limit(1)
        )
    )


ReopenResult = Literal[
    "reopened", "already_open", "blocked_by_revoke", "not_found"
]


async def reopen(session: AsyncSession, appeal_id: int) -> ReopenResult:
    """Возобновить обращение: ANSWERED/CLOSED → IN_PROGRESS.

    Возвращает один из:
    - `"reopened"` — статус сменился (ANSWERED/CLOSED → IN_PROGRESS).
    - `"already_open"` — обращение уже NEW/IN_PROGRESS, no-op (повторный
      клик кнопки «🔁 Возобновить» не должен переписывать timestamps).
    - `"blocked_by_revoke"` — обращение закрыто из-за отзыва согласия
      или удаления ПДн (`closed_due_to_revoke=true`). Возобновлять
      нельзя: доставка ответа всё равно запрещена guard'ами ПДн.
      Оператор видит понятное сообщение вместо «Не найдено».
    - `"not_found"` — обращения с таким id нет в БД.

    Раньше возвращал bool — все три negative-case схлопывались в False,
    оператор для closed_due_to_revoke видел «Обращение не найдено», что
    дезориентировало. См. P1 #21.
    """
    # Сначала читаем актуальный статус и флаг revoke — двух SELECT'ов
    # нет, один row. Race с конкурентной правкой того же обращения
    # маловероятен (операторский UI, не машинный поток), но если он
    # есть — UPDATE ниже всё равно не сработает (where-clause не
    # совпадёт) и мы вернёмся к разводящей логике на следующем клике.
    row = (
        await session.execute(
            select(Appeal.status, Appeal.closed_due_to_revoke).where(
                Appeal.id == appeal_id
            )
        )
    ).one_or_none()
    if row is None:
        return "not_found"
    status, blocked = row
    if blocked:
        return "blocked_by_revoke"
    if status not in {AppealStatus.ANSWERED.value, AppealStatus.CLOSED.value}:
        return "already_open"
    await session.execute(
        update(Appeal)
        .where(Appeal.id == appeal_id)
        .values(
            status=AppealStatus.IN_PROGRESS.value,
            answered_at=None,
            closed_at=None,
        )
    )
    return "reopened"


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

    Если планируете дальше рендерить `admin_card.render(appeal)` —
    используйте `list_unanswered_with_messages` (с догруженной
    перепиской). Иначе timeline в карточке окажется пустым.
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


async def list_unanswered_with_messages(
    session: AsyncSession, *, limit: int = 500
) -> list[Appeal]:
    """То же, что `list_unanswered`, но дополнительно догружает
    `Appeal.messages` (selectinload).

    SACRED #5: если карточка обращения публикуется в admin chat через
    `admin_card.render`, нужны загруженные `messages` — без них блок
    «История переписки» пуст (см. `card_format._loaded_messages` —
    он намеренно не делает lazy-load, чтобы не падать в async-сессии
    после её закрытия). Если использовать `list_unanswered` (без
    messages), карточка #N окажется без переписки.

    Цена: один лишний JOIN на каждый Appeal. На N≤500 (LIMIT) это
    одно SELECT с messages в одном round-trip через selectinload.
    """
    res = await session.scalars(
        select(Appeal)
        .options(selectinload(Appeal.user), selectinload(Appeal.messages))
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
    session: AsyncSession, sla_hours: int, *, limit: int = 500
) -> list[Appeal]:
    """Обращения, которые в работе/новые дольше SLA и пока без ответа.

    Используется часовым SLA-алёртом в `services/cron.py::sla_overdue_check`:
    раз в час смотрим, что висит дольше `sla_response_hours`, и шлём
    оператору список просроченных. Если ничего не висит — алерта нет
    (тишина в группе ценнее, чем «по нулям» каждый час).

    Возвращает результат с догруженным `user`, чтобы вызывающий код
    мог показать имя жителя без N+1.

    LIMIT 500 — защита от лавины (то же, что `list_unanswered`): если
    оператор был в отпуске, и месяцами копится сотни просрочек, без
    лимита cron-алерт тянет всё разом + selectinload по `user` создаёт
    второй IN-query на тысячи id. Дальше всё равно обрезается в
    форматтере, но БД-запрос уже отдал всё. 500 заведомо больше
    реальной очереди.
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
        .limit(limit)
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
        # scalar() и так берёт первую строку; .limit(1) лишь снимает с БД
        # обязанность отсортировать и материализовать весь набор открытых
        # обращений жителя (обычно 1-2, но гарантию даёт limit). Поведение
        # идентично — возвращается то же самое последнее обращение.
        .limit(1)
    )
