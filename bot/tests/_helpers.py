"""Общие фабрики тестовых событий и session-scope.

До этого модуля `_make_event` / `_make_callback_event` /
`_fake_session_scope` были скопированы в ~14 тест-файлов, каждый со
слегка своей сигнатурой. Любое изменение формы MAX-события требовало
правки в 14 местах — тесты тормозили рефакторинг вместо того, чтобы
его защищать (отмечено swarm code-review).

Здесь — один источник правды для СТРУКТУРЫ события. Все 12 handler-
тест-файлов мигрированы, включая `test_handlers_funnel.py` (он держал
свой `bot=AsyncMock()` — структурно покрывается `with_edit_message=True`,
оба `bot.send_message`/`bot.edit_message` остаются awaitable).

Файловые дефолты (`chat_id`, `user_id` отличаются между файлами и
иногда важны для сверки с `cfg.admin_group_id`) сохраняются: тест-файл
оставляет тонкий `_make_event`-адаптер, который зовёт `make_event(...)`
с нужными дефолтами. Это не дублирование — адаптер не содержит
структуры события, только параметры; вся структура здесь. Полное
удаление адаптеров потребовало бы переписать сотни вызовов
`_make_event(...)` в телах тестов на явные `make_event(...)` — большой
механический diff без дополнительной ценности, т.к. централизация
структуры уже достигнута.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


def make_event(
    *,
    chat_id: int = 555,
    user_id: int = 7,
    text: str = "",
    first_name: str | None = None,
    mid: str = "m-1",
    with_callback: bool = False,
    with_user: bool = False,
    send_returns_mid: bool = False,
    with_edit_message: bool = False,
) -> SimpleNamespace:
    """Фабрика MAX-события для handler-тестов. Суперсет всех вариаций.

    - `with_callback` — добавить `event.callback` (SimpleNamespace с
      callback_id) для callback-handler'ов.
    - `with_user` — добавить `event.user` (некоторые handler'ы читают
      его, а не `event.message.sender`).
    - `send_returns_mid` — `bot.send_message` вернёт SendedMessage-like
      с `message.body.mid`; нужно для `extract_message_id` в
      progress/broadcast-флоу.
    - `with_edit_message` — добавить `bot.edit_message` как AsyncMock.
    - `first_name` — если задан, проставляется в `sender` (и в `user`,
      если `with_user`).
    """
    bot = MagicMock()
    if send_returns_mid:
        bot.send_message = AsyncMock(
            return_value=SimpleNamespace(
                message=SimpleNamespace(body=SimpleNamespace(mid="m-progress"))
            )
        )
    else:
        bot.send_message = AsyncMock()
    if with_edit_message:
        bot.edit_message = AsyncMock()

    sender_kwargs: dict = {"user_id": user_id}
    if first_name is not None:
        sender_kwargs["first_name"] = first_name

    event_kwargs: dict = {
        "bot": bot,
        "message": SimpleNamespace(
            answer=AsyncMock(),
            sender=SimpleNamespace(**sender_kwargs),
            recipient=SimpleNamespace(chat_id=chat_id),
            body=SimpleNamespace(text=text, attachments=[], mid=mid),
        ),
    }
    if with_callback:
        event_kwargs["callback"] = SimpleNamespace(callback_id="cb-1")
    if with_user:
        event_kwargs["user"] = SimpleNamespace(**sender_kwargs)
    return SimpleNamespace(**event_kwargs)


def make_callback_event(
    *,
    chat_id: int = 555,
    user_id: int = 7,
    payload: str = "",
    first_name: str | None = None,
) -> SimpleNamespace:
    """MAX callback-событие. `callback.payload` — нажатый payload,
    `callback.callback_id` — id для ack."""
    event = make_event(
        chat_id=chat_id,
        user_id=user_id,
        first_name=first_name,
        with_callback=True,
    )
    event.callback.payload = payload
    return event


@asynccontextmanager
async def fake_session_scope():
    """Заглушка `session_scope()` — отдаёт MagicMock вместо реальной
    сессии. Для handler-тестов, где БД-вызовы и так замоканы."""
    yield MagicMock()


def fake_current_user(user, *, session=None):
    """Заглушка `handlers._common.current_user` для handler-тестов.

    Возвращает CM-фабрику, которая yield'ит пару ``(session, user)`` —
    ту же форму, что и боевой `current_user`. Заменяет связку из двух
    патчей (`session_scope` + `users_service.get_or_create`) одним.

    `session` по умолчанию — MagicMock; передайте AsyncMock, если тест
    проверяет прямые вызовы `session.execute` / `session.add`.
    """
    sess = MagicMock() if session is None else session

    @asynccontextmanager
    async def _cm(max_user_id, *, first_name=None):
        yield sess, user

    return _cm
