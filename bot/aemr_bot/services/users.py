import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from aemr_bot.config import settings as cfg
from aemr_bot.db.models import (
    ANONYMOUS_MAX_USER_ID,
    Appeal,
    AppealStatus,
    DialogState,
    Message,
    User,
)

# Постоянный ключ Postgres advisory-lock для пути «создать anonymous user
# на лету». Используется только в get_anonymous_user_id, чтобы две
# параллельные корутины не пробивали UNIQUE на max_user_id одновременно.
# Любая bigint-константа подойдёт; выбрана не-нулевая, чтобы не
# спутаться с дефолтами.
_ANONYMOUS_USER_LOCK_KEY = 0x4145_4D52_414E_4F4E  # 'AEMR_ANON' в hex
log = logging.getLogger(__name__)


def _normalize_phone(phone: str) -> str:
    """Нормализация телефона под сравнение: оставляем цифры, остальное убираем.

    Граждане сдают телефон в любом формате, который кнопка контакта в
    MAX отдаёт: «+7 (415-31) 7-25-29», «89001234567», «79001234567».
    Операторы в админ-чате печатают то, что помнят. Сравниваем только
    цифры, при необходимости срезаем ведущий код страны 7 или 8.
    """
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        digits = digits[1:]
    return digits


async def get_or_create(session: AsyncSession, max_user_id: int, first_name: str | None = None) -> User:
    user = await session.scalar(select(User).where(User.max_user_id == max_user_id))
    if user is None:
        user = User(max_user_id=max_user_id, first_name=first_name)
        session.add(user)
        await session.flush()
    return user


async def find_by_max_id(session: AsyncSession, max_user_id: int) -> User | None:
    """Read-only поиск без создания. Используется для уведомлений
    оператору, где запись жителя точно должна существовать; если её
    нет — пишем «—» вместо создания фантомной."""
    return await session.scalar(
        select(User).where(User.max_user_id == max_user_id)
    )


async def has_consent(session: AsyncSession, max_user_id: int) -> bool:
    user = await session.scalar(select(User).where(User.max_user_id == max_user_id))
    return bool(user and user.consent_pdn_at)


async def set_consent(session: AsyncSession, max_user_id: int) -> None:
    """Дать (или возобновить) согласие на обработку ПДн.

    Снимаем is_blocked: житель мог раньше воспользоваться /forget или
    его блокировал IT, потом вернулся и дал согласие заново — это
    явное «свяжитесь со мной снова», блокировка устаревает.

    Обнуляем consent_revoked_at: иначе свежее согласие соседствует
    с давним отзывом, и retention-cron через 30 дней с того отзыва
    обезличит жителя несмотря на актуальное согласие.
    """
    await session.execute(
        update(User)
        .where(User.max_user_id == max_user_id)
        .values(
            consent_pdn_at=datetime.now(timezone.utc),
            consent_revoked_at=None,
            # SEC #1: НЕ сбрасываем is_blocked здесь — иначе blocked житель,
            # тапнув старую кнопку «✅ Согласен» из истории чата (после того
            # как IT заблокировал его), снимет блок себе сам. Разблокировку
            # делает только IT через admin_audience / admin_appeal_ops с
            # ensure_role(IT). is_blocked сбрасывается явно через
            # users_service.set_blocked(blocked=False).
        )
    )


async def set_phone(session: AsyncSession, max_user_id: int, phone: str) -> None:
    # Держим phone_normalized в синхроне с phone: это индексированная колонка,
    # из которой читает find_by_phone, и любое расхождение тихо сломает
    # /erase phone=.
    await session.execute(
        update(User)
        .where(User.max_user_id == max_user_id)
        .values(phone=phone, phone_normalized=_normalize_phone(phone) or None)
    )


async def set_first_name(session: AsyncSession, max_user_id: int, first_name: str) -> None:
    await session.execute(update(User).where(User.max_user_id == max_user_id).values(first_name=first_name))


