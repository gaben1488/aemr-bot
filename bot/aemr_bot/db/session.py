from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aemr_bot.config import settings

def _engine_kwargs() -> dict:
    """Параметры создания engine. SQLite (для unit-тестов) использует
    StaticPool и не принимает pool_size/max_overflow/pool_recycle —
    отдаём только universal-параметры."""
    base: dict = {"echo": False}
    if settings.database_url.startswith("postgresql"):
        base.update(
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            # 30 минут: переподключение после Postgres failover/restart
            # либо обрыва TCP. Без этого pool отдаёт мёртвые соединения,
            # pool_pre_ping ловит, но даёт лишний RTT на каждый запрос.
            pool_recycle=1800,
            # Защита пула (5+10) от зависшего запроса. Миграция 0010
            # ставит statement_timeout через ALTER DATABASE — но это
            # покрывает только новые коннекты к БД с применённой
            # миграцией. Дублируем на уровне engine как defense-in-depth:
            #   - statement_timeout=30s — Postgres сам abort'ит SQL,
            #     висящий дольше 30 секунд (тяжёлый build_xlsx, lock
            #     contention), освобождая соединение в пул;
            #   - command_timeout=30 — asyncpg-уровень: страховка, если
            #     server-side timeout не сработал (зависла сеть до БД).
            # Без этого один медленный запрос держит соединение
            # бесконечно; пул из 15 исчерпывается — single-process бот
            # перестаёт отвечать.
            connect_args={
                "command_timeout": 30,
                "server_settings": {"statement_timeout": "30000"},
            },
        )
    return base


engine = create_async_engine(settings.database_url, **_engine_kwargs())

SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
