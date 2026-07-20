from datetime import datetime, timedelta, timezone
from typing import Literal

from dateutil.relativedelta import relativedelta
from sqlalchemy import case, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from aemr_bot.db.models import Appeal, AppealStatus, Message, MessageDirection, User
from aemr_bot.services import sla as sla_service


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
    # Гард — прямо в UPDATE WHERE (как в close/mark_in_progress), а не
    # read-then-write: статус-предикат ANSWERED/CLOSED + флаг
    # closed_due_to_revoke=False делают запись безопасной под гонкой с
    # /forget. Раньше UPDATE был безусловным (WHERE только по id) поверх
    # предварительного SELECT'а — между чтением и записью конкурентный
    # отзыв согласия мог выставить closed_due_to_revoke=True, а reopen
    # всё равно воскрешал стёртое обращение (status=IN_PROGRESS при
    # closed_due_to_revoke=True). Теперь такое обновление не сматчит.
    result = await session.execute(
        update(Appeal)
        .where(
            Appeal.id == appeal_id,
            Appeal.status.in_(
                [AppealStatus.ANSWERED.value, AppealStatus.CLOSED.value]
            ),
            Appeal.closed_due_to_revoke.is_(False),
        )
        .values(
            status=AppealStatus.IN_PROGRESS.value,
            answered_at=None,
            closed_at=None,
        )
    )
    if result.rowcount > 0:
        return "reopened"
    # UPDATE не затронул строк — гард отсёк запись. Разрешаем причину
    # повторным чтением актуального состояния (already_open /
    # blocked_by_revoke / not_found), сохраняя прежний контракт возврата.
    row = (
        await session.execute(
            select(Appeal.status, Appeal.closed_due_to_revoke).where(
                Appeal.id == appeal_id
            )
        )
    ).one_or_none()
    if row is None:
        return "not_found"
    _status, blocked = row
    if blocked:
        return "blocked_by_revoke"
    return "already_open"


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