async def set_state(session: AsyncSession, max_user_id: int, state: DialogState, data: dict | None = None) -> None:
    values: dict = {"dialog_state": state.value}
    if data is not None:
        values["dialog_data"] = data
    await session.execute(update(User).where(User.max_user_id == max_user_id).values(**values))


async def reset_state(session: AsyncSession, max_user_id: int) -> None:
    await session.execute(
        update(User)
        .where(User.max_user_id == max_user_id)
        .values(dialog_state=DialogState.IDLE.value, dialog_data={})
    )


async def update_dialog_data(session: AsyncSession, max_user_id: int, patch: dict) -> dict:
    """Read-modify-write апдейт jsonb dialog_data с защитой от гонки.

    Без advisory-lock два параллельных callback'а одного жителя
    (например, нажал две кнопки подряд) делают read-modify-write на
    одной строке — последний writer переписывает изменения первого.
    `pg_advisory_xact_lock(max_user_id)` сериализует параллельные
    транзакции по этому конкретному `max_user_id`, не трогая других
    жителей. Lock освобождается на commit/rollback автоматически.

    На SQLite (тесты с `_PSEUDO_DB`) advisory_xact_lock отсутствует —
    игнорируем ошибку и идём дальше: для unit-тестов гонок нет.
    """
    try:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": int(max_user_id)},
        )
    except Exception:
        # SQLite / unsupported backend — без lock-а живём, в production
        # это postgres и lock работает.
        log.debug("advisory-lock недоступен для backend тестов", exc_info=True)
    user = await session.scalar(select(User).where(User.max_user_id == max_user_id))
    if user is None:
        return {}
    data = dict(user.dialog_data or {})
    data.update(patch)
    user.dialog_data = data
    await session.flush()
    return data


async def find_stuck_in_summary(
    session: AsyncSession,
    idle_seconds: int,
    limit: int | None = None,
) -> list[int]:
    """Вернуть max_user_id пользователей, застрявших в AWAITING_SUMMARY
    дольше idle_seconds.

    Лимит защищает от патологических случаев: например, 10 тысяч
    застрявших строк после долгого простоя иначе породят 10 тысяч
    вызовов API бота при восстановлении на старте.
    """
    if limit is None:

        limit = cfg.recover_batch_size
    threshold = datetime.now(timezone.utc) - timedelta(seconds=idle_seconds)
    result = await session.scalars(
        select(User.max_user_id)
        .where(
            User.dialog_state == DialogState.AWAITING_SUMMARY.value,
            User.updated_at <= threshold,
        )
        .limit(limit)
    )
    return list(result)


async def find_stuck_in_funnel(
    session: AsyncSession,
    idle_seconds: int,
    limit: int | None = None,
) -> list[tuple[int, str]]:
    """Все жители, застрявшие в любом промежуточном шаге воронки дольше
    `idle_seconds`. Возвращает [(max_user_id, dialog_state), ...].

    Используется фоновым watchdog'ом — раз в N часов сканирует и
    деликатно сбрасывает зависшие воронки в IDLE с напоминанием.
    Без этого житель, начавший «Написать обращение» и закрывший MAX,
    получает любой следующий текст в обработчик «продолжай шаг», а
    случайное «привет» через неделю запишется как имя/адрес/тема.

    Список состояний — все ожидания КРОМЕ AWAITING_SUMMARY: тот
    обрабатывается отдельно через find_stuck_in_summary с другим
    набором действий (там есть собранные attachments, которые надо
    финализировать как обращение).
    """
    if limit is None:

        limit = cfg.recover_batch_size
    pending_states = [
        DialogState.AWAITING_CONSENT.value,
        DialogState.AWAITING_CONTACT.value,
        DialogState.AWAITING_NAME.value,
        DialogState.AWAITING_LOCALITY.value,
        # Geo-confirm — такой же промежуточный шаг, как выбор поселения
        # или адрес. Раньше watchdog его не видел, и зависшая карточка
        # после геолокации могла оставаться навсегда.
        DialogState.AWAITING_GEO_CONFIRM.value,
        DialogState.AWAITING_ADDRESS.value,
        DialogState.AWAITING_TOPIC.value,
        # Житель тапнул «📎 Дополнить», но не дописал текст. Без watchdog
        # FSM остаётся в AWAITING_FOLLOWUP_TEXT навсегда; следующее
        # «привет» через неделю уйдёт в обработчик дополнения и
        # запишется как followup случайному обращению.
        DialogState.AWAITING_FOLLOWUP_TEXT.value,
    ]
    threshold = datetime.now(timezone.utc) - timedelta(seconds=idle_seconds)
    result = await session.execute(
        select(User.max_user_id, User.dialog_state)
        .where(
            User.dialog_state.in_(pending_states),
            User.updated_at <= threshold,
            User.is_blocked.is_(False),
        )
        .limit(limit)
    )
    return [(row[0], row[1]) for row in result]


