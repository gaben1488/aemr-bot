from maxapi import Dispatcher
from maxapi.filters.middleware import BaseMiddleware

from aemr_bot.handlers import (
    admin_commands,
    appeal,
    broadcast,
    start,
)
from aemr_bot.services import idempotency


class IdempotencyMiddleware(BaseMiddleware):
    """Отбрасывает дубликаты событий до того, как они доходят до обработчиков."""

    async def __call__(self, handler, event_object, data):
        if not await idempotency.claim(event_object):
            return None
        return await handler(event_object, data)


def _attach_outer_middleware(dp: Dispatcher, middleware: BaseMiddleware) -> None:
    """Подключить промежуточный слой как внешний в разных версиях maxapi.

    Форма меняется от выпуска к выпуску: 0.9.18+ предоставляет вызываемый
    метод `outer_middleware(mw)`; в HEAD есть список `outer_middlewares`;
    в более ранних 0.9.0–0.9.17 был только `middlewares`, где «внешний»
    означает вставку в начало списка.
    """
    add = getattr(dp, "outer_middleware", None)
    if callable(add):
        add(middleware)
        return
    bucket = getattr(dp, "outer_middlewares", None)
    if isinstance(bucket, list):
        bucket.append(middleware)
        return
    bucket = getattr(dp, "middlewares", None)
    if isinstance(bucket, list):
        bucket.insert(0, middleware)
        return
    raise RuntimeError(
        "у maxapi.Dispatcher нет точки подключения middleware — проверьте установленную версию"
    )


def register_handlers(dp: Dispatcher) -> None:
    """Регистрирует обработчики в порядке: команды первыми, catch-all последним.

    `appeal.register` ставит `@dp.message_created()` без фильтров — это
    catch-all-маршрутизатор анкеты для жителя. maxapi обрабатывает
    обработчики одного и того же update_type в порядке регистрации и
    останавливается на первом подошедшем. Поэтому catch-all обязан идти
    ПОСЛЕ каждого обработчика с фильтром Command, иначе он молча проглотит
    /stats, /reopen, /broadcast и прочее ещё до того, как они дойдут до
    своих декораторов.

    `start.register` спокойно ставится первым, потому что у всех его
    обработчиков ЕСТЬ фильтр-команда. Нажатия меню и ответы операторов
    маршрутизируются из `appeal.on_callback` / `appeal.on_message`, поэтому
    отдельные register-заглушки для них не нужны.
    """
    _attach_outer_middleware(dp, IdempotencyMiddleware())
    start.register(dp)
    admin_commands.register(dp)
    broadcast.register(dp)
    # Catch-all последним: см. докстринг выше.
    appeal.register(dp)
