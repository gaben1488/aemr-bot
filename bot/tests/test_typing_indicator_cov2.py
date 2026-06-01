"""Покрытие непокрытой ветки utils/typing_indicator.mark_typing.

Базовый test_typing_indicator.py покрывает успех, проглатывание ошибки
send_action, извлечение chat_id из event.get_ids, no-op при bot=None и
при отсутствии chat_id. Не покрыта ветка, где chat_id не передан и
get_chat_id(event) КИДАЕТ исключение (строки 45-47) — тоже должна быть
тихим no-op без падения наружу.
"""
from __future__ import annotations

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("maxapi", reason="нужен maxapi.enums.sender_action")


@pytest.mark.asyncio
async def test_chat_id_extraction_raises_is_swallowed() -> None:
    from aemr_bot.utils.typing_indicator import mark_typing

    bot = NS(send_action=AsyncMock())
    # bot truthy, chat_id не передан → mark_typing зовёт get_chat_id(event).
    # Патчим его так, чтобы он КИНУЛ (в реале get_chat_id глушит ошибки
    # сам, поэтому имитируем худший случай напрямую) → ветка except
    # (строки 45-47): тихий no-op, send_action не вызывается.
    event = NS(bot=bot)
    with patch(
        "aemr_bot.utils.typing_indicator.get_chat_id",
        side_effect=RuntimeError("get_chat_id broke"),
    ):
        result = await mark_typing(event)
    assert result is None
    bot.send_action.assert_not_called()
