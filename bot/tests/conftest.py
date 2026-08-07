import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings

os.environ.setdefault("BOT_TOKEN", "test-token")
# Pseudo-URL по умолчанию: pure-тесты импортируют модули с engine-
# on-import (db/session.py, services/idempotency.py) без падения.
# Используем sqlite+aiosqlite — aiosqlite есть в dev-deps локально и
# не требует asyncpg. Реальный DATABASE_URL ставится через CI env или
# локальной переменной до запуска — и перебивает этот default через
# setdefault-семантику.
_PSEUDO_DB = "sqlite+aiosqlite:///:memory:"
os.environ.setdefault("DATABASE_URL", _PSEUDO_DB)
os.environ.setdefault("ADMIN_GROUP_ID", "123")

DATABASE_URL = os.environ["DATABASE_URL"]
# Pure-юнит-тесты не требуют Postgres. Маркер «реальная БД» — postgresql
# в URL (модели используют JSONB, sqlite не подойдёт).
_HAS_REAL_DB = DATABASE_URL.startswith("postgresql")

# Hypothesis: снять дедлайн на один пример (по умолчанию 200 мс).
# Property-тесты (tests/test_validators_hypothesis.py) проверяют ЛОГИКУ
# валидаторов, а не их скорость. Дефолтный дедлайн делал их флаки: в
# полном прогоне (2700+ тестов, загруженная машина, медленный раннер CI)
# отдельный пример не укладывался в 200 мс и тест падал с
# DeadlineExceeded — при этом в одиночном прогоне проходил. Падение,
# которое зависит от нагрузки машины, а не от кода, — это шум, который
# приучает не верить красному CI. Скорость валидаторов, если понадобится,
# меряется отдельными perf-тестами.
hypothesis_settings.register_profile(
    "aemr", deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
hypothesis_settings.load_profile("aemr")


@pytest_asyncio.fixture(autouse=True)
async def _no_async_leaks_between_tests() -> AsyncIterator[None]:
    """Не выпускать фоновые задачи и соединения псевдо-БД за границу теста.

    Диагноз (2026-08-07): полный прогон намертво вставал около 79 %
    (`tests/test_sec_broadcast.py`), при этом любая партия файлов
    проходила за секунды. Механизм:

    1. Хендлеры дёргают `wizard_registry.schedule_persist_*`, тот через
       `spawn_background_task` заводит НАСТОЯЩУЮ фоновую запись в БД.
       Тест мокает сам хендлер, но задачу не ждёт — она переживает тест.
    2. Псевдо-БД юнит-тестов — `sqlite+aiosqlite:///:memory:`, а это
       StaticPool: одно соединение на весь процесс. У aiosqlite соединение
       обслуживает поток, который отдаёт результат через
       `future.get_loop().call_soon_threadsafe(...)`.
    3. pytest-asyncio даёт каждому тесту СВОЙ event loop. Когда цикл
       первого теста закрывается, поток соединения падает с
       `RuntimeError: Event loop is closed` (это и есть россыпь
       PytestUnhandledThreadExceptionWarning в отчёте) — и умирает.
    4. Следующий тест берёт из пула то же мёртвое соединение. Фоновая
       задача уходит в await на future, который резолвить уже некому.
       На закрытии цикла `asyncio.Runner.close` отменяет задачи и ждёт
       их в `run_until_complete` — а задача при отмене идёт в
       `session_scope.__aexit__` и снова ждёт мёртвый поток. Прогон
       встаёт навсегда.

    Фикстура закрывает обе течи в правильном порядке: сначала гасит
    фоновые задачи, пока их цикл и поток соединения ещё живы, затем
    рассыпает engine — тогда следующий тест получает свежее соединение
    в своём цикле. Прод не трогаем: там цикл ровно один и Postgres, а
    не sqlite.
    """
    yield

    import asyncio

    from aemr_bot.utils.background import _BACKGROUND_TASKS

    leaked = [t for t in list(_BACKGROUND_TASKS) if not t.done()]
    if leaked:
        # Сначала короткая отсрочка на естественное завершение: persist в
        # псевдо-БД падает на первом же запросе («no such table») и
        # укладывается в один тик. Отменять сразу — значит бросить в
        # потоке aiosqlite запрос, чей future уже некому резолвить: поток
        # доедет до call_soon_threadsafe на закрытом цикле и выдаст
        # PytestUnhandledThreadExceptionWarning.
        _, leaked_still = await asyncio.wait(leaked, timeout=1.0)
        for task in leaked_still:
            task.cancel()
        # Ждём с потолком: если задача всё-таки зависла на мёртвом
        # соединении, тест закончится с предупреждением, а не повесит
        # весь прогон.
        if leaked_still:
            await asyncio.wait(leaked_still, timeout=5)

    if DATABASE_URL.startswith("sqlite"):
        from aemr_bot.db import session as db_session

        await db_session.engine.dispose()


@pytest_asyncio.fixture
async def session() -> AsyncIterator:
    if not _HAS_REAL_DB or DATABASE_URL.startswith("sqlite"):
        pytest.skip(
            "Test requires PostgreSQL (models use JSONB). "
            "Set DATABASE_URL=postgresql+asyncpg://... before running pytest."
        )
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from aemr_bot.db.models import Base

    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
        await s.rollback()
    await engine.dispose()
