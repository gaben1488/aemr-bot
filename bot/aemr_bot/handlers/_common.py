"""Общие хелперы хендлеров.

`current_user` — самый частый паттерн во всех handler-файлах: открыть
транзакцию и получить (или создать) запись жителя по его MAX user_id.
До этого хелпера ~30 мест повторяли две строки дословно::

    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=...)
        ...

Контекст-менеджер `current_user` сворачивает их в одну строку и даёт
интенту имя. Важно: он отдаёт **и сессию, и пользователя** — потому что
почти все вызовы продолжают работать с той же сессией (подписки, аудит,
списки обращений) в той же транзакции. Возврат только `user` сломал бы
границу транзакции и потребовал бы второго `session_scope`.

`expire_on_commit=False` в SessionFactory гарантирует, что атрибуты
`user` остаются доступны и после выхода из контекста — некоторые
вызовы читают `user.is_blocked` / `user.consent_pdn_at` уже за пределами
блока `async with`.

`op_screen` — тот же приём для операторского чата: ~150 мест в
`handlers/admin_*` и `handlers/broadcast_*` повторяли дословно один и
тот же пятистрочный вызов `send_or_edit_screen`.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from aemr_bot import keyboards as kbds
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import User
from aemr_bot.db.session import session_scope
from aemr_bot.services import users as users_service
from aemr_bot.utils.event import send_or_edit_screen


@asynccontextmanager
async def current_user(
    max_user_id: int, *, first_name: str | None = None
) -> AsyncIterator[tuple[AsyncSession, User]]:
    """Открыть транзакцию и получить запись жителя по `max_user_id`.

    Отдаёт кортеж ``(session, user)``: сессия остаётся открытой внутри
    блока ``async with`` для дальнейших запросов в той же транзакции,
    `user` гарантированно существует (создаётся, если записи не было).

    `first_name` прокидывается в `get_or_create` только при создании
    новой записи — для уже существующего жителя имя не перезаписывается
    (так же, как в исходном `users_service.get_or_create`).

    Пример::

        async with current_user(max_user_id) as (session, user):
            if user.is_blocked:
                return
            await broadcasts_service.set_subscription(session, max_user_id, True)
    """
    async with session_scope() as session:
        user = await users_service.get_or_create(
            session, max_user_id=max_user_id, first_name=first_name
        )
        yield session, user


async def op_screen(event: Any, text: str, kb: Any = None, *, new: bool = False):
    """Показать экран в служебной группе операторов (ADMIN_GROUP_ID).

    До этого хелпера ~150 мест в `handlers/admin_*` и
    `handlers/broadcast_*` повторяли дословно один и тот же блок::

        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=...,
            attachments=[kbds.op_back_to_menu_keyboard()],
        )

    Это ТОНКАЯ обёртка, а не новый механизм: freshness-rule «edit vs
    новое сообщение», menu_tracker и sacred-инварианты остаются целиком
    в `send_or_edit_screen` — здесь только подставляются chat_id и
    клавиатура по умолчанию.

    `kb` — одна клавиатура; по умолчанию «⬅️ В меню оператора» (самый
    частый выход из тупикового экрана). Списком передают редкий случай,
    когда экран несёт ещё и превью картинок: `[*preview_images, kb]`.

    `new=True` → `force_new_message=True`. Нужен там, где экран НЕ
    должен перетереть карточку выше (prompt ввода ответа, fallback
    после op-действия) — см. SACRED #4 в `admin_appeal_ops`.
    """
    if kb is None:
        kb = kbds.op_back_to_menu_keyboard()
    return await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=text,
        attachments=kb if isinstance(kb, list) else [kb],
        force_new_message=new,
    )


async def op_send(event: Any, text: str, kb: Any = None):
    """Отправить НОВОЕ сообщение в служебную группу операторов.

    Родственник `op_screen`, но принципиально другой примитив: здесь
    сырой `bot.send_message`, БЕЗ freshness-rule и без menu_tracker.
    Так работают потоки, где сообщение обязано лечь отдельной карточкой
    и не имеет права перетереть экран выше: валидация ввода в
    `admin_settings_*` (оператор дописывает значение сообщением, и
    ошибка должна остаться в переписке рядом с его вводом), список
    жителей по карточке на человека в `admin_audience`, ответ
    `/find_resident`, шаги мастера добавления оператора.

    Подменять его на `op_screen` НЕЛЬЗЯ — это заменило бы «отправить»
    на «отредактировать предыдущее», то есть видимое оператору
    поведение. ~24 места повторяли этот вызов дословно.

    `kb` пропускается только когда он задан: часть вызовов уходит без
    `attachments` вовсе (`/find_resident`, предупреждение о смене
    intent), и подстановка пустого списка была бы не тем же вызовом.
    """
    kwargs: dict[str, Any] = {"chat_id": cfg.admin_group_id, "text": text}
    if kb is not None:
        kwargs["attachments"] = kb if isinstance(kb, list) else [kb]
    return await event.bot.send_message(**kwargs)