async def get_anonymous_user_id(session: AsyncSession) -> int:
    """Вернуть users.id для технической записи anonymous user.

    Запись создаётся миграцией 0007 и не должна исчезать. Если её по
    какой-то причине нет (тест, ручное вмешательство), создаём
    on-the-fly: это безопасно, потому что max_user_id=ANONYMOUS_MAX_USER_ID
    — фиксированный sentinel.

    Защита от гонки: две параллельные erase_pdn-корутины могли
    одновременно увидеть «записи нет» и попытаться INSERT, упав на
    UNIQUE(max_user_id). Перед созданием берём transactional advisory
    lock на фиксированный ключ — постгрес сам сериализует параллельные
    транзакции на этом участке, lock освобождается на commit/rollback.
    """


    anon_id = await session.scalar(
        select(User.id).where(User.max_user_id == ANONYMOUS_MAX_USER_ID)
    )
    if anon_id is not None:
        return anon_id
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": _ANONYMOUS_USER_LOCK_KEY},
    )
    # Повторное чтение под локом: соседняя транзакция могла за это время
    # создать запись, и мы должны её вернуть, а не пытаться вставить вторую.
    anon_id = await session.scalar(
        select(User.id).where(User.max_user_id == ANONYMOUS_MAX_USER_ID)
    )
    if anon_id is not None:
        return anon_id
    user = User(
        max_user_id=ANONYMOUS_MAX_USER_ID,
        first_name="Удалено",
        is_blocked=True,
    )
    session.add(user)
    await session.flush()
    return user.id


async def _redact_appeal_payloads_for_user(session: AsyncSession, user_id: int) -> tuple[int, int]:
    """Стереть свободный текст и вложения по всем обращениям жителя.

    Это отдельный шаг от удаления строки users. Без него `/erase` удалял
    имя и телефон, но оставлял ПДн в фактическом теле обращения:
    address, summary, messages.text, attachments. Для муниципальной
    статистики сохраняются только метаданные: дата, статус, тема,
    населённый пункт и факт обращения.
    """
    appeal_ids = select(Appeal.id).where(Appeal.user_id == user_id)
    appeals_result = await session.execute(
        update(Appeal)
        .where(Appeal.user_id == user_id)
        .values(address=None, summary=None, attachments=[])
    )
    messages_result = await session.execute(
        update(Message)
        .where(Message.appeal_id.in_(appeal_ids))
        .values(text=None, attachments=[])
    )
    return appeals_result.rowcount or 0, messages_result.rowcount or 0


async def erase_pdn(session: AsyncSession, max_user_id: int) -> bool:
    """Полное удаление ПДн жителя из рабочей БД.

    Wrapper для backwards-compat (возвращает bool). Новый код
    использует `erase_pdn_detailed` чтобы получить список закрытых
    обращений (для уведомления оператору).
    """
    closed_ids = await erase_pdn_detailed(session, max_user_id)
    return closed_ids is not None


