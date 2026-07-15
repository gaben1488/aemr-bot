"""Тесты на pure-helpers из main.py.

main.py импортирует maxapi на верху (Bot, Dispatcher, InvalidToken) —
без установленного maxapi импорт падает. Локально skip, в CI работает.

Покрываем:
- spawn_background_task — слабая ссылка через _BACKGROUND_TASKS
- _build_admin_senders — closures send_admin_text/send_admin_document
- _register_bot_commands — PATCH /me публикует /start, /menu
- _preflight_check_token — InvalidToken → sys.exit(1)
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Без maxapi импорт main падает на верхнем уровне.
pytest.importorskip("maxapi", reason="main.py требует установленного maxapi")


class TestSpawnBackgroundTask:
    """spawn_background_task — strong ref + автоудаление done_callback.

    Функция переехала в aemr_bot.utils.background (батч 4 polish);
    aemr_bot.main ре-экспортирует её для обратной совместимости.
    Тесты смотрят на канонический модуль utils.background.
    """

    @pytest.mark.asyncio
    async def test_task_completes_and_self_unregisters(self) -> None:
        from aemr_bot.utils import background

        async def quick_coro():
            return "done"

        task = background.spawn_background_task(quick_coro(), name="t1")
        await task
        # done_callback асинхронный, дадим event-loop'у дойти до него.
        await asyncio.sleep(0)
        assert task not in background._BACKGROUND_TASKS

    @pytest.mark.asyncio
    async def test_pending_task_still_tracked(self) -> None:
        from aemr_bot.utils import background

        ev = asyncio.Event()

        async def waiting():
            await ev.wait()

        task = background.spawn_background_task(waiting(), name="t2")
        # Ещё не завершилась.
        assert task in background._BACKGROUND_TASKS
        ev.set()
        await task
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_main_reexports_same_callable(self) -> None:
        """Регрессия: `from aemr_bot.main import spawn_background_task`
        должен продолжать работать — это исторический путь импорта."""
        from aemr_bot import main
        from aemr_bot.utils import background

        assert main.spawn_background_task is background.spawn_background_task


class TestCreateAppFactory:
    """E-3: фабрика create_app/build_bot/build_dispatcher. Поведение
    запуска должно совпадать с module-level bot/dp."""

    def test_module_level_bot_dp_built_via_factory(self) -> None:
        """module-level bot/dp существуют и несут наши свойства
        (use_create_task, наш http-таймаут < maxapi-дефолта)."""
        from aemr_bot import main

        assert main.bot is not None
        assert main.dp.use_create_task is True
        assert main.bot.default_connection.timeout.total < 150

    def test_build_dispatcher_returns_fresh_dispatcher(self) -> None:
        """build_dispatcher даёт НОВЫЙ Dispatcher с use_create_task=True
        (роутеры зарегистрированы внутри register_handlers)."""
        from aemr_bot import main

        dp = main.build_dispatcher()
        assert dp.use_create_task is True
        assert dp is not main.dp  # свежий экземпляр, не module-level

    def test_create_app_returns_bot_and_dispatcher(self) -> None:
        """create_app возвращает кортеж (Bot, Dispatcher) с теми же
        свойствами, что и module-level пара."""
        from maxapi import Bot, Dispatcher

        from aemr_bot import main

        new_bot, new_dp = main.create_app()
        assert isinstance(new_bot, Bot)
        assert isinstance(new_dp, Dispatcher)
        assert new_dp.use_create_task is True
        assert new_bot.default_connection.timeout.total == pytest.approx(
            main.settings.max_api_timeout_seconds
        )


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
    """_register_bot_commands — PATCH /me публикует /start и /menu.

    Транспорт — сессия maxapi (`bot.ensure_session()`) с доверенным
    Russian-CA коннектором, НЕ голая aiohttp.ClientSession к api2 (та
    несёт только публичные CA и молча падала бы на TLS-проверке).
    """

    @staticmethod
    def _make_session(resp: MagicMock) -> MagicMock:
        """Сессия-заглушка: `.patch(...)` — async context manager → resp."""
        resp_ctx = MagicMock()
        resp_ctx.__aenter__ = AsyncMock(return_value=resp)
        resp_ctx.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.patch = MagicMock(return_value=resp_ctx)
        return session

    @pytest.mark.asyncio
    async def test_patch_goes_through_maxapi_trusted_session(self) -> None:
        from aemr_bot import main

        resp = MagicMock()
        resp.status = 200
        resp.text = AsyncMock(return_value="ok")
        session = self._make_session(resp)

        bot = MagicMock()
        bot.ensure_session = AsyncMock(return_value=session)

        # Голую ClientSession поднимать НЕЛЬЗЯ — только доверенный
        # транспорт maxapi. Ловим любую попытку её создать.
        with patch("aiohttp.ClientSession") as raw_session_cls:
            await main._register_bot_commands(bot)

        # PATCH ушёл через сессию maxapi, а не через свою ClientSession.
        bot.ensure_session.assert_awaited_once()
        raw_session_cls.assert_not_called()

        session.patch.assert_called_once()
        call = session.patch.call_args
        # base_url сессии = api_url, путь относительный.
        assert call.args[0].endswith("/me")
        json_payload = call.kwargs.get("json")
        names = [c["name"] for c in json_payload["commands"]]
        assert names == ["start", "menu"]
        # У каждой опубликованной команды есть текст-подсказка.
        assert all(c.get("description") for c in json_payload["commands"])
        # Per-request timeout сохранён (10-секундный потолок).
        assert call.kwargs.get("timeout") is not None

    @pytest.mark.asyncio
    async def test_swallows_network_errors(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from aemr_bot import main

        # Транспорт maxapi падает (сеть/TLS) — не должно поднять наружу.
        bot = MagicMock()
        bot.ensure_session = AsyncMock(
            side_effect=RuntimeError("connection refused")
        )

        with caplog.at_level(logging.ERROR, logger="aemr_bot"):
            await main._register_bot_commands(bot)

        # Сбой проглочен именно веткой except (а не «тихо пропущен»):
        # в лог ушло сообщение об ошибке PATCH /me.
        assert any(
            "PATCH /me" in rec.message and rec.levelno >= logging.ERROR
            for rec in caplog.records
        )


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
    async def test_network_error_does_not_exit(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from aemr_bot import main

        bot = MagicMock()
        bot.get_me = AsyncMock(side_effect=ConnectionError("network down"))
        # Не должно ронять процесс — сетевая ошибка идёт как warning,
        # без sys.exit (иначе сюда долетел бы SystemExit).
        with caplog.at_level(logging.WARNING, logger="aemr_bot"):
            await main._preflight_check_token(bot)

        assert any(
            rec.levelno == logging.WARNING and "preflight" in rec.message
            for rec in caplog.records
        )
        # Подтверждаем, что НЕ ушли по ветке InvalidToken → sys.exit(1).
        assert not any(rec.levelno >= logging.ERROR for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_valid_token_logs_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from aemr_bot import main

        bot = MagicMock()
        bot.get_me = AsyncMock(
            return_value=SimpleNamespace(first_name="TestBot", user_id=42)
        )
        # Валидный токен → info с именем и id бота, без ошибок.
        with caplog.at_level(logging.INFO, logger="aemr_bot"):
            await main._preflight_check_token(bot)

        assert any(
            rec.levelno == logging.INFO
            and "TestBot" in rec.message
            and "42" in rec.message
            for rec in caplog.records
        )
