from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aemr_bot.db.models import DialogState, User


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


async def has_consent(session: AsyncSession, max_user_id: int) -> bool:
    user = await session.scalar(select(User).where(User.max_user_id == max_user_id))
    return bool(user and user.consent_pdn_at)


async def set_consent(session: AsyncSession, max_user_id: int) -> None:
    # Заодно снимаем is_blocked: житель мог раньше воспользоваться
    # /forget, что выставляет is_blocked=true. Если он вернулся,
    # дал согласие заново — это явное «свяжитесь со мной снова»,
    # блокировка устаревает. Без этого сброса оператор увидит
    # «Не могу доставить ответ — житель отозвал согласие», хотя
    # на самом деле согласие свежее.
    await session.execute(
        update(User)
        .where(User.max_user_id == max_user_id)
        .values(
            consent_pdn_at=datetime.now(timezone.utc),
            is_blocked=False,
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
    await session.execute(
        update(User).where(User.max_user_id == max_user_id).values(first_name=first_name)
    )


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
        from aemr_bot.config import settings as cfg
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
        from aemr_bot.config import settings as cfg
        limit = cfg.recover_batch_size
    pending_states = [
        DialogState.AWAITING_CONSENT.value,
        DialogState.AWAITING_CONTACT.value,
        DialogState.AWAITING_NAME.value,
        DialogState.AWAITING_LOCALITY.value,
        DialogState.AWAITING_ADDRESS.value,
        DialogState.AWAITING_TOPIC.value,
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


async def erase_pdn(session: AsyncSession, max_user_id: int) -> bool:
    """Обезличить пользователя и отозвать согласие ПДн (152-ФЗ, ст. 9 §2).

    Отзыв согласия снимает подписку на рассылку и поднимает is_blocked,
    чтобы пользователь не попадал в выборку подписчиков. Иначе любая
    последующая отправка ему была бы обработкой данных без согласия.
    """
    result = await session.execute(
        update(User)
        .where(User.max_user_id == max_user_id)
        .values(
            first_name="Удалено",
            phone=None,
            phone_normalized=None,
            consent_pdn_at=None,
            consent_revoked_at=datetime.now(timezone.utc),
            dialog_state=DialogState.IDLE.value,
            dialog_data={},
            subscribed_broadcast=False,
            is_blocked=True,
        )
    )
    return result.rowcount > 0


async def revoke_consent(session: AsyncSession, max_user_id: int) -> bool:
    """Мягкий отзыв согласия: сохраняем имя/телефон и историю обращений,
    но помечаем согласие отозванным.

    В отличие от `erase_pdn`, это не «удаление меня из системы», а
    «прекратите использовать мои данные для новых обращений и рассылок».
    Сценарий: житель не хочет получать рассылку, не хочет писать новые
    обращения, но не возражает, чтобы оператор закрыл уже открытые.

    Что делаем: consent_pdn_at=NULL, consent_revoked_at=now,
    subscribed_broadcast=false, dialog_state=IDLE (на случай, если
    житель отзывал прямо посреди воронки). is_blocked НЕ ставим —
    доставка ответов оператора по уже открытым обращениям должна
    продолжать работать (право на ответ по 59-ФЗ).
    """
    result = await session.execute(
        update(User)
        .where(User.max_user_id == max_user_id)
        .values(
            consent_pdn_at=None,
            consent_revoked_at=datetime.now(timezone.utc),
            subscribed_broadcast=False,
            dialog_state=DialogState.IDLE.value,
            dialog_data={},
        )
    )
    return result.rowcount > 0


async def set_blocked(
    session: AsyncSession, max_user_id: int, *, blocked: bool
) -> bool:
    """Поднять/снять флаг is_blocked.

    Используется кнопкой «🚫 Заблокировать жителя» в карточке обращения
    и админ-меню «Подписчики и согласия». Заблокированному пользователю
    бот не доставляет ничего: ни ответы оператора, ни рассылки.
    Если житель потом снова напишет /start — гард в обработчиках
    проверит is_blocked и не пустит дальше.
    """
    result = await session.execute(
        update(User)
        .where(User.max_user_id == max_user_id)
        .values(is_blocked=blocked)
    )
    return result.rowcount > 0


async def list_subscribers(session: AsyncSession, *, limit: int = 20) -> list[User]:
    """Активные подписчики на рассылку. Используется в IT-меню «Аудитория»."""
    res = await session.scalars(
        select(User)
        .where(User.subscribed_broadcast.is_(True), User.is_blocked.is_(False))
        .order_by(User.updated_at.desc())
        .limit(limit)
    )
    return list(res)


async def list_consented(session: AsyncSession, *, limit: int = 20) -> list[User]:
    """Жители с активным согласием на ПДн. Это все, кто проходил воронку
    хотя бы раз и не отзывал согласие."""
    res = await session.scalars(
        select(User)
        .where(User.consent_pdn_at.isnot(None))
        .order_by(User.consent_pdn_at.desc())
        .limit(limit)
    )
    return list(res)


async def list_blocked(session: AsyncSession, *, limit: int = 20) -> list[User]:
    """Заблокированные пользователи — после /forget или ручной блокировки IT."""
    res = await session.scalars(
        select(User)
        .where(User.is_blocked.is_(True))
        .order_by(User.updated_at.desc())
        .limit(limit)
    )
    return list(res)


async def can_post_new_appeal(session: AsyncSession, max_user_id: int) -> bool:
    """Можно ли жителю подать НОВОЕ обращение прямо сейчас.

    Условия: согласие активно (consent_pdn_at не NULL) и не отозвано;
    бот не заблокировал пользователя. Если согласие никогда не давалось,
    результат тоже False — воронка попросит дать согласие в первом шаге.
    """
    user = await session.scalar(select(User).where(User.max_user_id == max_user_id))
    if user is None:
        return False
    if user.is_blocked:
        return False
    return user.consent_pdn_at is not None


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
        import logging

        logging.getLogger(__name__).warning(
            "find_by_phone: найдено %d совпадений по %s, требуется "
            "уточнение max_user_id",
            len(rows), target,
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