async def erase_pdn_detailed(
    session: AsyncSession, max_user_id: int
) -> list[int] | None:
    """Полное удаление ПДн с возвратом списка закрытых обращений.

    Возвращает:
    - None — пользователь не найден (ничего не сделано);
    - list[int] — id обращений NEW/IN_PROGRESS, закрытых из-за erase
      (может быть пустой, если у жителя не было открытых).

    1. Селектим id NEW/IN_PROGRESS обращений ДО update — нужны для
       уведомления оператора «закрыто без ответа: #123, #456».
    2. Все NEW/IN_PROGRESS обращения этого жителя закрываются
       (`closed_due_to_revoke=true`, чтобы оператор не пытался их
       возобновить — гард доставки всё равно откажет).
    3. Свободный текст и вложения по обращениям стираются: address,
       summary, messages.text, attachments. Именно там чаще всего
       повторяются имя, телефон, адрес квартиры, фото и другие ПДн.
    4. Все обращения жителя (любого статуса) переподвешиваются на
       техническую запись «anonymous user» через UPDATE appeals.user_id.
       Так статистика количества обращений сохраняется, а связь с
       конкретным MAX-пользователем физически уходит.
    5. Запись жителя в users физически удаляется.
    """
    user_row = await session.scalar(
        select(User.id).where(User.max_user_id == max_user_id)
    )
    if user_row is None:
        return None
    # 1. Список открытых обращений ДО UPDATE — нужен для уведомления.
    open_appeals_result = await session.execute(
        select(Appeal.id).where(
            Appeal.user_id == user_row,
            Appeal.status.in_(
                [AppealStatus.NEW.value, AppealStatus.IN_PROGRESS.value]
            ),
        )
    )
    closed_ids: list[int] = [row[0] for row in open_appeals_result.all()]
    # 2. Закрыть открытые обращения с флагом closed_due_to_revoke,
    #    чтобы кнопка «🔁 Возобновить» под ними не показывалась
    #    оператору (всё равно гард доставки откажет).
    await session.execute(
        update(Appeal)
        .where(
            Appeal.user_id == user_row,
            Appeal.status.in_(
                [AppealStatus.NEW.value, AppealStatus.IN_PROGRESS.value]
            ),
        )
        .values(
            status=AppealStatus.CLOSED.value,
            closed_at=datetime.now(timezone.utc),
            closed_due_to_revoke=True,
        )
    )
    # 3. Стереть фактическое содержимое обращений/сообщений до
    #    переподвешивания на anonymous-user. Метаданные оставляем для
    #    статистики и аудита количества обращений.
    await _redact_appeal_payloads_for_user(session, user_row)
    # 4. Переподвесить ВСЕ обращения этого жителя (любого статуса) на
    #    anonymous-запись. Статистика количества обращений за период
    #    остаётся, имя/телефон жителя физически уходят.
    anonymous_id = await get_anonymous_user_id(session)
    await session.execute(
        update(Appeal)
        .where(Appeal.user_id == user_row)
        .values(user_id=anonymous_id, closed_due_to_revoke=True)
    )
    # 5. Физически удалить запись жителя.
    await session.execute(delete(User).where(User.id == user_row))
    return closed_ids


async def revoke_consent(session: AsyncSession, max_user_id: int) -> bool:
    """Отзыв согласия и выход из бота без немедленного стирания записи.

    Имя и телефон остаются в рабочей БД до /erase или 30-дневного
    retention-обезличивания. Новые обращения и рассылка прекращаются,
    но уже принятые открытые обращения остаются в работе для финального
    ответа оператора через бот.

    Что делаем:
    - consent_pdn_at = NULL, consent_revoked_at = now;
    - subscribed_broadcast = false, consent_broadcast_at = NULL
      (рассылка тоже off — текст «Подписка отключится» так обещает);
    - dialog_state = IDLE (если житель отзывал посреди воронки);
    - открытые NEW/IN_PROGRESS обращения не закрываются здесь: оператор
      отправит финальный ответ, и стандартный путь ответа закроет
      обращение.

    is_blocked НЕ ставится: житель может передумать и дать согласие
    заново через /start.
    """
    result = await session.execute(
        update(User)
        .where(User.max_user_id == max_user_id)
        .values(
            consent_pdn_at=None,
            consent_revoked_at=datetime.now(timezone.utc),
            subscribed_broadcast=False,
            consent_broadcast_at=None,
            dialog_state=DialogState.IDLE.value,
            dialog_data={},
        )
    )
    # rowcount может быть -1 в asyncpg для UPDATE без точного знания
    # числа строк. (rowcount or 0) > 0 страхует от false-positive.
    return (result.rowcount or 0) > 0


