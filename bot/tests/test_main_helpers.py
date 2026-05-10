"""Тесты на pure-helpers из main.py.

main.py импортирует maxapi на верху (Bot, Dispatcher, InvalidToken) —
без установленного maxapi импорт падает. Локально skip, в CI работает.

Покрываем:
- spawn_background_task — слабая ссылка через _BACKGROUND_TASKS
- _build_admin_senders — closures send_admin_text/send_admin_document
- _register_bot_commands — PATCH /me с {"commands": []}
- _preflight_check_token — InvalidToken → sys.exit(1)
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Без maxapi импорт main падает на верхнем уровне.
pytest.importorskip("maxapi", reason="main.py требует установленного maxapi")


class TestSpawnBackgroundTask:
    """spawn_background_task — strong ref + автоудаление done_callback."""

    @pytest.mark.asyncio
    async def test_task_completes_and_self_unregisters(self) -> None:
        from aemr_bot import main

        async def quick_coro():
            return "done"

        task = main.spawn_background_task(quick_coro(), name="t1")
        await task
        # done_callback асинхронный, дадим event-loop'у дойти до него.
        await asyncio.sleep(0)
        assert task not in main._BACKGROUND_TASKS

    @pytest.mark.asyncio
    async def test_pending_task_still_tracked(self) -> None:
        from aemr_bot import main

        ev = asyncio.Event()

        async def waiting():
            await ev.wait()

        task = main.spawn_background_task(waiting(), name="t2")
        # Ещё не завершилась.
        assert task in main._BACKGROUND_TASKS
        ev.set()
        await task
        await asyncio.sleep(0)


class TestBuildAdminSenders:
    """_build_admin_senders возвращает две closures."""

    @pytest.mark.asyncio
    async def test_send_admin_text_calls_bot_send_message(self) -> None:
        from aemr_bot import main

        bot = MagicMock()
        bot.send_message = AsyncMock()
        with patch.object(main.settings, "admin_group_id", 999):
            send_text, _ = main._build_admin_senders(bot)
            await send_text("hello")
        bot.send_message.assert_called_once()
        kwargs = bot.send_message.call_args.kwargs
        assert kwargs.get("chat_id") == 999
        assert kwargs.get("text") == "hello"

    @pytest.mark.asyncio
    async def test_send_admin_text_skipped_when_no_admin_group(self) -> None:
        from aemr_bot import main

        bot = MagicMock()
        bot.send_message = AsyncMock()
        with patch.object(main.settings, "admin_group_id", None):
            send_text, _ = main._build_admin_senders(bot)
            await send_text("hello")
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_admin_document_uploads_and_attaches(self) -> None:
        from aemr_bot import main

        bot = MagicMock()
        bot.send_message = AsyncMock()
        with patch.object(main.settings, "admin_group_id", 555), \
             patch("aemr_bot.services.uploads.upload_bytes",
                   AsyncMock(return_value="tok-1")), \
             patch("aemr_bot.services.uploads.file_attachment",
                   return_value={"type": "file"}):
            _, send_doc = main._build_admin_senders(bot)
            await send_doc("report.xlsx", b"binary", caption="month report")
        bot.send_message.assert_called_once()
        kwargs = bot.send_message.call_args.kwargs
        assert kwargs.get("chat_id") == 555
        assert kwargs.get("text") == "month report"
        assert kwargs.get("attachments") is not None

    @pytest.mark.asyncio
    async def test_send_admin_document_falls_back_when_upload_fails(self) -> None:
        from aemr_bot import main

        bot = MagicMock()
        bot.send_message = AsyncMock()
        with patch.object(main.settings, "admin_group_id", 555), \
             patch("aemr_bot.services.uploads.upload_bytes",
                   AsyncMock(return_value=None)):
            _, send_doc = main._build_admin_senders(bot)
            await send_doc("r.xlsx", b"x", caption="cap")
        # Должно отправить текстовый fallback с упоминанием неудачи.
        bot.send_message.assert_called_once()
        text = bot.send_message.call_args.kwargs.get("text", "")
        assert "загрузка не удалась" in text


class TestRegisterBotCommands:
    """_register_bot_commands — PATCH /me с пустым commands."""

    @pytest.mark.asyncio
    async def test_patch_uses_authorization_header_and_empty_commands(self) -> None:
        from aemr_bot import main

        bot = MagicMock()
        bot.api_url = "https://botapi.max.ru"

        # Имитируем aiohttp.ClientSession.patch как async context manager.
        resp = MagicMock()
        resp.status = 200
        resp.text = AsyncMock(return_value="ok")
        resp_ctx = MagicMock()
        resp_ctx.__aenter__ = AsyncMock(return_value=resp)
        resp_ctx.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.patch = MagicMock(return_value=resp_ctx)
        session_ctx = MagicMock()
        session_ctx.__aenter__ = AsyncMock(return_value=session)
        session_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch.object(main.settings, "bot_token", "TOKEN-123"), \
             patch("aiohttp.ClientSession", return_value=session_ctx):
            await main._register_bot_commands(bot)

        # Проверяем сам PATCH-вызов: URL и payload.
        session.patch.assert_called_once()
        call = session.patch.call_args
        assert call.args[0].endswith("/me")
        assert call.kwargs.get("json") == {"commands": []}
        headers = call.kwargs.get("headers") or {}
        # Authorization без префикса Bearer (см. docstring).
        assert headers.get("Authorization") == "TOKEN-123"

    @pytest.mark.asyncio
    async def test_swallows_network_errors(self) -> None:
        from aemr_bot import main

        bot = MagicMock()
        bot.api_url = "https://botapi.max.ru"

        with patch.object(main.settings, "bot_token", "TOKEN"), \
             patch("aiohttp.ClientSession",
                   side_effect=RuntimeError("connection refused")):
            # Не должно поднять исключение наружу.
            await main._register_bot_commands(bot)


class TestPreflightCheckToken:
    """_preflight_check_token — sys.exit(1) при InvalidToken."""

    @pytest.mark.asyncio
    async def test_invalid_token_exits_1(self) -> None:
        from maxapi.exceptions.max import InvalidToken

        from aemr_bot import main

        bot = MagicMock()
        bot.get_me = AsyncMock(side_effect=InvalidToken("bad"))

        with pytest.raises(SystemExit) as exc:
            await main._preflight_check_token(bot)
        assert exc.value.code == 1

    @pytest.mark.asyncio
    async def test_network_error_does_not_exit(self) -> None:
        from aemr_bot import main

        bot = MagicMock()
        bot.get_me = AsyncMock(side_effect=ConnectionError("network down"))
        # Не должно ронять процесс — просто warning в лог.
        await main._preflight_check_token(bot)

    @pytest.mark.asyncio
    async def test_valid_token_logs_info(self) -> None:
        from aemr_bot import main

        bot = MagicMock()
        bot.get_me = AsyncMock(
            return_value=SimpleNamespace(first_name="TestBot", user_id=42)
        )
        # Без exception. Логируется info.
        await main._preflight_check_token(bot)
