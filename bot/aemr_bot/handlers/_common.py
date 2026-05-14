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
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from aemr_bot.db.models import User
from aemr_bot.db.session import session_scope
from aemr_bot.services import users as users_service


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
