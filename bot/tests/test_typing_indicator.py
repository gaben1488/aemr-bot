"""Тесты для `utils/typing_indicator.mark_typing` — UX-помощник, который
должен быть полностью fail-safe.

Контракт:
- Любой Exception от MAX API НЕ ломает caller.
- Без chat_id (и без event'а, из которого его можно извлечь) — no-op.
- Без bot — no-op.
- Successful path — вызывает `bot.send_action(chat_id, TYPING_ON)`.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("maxapi", reason="нужен maxapi.enums.sender_action")


@pytest.mark.asyncio
async def test_mark_typing_calls_send_action_on_success() -> None:
    from aemr_bot.utils.typing_indicator import mark_typing

    bot = SimpleNamespace(send_action=AsyncMock())
    await mark_typing(bot, chat_id=555)

    bot.send_action.assert_awaited_once()
    call_kwargs = bot.send_action.call_args.kwargs
    assert call_kwargs.get("chat_id") == 555
    # action — это enum SenderAction.TYPING_ON.
    from maxapi.enums.sender_action import SenderAction
    assert call_kwargs.get("action") == SenderAction.TYPING_ON


@pytest.mark.asyncio
async def test_mark_typing_swallows_send_action_failure() -> None:
    """API упал (network / 5xx / unknown chat) — caller не должен упасть."""
    from aemr_bot.utils.typing_indicator import mark_typing

    bot = SimpleNamespace(send_action=AsyncMock(side_effect=Exception("MAX 500")))
    # Не должно бросить.
    await mark_typing(bot, chat_id=555)
    bot.send_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_mark_typing_without_chat_id_extracts_from_event() -> None:
    """Без chat_id helper берёт из event.get_ids()."""
    from aemr_bot.utils.typing_indicator import mark_typing

    bot = SimpleNamespace(send_action=AsyncMock())
    event = SimpleNamespace(
        bot=bot,
        get_ids=lambda: (777, 42),
    )
    await mark_typing(event)

    bot.send_action.assert_awaited_once()
    assert bot.send_action.call_args.kwargs.get("chat_id") == 777


@pytest.mark.asyncio
async def test_mark_typing_without_chat_id_and_no_event_is_noop() -> None:
    """Без chat_id и без get_ids на event — тихий no-op, не падаем."""
    from aemr_bot.utils.typing_indicator import mark_typing

    bot = SimpleNamespace(send_action=AsyncMock())
    # Не должно бросить, send_action не вызван.
    await mark_typing(bot)
    bot.send_action.assert_not_called()


@pytest.mark.asyncio
async def test_mark_typing_with_none_bot_is_noop() -> None:
    """Edge: bot=None / event без .bot → no-op."""
    from aemr_bot.utils.typing_indicator import mark_typing

    event = SimpleNamespace(bot=None, get_ids=lambda: (555, 7))
    # bot is None → ранний выход ДО резолва chat_id: get_chat_id (его
    # ленивый импорт берётся из aemr_bot.utils.event) не дёргается,
    # ничего не отправляется, наружу не бросает.
    with patch("aemr_bot.utils.event.get_chat_id") as get_chat_id:
        result = await mark_typing(event)
    assert result is None
    get_chat_id.assert_not_called()