async def set_blocked(
    session: AsyncSession, max_user_id: int, *, blocked: bool
) -> bool:
    """Поднять/снять флаг is_blocked. Только для IT.

    is_blocked — это IT-блокировка за злоупотребления (бот-спам,
    оскорбления оператора, мошенничество). НЕ ставится при
    /forget — там житель просто уходит и может вернуться. Здесь
    он действительно отрезан: ответы оператора не доставляются,
    рассылки не приходят, /start показывает урезанное меню.

    При блокировке автоматически закрываем все NEW/IN_PROGRESS
    обращения этого жителя — иначе они продолжают тикать в
    SLA-просрочке и спамить алёрты в админ-чат, хотя отвечать на
    них всё равно нельзя (доставка отказывает по is_blocked).
    """


    if blocked:
        user_id = await session.scalar(
            select(User.id).where(User.max_user_id == max_user_id)
        )
        if user_id is not None:
            await session.execute(
                update(Appeal)
                .where(
                    Appeal.user_id == user_id,
                    Appeal.status.in_(
                        [AppealStatus.NEW.value, AppealStatus.IN_PROGRESS.value]
                    ),
                )
                .values(
                    status=AppealStatus.CLOSED.value,
                    closed_at=datetime.now(timezone.utc),
                    # Помечаем «закрыто из-за отзыва/блокировки», чтобы
                    # кнопка «🔁 Возобновить» под карточкой не показывалась
                    # оператору — гард доставки всё равно откажет.
                    closed_due_to_revoke=True,
                )
            )
    result = await session.execute(
        update(User)
        .where(User.max_user_id == max_user_id)
        .values(is_blocked=blocked)
    )
    return result.rowcount > 0


def _subscribers_where():
    """Условие выборки подписчиков. Вынесено чтобы count и list
    гарантированно использовали один и тот же фильтр (без рассинхрона
    «count != len(list)»)."""
    return (
        User.subscribed_broadcast.is_(True),
        User.consent_broadcast_at.isnot(None),
        User.is_blocked.is_(False),
        User.first_name != "Удалено",
    )


def _consented_where():
    return (
        User.consent_pdn_at.isnot(None),
        User.is_blocked.is_(False),
        User.first_name != "Удалено",
    )


def _blocked_where():
    return (User.is_blocked.is_(True),)


