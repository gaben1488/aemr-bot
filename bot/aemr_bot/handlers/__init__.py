from maxapi import Dispatcher
from maxapi.filters.middleware import BaseMiddleware

from aemr_bot.config import settings as cfg
from aemr_bot.handlers import (
    admin_commands,
    appeal,
    broadcast,
    start,
)
from aemr_bot.services import admin_bus, idempotency


class IdempotencyMiddleware(BaseMiddleware):
    """Отбрасывает дубликаты событий до того, как они доходят до обработчиков."""

    async def __call__(self, handler, event_object, data):
        if not await idempotency.claim(event_object):
            return None
        return await handler(event_object, data)


class AdminChatActivityMiddleware(BaseMiddleware):
    """Двигает `menu_tracker[admin_group_id]` на mid входящего сообщения.

    **Зачем.** Freshness-rule в `admin_card.render` и `send_or_edit_screen`
    решает «edit vs send_new» сравнением `callback_mid == tracker`. Если
    оператор написал в admin chat текст / стикер / голос, физический чат
    сдвинулся вниз, но tracker остался на старой карточке. Любой
    следующий тап оператора на карточку выше — freshness false-positive
    «эта карточка ещё последняя» → edit поверх. Этот middleware ловит
    каждое входящее сообщение в admin chat и сдвигает tracker заранее.

    Срабатывает на любой `MessageCreated` событие в чате с `chat_id ==
    ADMIN_GROUP_ID`. Применяется ПОСЛЕ idempotency-проверки (т.е. на
    реально новые события, не дубли) и ДО handlers — чтобы commit
    последствия выполнения (например, тот же handler шлёт ответ через
    `admin_bus.send`) не перетёрли свежий incoming-mid.

    Outgoing-сообщения бота tracker'ом ведает `services/admin_bus.send`.
    """

    async def __call__(self, handler, event_object, data):
        if cfg.admin_group_id:
            try:
                from aemr_bot.utils.event import get_chat_id

                if get_chat_id(event_object) == cfg.admin_group_id:
                    # Извлекаем mid из event.message.body.mid (для
                    # MessageCreated). extract_message_id написан для
                    # ответа send_message, но структура совместима.
                    body = getattr(getattr(event_object, "message", None), "body", None)
                    mid = getattr(body, "mid", None)
                    if mid:
                        admin_bus.note_incoming_admin_message(str(mid))
            except Exception:
                # Tracker-sync не должен ломать pipeline — это best-effort.
                pass
        return await handler(event_object, data)


def _attach_outer_middleware(dp: Dispatcher, middleware: BaseMiddleware) -> None:
    """Подключить outer middleware. Полагается на публичный API maxapi 1.1+.

    Pyproject pin = `maxapi~=1.1`, поэтому 1.1.x гарантированно даёт
    `register_outer_middleware`. Если апгрейд сломал API — отказываем
    ясной ошибкой, без silent-fallback на устаревшие формы.
    """
    register = getattr(dp, "register_outer_middleware", None)
    if not callable(register):
        raise RuntimeError(
            "maxapi.Dispatcher.register_outer_middleware отсутствует — "
            "ожидается maxapi>=1.1; проверь pyproject.toml и uv sync"
        )
    register(middleware)


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
    # Порядок outer middlewares важен: idempotency первым (отбрасывает
    # дубли до tracker-sync — иначе MAX retry того же события смещал бы
    # tracker дважды). Activity middleware вторым — после dedupe двигает
    # tracker на реально новое сообщение в admin chat.
    _attach_outer_middleware(dp, IdempotencyMiddleware())
    _attach_outer_middleware(dp, AdminChatActivityMiddleware())
    start.register(dp)
    admin_commands.register(dp)
    broadcast.register(dp)
    # Catch-all последним: см. докстринг выше.
    appeal.register(dp)