async def list_unanswered_for_user(
    session: AsyncSession, user_id: int, *, limit: int = 500
) -> list[Appeal]:
    """Открытые обращения (NEW + IN_PROGRESS) КОНКРЕТНОГО жителя, с
    догруженными user и messages (selectinload).

    Прицельный вариант `list_unanswered_with_messages`: `WHERE user_id`
    задаётся прямо в SQL, а не «загрузить все открытые (до LIMIT 500) и
    отфильтровать по user_id в Python». Используется в citizen-хендлерах
    отзыва согласия и удаления данных (menu.do_consent_revoke,
    ask_forget_confirm, ask_goodbye_erase_confirm, do_forget), где нужен
    список открытых обращений именно этого жителя.

    `messages` догружаются (selectinload), потому что `do_consent_revoke`
    публикует `admin_card.render(appeal)` по каждому открытому обращению —
    без загруженной переписки timeline в карточке пуст (SACRED #5, та же
    причина, что в `list_unanswered_with_messages`). Прочим вызывающим
    (счётчики/списки id/тем) лишний selectinload по messages стоит одного
    IN-запроса на N≤LIMIT обращений жителя — на реальной очереди 1-2
    открытых обращений это пренебрежимо.
    """
    res = await session.scalars(
        select(Appeal)
        .options(selectinload(Appeal.user), selectinload(Appeal.messages))
        .where(
            Appeal.user_id == user_id,
            Appeal.status.in_(
                [AppealStatus.NEW.value, AppealStatus.IN_PROGRESS.value]
            ),
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

    Просрочка считается по РАБОЧЕМУ времени (services/sla.py), а не
    календарному: обращение, поступившее в пятницу вечером, не должно
    считаться просроченным в субботу, если рабочих часов ещё не было.

    SQL-префильтр ниже (`created_at <= now - sla_hours` КАЛЕНДАРНО)
    сознательно оставлен как есть — это корректная надмножество-
    оптимизация, а не забытая календарная логика: business-время между
    двумя моментами всегда ≤ календарного (рабочее окно — подмножество
    суток), поэтому если business_seconds_between(created_at, now) уже
    достиг sla_hours*3600, то и календарная разница now - created_at
    тем более ≥ sla_hours часов. Значит порог по календарному времени
    не может ОТСЕЯТЬ настоящую просрочку (не теряет кандидатов), а лишь
    расширяет выборку — точный отбор доделывает Python-фильтр по
    `sla_service.is_overdue` ниже. Смысл префильтра — не тащить из БД
    вообще все открытые обращения (см. защиту LIMIT выше), а не
    заменить собой правильный business-time расчёт.
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
    candidates = list(res)
    now = datetime.now(timezone.utc)
    return [
        appeal
        for appeal in candidates
        if sla_service.is_overdue(appeal.created_at, now, sla_hours)
    ]


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
    """Стереть текстовые поля и attachments у обращений старше N лет.

    Сами строки appeal/messages остаются — это нужно для статистики
    количества и сохранения связи с обезличенным жителем. Содержимое
    (summary, address, text сообщений, attachments) NULL'ится.

    Срок отсчитывается от даты отработки (закрытия либо ответа), а для
    брошенных обращений — от даты подачи; статус при этом не важен.
    Подробнее про якорь отсчёта — в комментарии ниже.

    Срок N лет — установлен ОПЕРАТОРОМ исходя из цели обработки
    (152-ФЗ ст. 18.1 ч. 1 п. 2 — локальный акт определяет способы и
    сроки хранения; ст. 5 ч. 7 — не дольше, чем требует цель). В самом
    152-ФЗ числа «5 лет» нет: срок закрепляется локальным актом
    оператора (см. задачу «Вписать бот в ОРД»), а не выводится из
    закона напрямую.

    Откуда именно 5 лет (решение владельца 2026-07-17). Два довода:
    (1) сообщение в боте может оказаться СМЕЖНЫМ с официальным
    обращением гражданина — бот работает вне 59-ФЗ, но житель нередко
    пишет по тому же вопросу и в электронную приёмную, и срок берётся
    по аналогии с практикой хранения обращений граждан; (2) цели
    отчётности и анализа эффективности принятых мер — по обращению
    надо видеть, что было сделано, в горизонте нескольких лет.
    Это осознанный выбор оператора с понятным ориентиром, а не
    требование закона: писать «требование 152-ФЗ» здесь нельзя.

    Возвращает (purged_appeals, purged_messages).
    """
    # `relativedelta(years=N)` корректно учитывает високосные — иначе
    # `timedelta(days=365*N)` теряет ~1 день за 4 года, и за 5-летний
    # порог retention обращения, поданные ровно 5 лет назад, могут
    # не попасть в первую же ночь. Не критично, но честнее.
    threshold = datetime.now(timezone.utc) - relativedelta(years=years)
    # Якорь отсчёта — COALESCE(closed_at, answered_at). closed_at ставит
    # ТОЛЬКО явное «Закрыть» (close()); финальный ответ оператора
    # (add_operator_message is_final=True) переводит в ANSWERED и ставит
    # answered_at, но closed_at оставляет NULL. Раньше фильтр требовал
    # `closed_at IS NOT NULL`, поэтому обращения на ОСНОВНОМ пути
    # «оператор ответил» (ANSWERED без явного закрытия) не чистились
    # НИКОГДА — адрес и текст висели бессрочно. Берём answered_at как
    # запасной якорь: и ANSWERED, и CLOSED считаются «отработанными» с
    # момента ответа/закрытия.
    #
    # Третий, страховочный якорь — created_at, и вместе с ним снят фильтр
    # по статусу. Обращение, БРОШЕННОЕ в NEW/IN_PROGRESS (никто так и не
    # ответил — оператор уволился, обращение потерялось в завале), не
    # имеет ни closed_at, ни answered_at, поэтому под прежнее условие не
    # попадало НИКОГДА: адрес и текст жителя оставались в базе бессрочно.
    # Молчаливое бессрочное хранение — ровно то, чего не должно быть.
    # Смысл третьего якоря: если обращение отработано — считаем от даты
    # ответа/закрытия; если за N лет так и не отработано — считаем от даты
    # подачи. Обнуление текста у формально открытого обращения не потеря:
    # спустя пять лет на него всё равно уже не ответят, а ПДн в нём живые.
    retention_anchor = func.coalesce(
        Appeal.closed_at, Appeal.answered_at, Appeal.created_at
    )
    closed_old_appeals = select(Appeal.id).where(
        retention_anchor.isnot(None),
        retention_anchor <= threshold,
    )
    # address — тоже ПДн (адрес обращения жителя). При erase по отзыву
    # согласия (services/users.py::_redact_appeal_payloads_for_user)
    # address уже чистится вместе с summary/attachments; здесь этой же
    # симметрии не хватало — retention обнулял summary/attachments,
    # но оставлял address висеть в БД дольше срока, установленного
    # оператором. Приводим к тому же поведению.
    appeals_result = await session.execute(
        update(Appeal)
        .where(
            Appeal.id.in_(closed_old_appeals),
            or_(
                Appeal.summary.isnot(None),
                Appeal.attachments != [],
                Appeal.address.isnot(None),
            ),
        )
        .values(summary=None, attachments=[], address=None)
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