async def list_subscribers(
    session: AsyncSession, *, limit: int = 20, offset: int = 0
) -> list[User]:
    """Активные подписчики на рассылку для IT-меню «Аудитория».

    Синхронизировано с services.broadcasts._eligible_filter(): список
    показывает только тех, кому рассылка действительно может уйти.
    Старый вариант показывал legacy-подписчиков без consent_broadcast_at,
    хотя broadcast-send их уже исключал.

    `offset` поддерживается для пагинации в админ-master (PR
    audience-paginated-master).
    """
    res = await session.scalars(
        select(User)
        .where(*_subscribers_where())
        .order_by(User.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(res)


async def count_subscribers_audience(session: AsyncSession) -> int:
    """Общее число активных подписчиков для total_pages в paginated
    листинге. Отделено от `count_subscribers` в `broadcasts` чтобы
    избежать circular import и потому что фильтр чуть отличается
    (там нужно для send-loop, тут — для UI)."""
    from sqlalchemy import func as sa_func

    return await session.scalar(
        select(sa_func.count(User.id)).where(*_subscribers_where())
    ) or 0


async def list_consented(
    session: AsyncSession, *, limit: int = 20, offset: int = 0
) -> list[User]:
    """Жители с активным согласием на ПДн.

    Обезличенные sentinel/удалённые записи исключаются из операторской
    выборки, даже если в старой или тестовой БД остался consent_pdn_at.
    """
    res = await session.scalars(
        select(User)
        .where(*_consented_where())
        .order_by(User.consent_pdn_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(res)


async def count_consented(session: AsyncSession) -> int:
    """Общее число жителей с активным consent_pdn_at."""
    from sqlalchemy import func as sa_func

    return await session.scalar(
        select(sa_func.count(User.id)).where(*_consented_where())
    ) or 0


async def find_pending_pdn_retention(
    session: AsyncSession,
    *,
    days_after_revoke: int,
    limit: int = 1000,
) -> list[int]:
    """Жители, у которых нужно обезличить ПДн по сроку 152-ФЗ ст. 21 ч. 5.

    Условия отбора:
    - consent_revoked_at не NULL и старше `days_after_revoke` дней;
    - first_name ещё не «Удалено» (значит обезличивание не выполнено);
    - is_blocked != true ИЛИ phone не NULL — то есть процедура не была
      доведена до конца. Признак «обезличен» — first_name='Удалено'
      (его ставит erase_pdn).

    Открытые обращения этого жителя по 59-ФЗ должны быть обработаны до
    обезличивания: проверка делается на стороне вызывающего кода
    (в cron-job) — если обращения NEW/IN_PROGRESS остаются, жителя
    пропускаем и попробуем на следующий день.

    Возвращает max_user_id жителей под отбор. Лимит защищает от
    лавины при первом запуске после долгого простоя.
    """
    threshold = datetime.now(timezone.utc) - timedelta(days=days_after_revoke)
    res = await session.scalars(
        select(User.max_user_id)
        .where(
            User.consent_revoked_at.isnot(None),
            User.consent_revoked_at <= threshold,
            User.first_name != "Удалено",
            # Если жителю дано свежее согласие после отзыва, retention
            # не должен его обезличить. set_consent теперь обнуляет
            # consent_revoked_at, но дублируем условие здесь как
            # дополнительную защиту.
            User.consent_pdn_at.is_(None),
        )
        .limit(limit)
    )
    return list(res)


async def has_open_appeals(session: AsyncSession, user_id: int) -> bool:
    """Есть ли у жителя живые обращения (NEW/IN_PROGRESS).

    Использует таблицу appeals напрямую через select(). Нужно для
    retention-крона: жителя нельзя обезличить, пока его обращения
    в работе — это нарушит 59-ФЗ право на ответ.
    """


    row = await session.scalar(
        select(Appeal.id)
        .where(
            Appeal.user_id == user_id,
            Appeal.status.in_(
                [AppealStatus.NEW.value, AppealStatus.IN_PROGRESS.value]
            ),
        )
        .limit(1)
    )
    return row is not None


async def list_blocked(
    session: AsyncSession, *, limit: int = 20, offset: int = 0
) -> list[User]:
    """Заблокированные пользователи — после /forget или ручной блокировки IT."""
    res = await session.scalars(
        select(User)
        .where(*_blocked_where())
        .order_by(User.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(res)


async def count_blocked(session: AsyncSession) -> int:
    """Общее число заблокированных жителей."""
    from sqlalchemy import func as sa_func

    return await session.scalar(
        select(sa_func.count(User.id)).where(*_blocked_where())
    ) or 0


async def search_audience(
    session: AsyncSession, query: str, *, limit: int = 20
) -> list[User]:
    """Универсальный поиск жителя для audience-master (PR
    audience-paginated-master).

    Принимает любую из форм:
    - точный `max_user_id` (целое число от 4 цифр).
    - точный или частичный телефон (нормализуется по цифрам через
      `phone_normalized ILIKE '%digits%'`).
    - имя/часть имени (fuzzy через `first_name ILIKE '%query%'`,
      без учёта регистра, частичное совпадение).

    Возвращает до `limit` объектов User. Дубли (одна запись по
    нескольким критериям) исключаются по `User.id`. Удалённые
    жители (first_name='Удалено') не возвращаются — иначе оператор
    случайно ткнёт на pseudo-row.
    """

    if not query or not query.strip():
        return []
    raw = query.strip()
    matched_ids: set[int] = set()
    results: list[User] = []

    # 1. Точное max_user_id (4+ цифр).
    if raw.isdigit() and 4 <= len(raw) <= 12:
        try:
            mid_int = int(raw)
        except ValueError:
            mid_int = 0
        if mid_int:
            user = await find_by_max_id(session, mid_int)
            if user is not None and user.first_name != "Удалено":
                matched_ids.add(user.id)
                results.append(user)

    # 2. Имя через ILIKE (fuzzy). Cyrillic-safe: ILIKE в Postgres
    # case-insensitive через lower().
    name_pattern = f"%{raw}%"
    name_rows = (
        await session.scalars(
            select(User)
            .where(
                User.first_name.ilike(name_pattern),
                User.first_name != "Удалено",
            )
            .order_by(User.updated_at.desc())
            .limit(limit)
        )
    ).all()
    for u in name_rows:
        if u.id not in matched_ids:
            matched_ids.add(u.id)
            results.append(u)
            if len(results) >= limit:
                return results

    # 3. Phone — нормализуем цифры из query, ищем по phone_normalized
    # с substring match (поддерживает частичный телефон). Если query
    # не содержит цифр — пропускаем.
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits and len(digits) >= 3:
        # Нормализация совпадает с _normalize_phone — срезаем
        # ведущий 7/8 для 11-значных.
        normalized = digits
        if len(normalized) == 11 and normalized[0] in {"7", "8"}:
            normalized = normalized[1:]
        phone_pattern = f"%{normalized}%"
        phone_rows = (
            await session.scalars(
                select(User)
                .where(
                    User.phone_normalized.ilike(phone_pattern),
                    User.first_name != "Удалено",
                )
                .order_by(User.updated_at.desc())
                .limit(limit)
            )
        ).all()
        for u in phone_rows:
            if u.id not in matched_ids:
                matched_ids.add(u.id)
                results.append(u)
                if len(results) >= limit:
                    return results

    return results


async def find_by_phone(session: AsyncSession, phone: str) -> User | None:
    """Найти пользователя по телефону, не споткнувшись о различия в формате.

    Читает индексированную колонку `phone_normalized`, поэтому это
    O(log n) по индексу, а не полный скан таблицы с нормализацией и
    сравнением в Python-цикле. Вставки и обновления `phone` обязаны
    держать `phone_normalized` в синхроне. См. `set_phone`, обработчик
    контакта в потоке гражданина в handlers/appeal.py и заполнение
    наследных строк в миграции 0003.
    """
    target = _normalize_phone(phone)
    if not target:
        return None
    rows = (
        await session.scalars(
            select(User).where(User.phone_normalized == target).limit(2)
        )
    ).all()
    if len(rows) == 0:
        return None
    if len(rows) > 1:
        # Один номер у нескольких жителей (например, муж и жена на
        # одной симке). Возвращаем None: пусть оператор уточнит
        # `max_user_id` явно через карточку обращения. Иначе /erase
        # phone= сотрёт случайного из совпавших.
        # В лог пишем только хеш — чистый номер в логах docker
        # переживёт events-retention и попадает в log shipper'ы,
        # которые 152-ФЗ erasure не обходит.
        import hashlib
        import logging

        digest = hashlib.sha256(target.encode()).hexdigest()[:8]
        logging.getLogger(__name__).warning(
            "find_by_phone: найдено %d совпадений по phone#%s, "
            "требуется уточнение max_user_id",
            len(rows), digest,
        )
        return None
    return rows[0]


async def erase_pdn_by_phone(session: AsyncSession, phone: str) -> int | None:
    """Удалить по телефону. Возвращает max_user_id затронутой записи
    или None, если совпадения нет. Вызывающий код использует id для
    подтверждения /erase и записи в audit-лог."""
    user = await find_by_phone(session, phone)
    if user is None:
        return None
    ok = await erase_pdn(session, user.max_user_id)
    return user.max_user_id if ok else None
