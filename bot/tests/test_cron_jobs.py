"""Unit-тесты на cron jobs.

После рефакторинга cron.py все jobs стали module-level coroutines с
явными зависимостями (раньше были nested closures с captured-переменными
в build_scheduler — нетестируемые без подъёма всего scheduler'а).

Тестируем:
- jobs не падают на типичных входах
- send_admin_text вызывается в нужных условиях
- selfcheck меняет состояние при смене heartbeat
- глотают исключения и не валят процесс
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# В Docker / CI этот импорт работает; локально без asyncpg — skip.
# Cron импортирует db.session, которая создаёт engine при импорте;
# без asyncpg-драйвера или с пустым DATABASE_URL — падает.
asyncpg = pytest.importorskip("asyncpg", reason="cron-тесты требуют asyncpg драйвер для импорта")

from aemr_bot.services import cron  # noqa: E402


class TestSelfcheck:
    """_job_selfcheck — алёрт при смене статуса heartbeat."""

    @pytest.mark.asyncio
    async def test_no_alert_when_status_unchanged(self) -> None:
        send = AsyncMock()
        cron._SELFCHECK_HEALTHY["healthy"] = True
        with patch("aemr_bot.health.heartbeat.is_fresh", return_value=True):
            await cron._job_selfcheck(send)
        send.assert_not_called()

    @pytest.mark.asyncio
    async def test_alert_when_becomes_unhealthy(self) -> None:
        send = AsyncMock()
        cron._SELFCHECK_HEALTHY["healthy"] = True
        with patch("aemr_bot.health.heartbeat.is_fresh", return_value=False):
            await cron._job_selfcheck(send)
        send.assert_called_once()
        text = send.call_args.args[0]
        assert "Проверка здоровья" in text
        assert "перестал отвечать" in text
        assert cron._SELFCHECK_HEALTHY["healthy"] is False

    @pytest.mark.asyncio
    async def test_recovery_when_becomes_healthy(self) -> None:
        send = AsyncMock()
        cron._SELFCHECK_HEALTHY["healthy"] = False
        with patch("aemr_bot.health.heartbeat.is_fresh", return_value=True):
            await cron._job_selfcheck(send)
        send.assert_called_once()
        text = send.call_args.args[0]
        assert "Проверка здоровья" in text
        assert "снова отвечает" in text
        assert cron._SELFCHECK_HEALTHY["healthy"] is True


class TestPulse:
    """_job_pulse — короткое подтверждение «бот жив»."""

    @pytest.mark.asyncio
    async def test_pulse_sends_text(self) -> None:
        send = AsyncMock()
        await cron._job_pulse(send)
        send.assert_called_once()
        text = send.call_args.args[0]
        assert "Пульс" in text
        assert "бот работает" in text
        assert "Время проверки" in text

    @pytest.mark.asyncio
    async def test_pulse_retries_on_transient_failure(self) -> None:
        send = AsyncMock(side_effect=[RuntimeError("network down"), None])
        with patch("asyncio.sleep", AsyncMock()) as sleep:
            await cron._job_pulse(send)
        assert send.call_count == 2
        sleep.assert_called_once_with(cron._ADMIN_SEND_RETRY_DELAYS_SEC[0])

    @pytest.mark.asyncio
    async def test_pulse_swallows_exception(self) -> None:
        """Если send упал — pulse не должен ронять scheduler-loop."""
        send = AsyncMock(side_effect=RuntimeError("network down"))
        with patch("asyncio.sleep", AsyncMock()):
            await cron._job_pulse(send)
        assert send.call_count == len(cron._ADMIN_SEND_RETRY_DELAYS_SEC) + 1


class TestStartupPulse:
    """_job_startup_pulse — catch-up хеартбит при рестарте процесса."""

    @pytest.mark.asyncio
    async def test_startup_pulse_sends_recovery_text(self) -> None:
        send = AsyncMock()
        await cron._job_startup_pulse(send)
        send.assert_called_once()
        text = send.call_args.args[0]
        # Текст должен явно отличать от обычного pulse, чтобы дежурный
        # видел «это рестарт», а не «штатный тик».
        assert "Рестарт" in text
        assert "процесс бота запущен заново" in text

    @pytest.mark.asyncio
    async def test_startup_pulse_retries_on_transient_failure(self) -> None:
        send = AsyncMock(side_effect=[RuntimeError("network down"), None])
        with patch("asyncio.sleep", AsyncMock()) as sleep:
            await cron._job_startup_pulse(send)
        assert send.call_count == 2
        sleep.assert_called_once_with(cron._ADMIN_SEND_RETRY_DELAYS_SEC[0])

    @pytest.mark.asyncio
    async def test_startup_pulse_swallows_exception(self) -> None:
        send = AsyncMock(side_effect=RuntimeError("network down"))
        # Не должно бросить — иначе scheduler не запустится.
        with patch("asyncio.sleep", AsyncMock()):
            await cron._job_startup_pulse(send)
        assert send.call_count == len(cron._ADMIN_SEND_RETRY_DELAYS_SEC) + 1


class TestBackupWithAlert:
    """_job_backup_with_alert — обёртка над _backup_db с алёртами."""

    @pytest.mark.asyncio
    async def test_alerts_on_none_result(self) -> None:
        """Если _backup_db вернул None — шлём предупреждение."""
        send = AsyncMock()
        with patch("aemr_bot.services.cron._backup_db", AsyncMock(return_value=None)):
            await cron._job_backup_with_alert(send)
        send.assert_called_once()
        assert "не выполнен" in send.call_args.args[0]

    @pytest.mark.asyncio
    async def test_silent_on_success(self) -> None:
        """Успешный бэкап — без алёрта."""
        from pathlib import Path
        send = AsyncMock()
        with patch("aemr_bot.services.cron._backup_db", AsyncMock(return_value=Path("/tmp/backup.sql"))):
            await cron._job_backup_with_alert(send)
        send.assert_not_called()

    @pytest.mark.asyncio
    async def test_alerts_on_exception(self) -> None:
        """Исключение в _backup_db → отдельный алёрт."""
        send = AsyncMock()
        with patch("aemr_bot.services.cron._backup_db", AsyncMock(side_effect=RuntimeError("disk full"))):
            await cron._job_backup_with_alert(send)
        send.assert_called_once()
        assert "исключением" in send.call_args.args[0]


class TestEventsRetention:
    """_job_events_retention — удаление старых events."""

    @pytest.mark.asyncio
    async def test_swallows_exception(self) -> None:
        """Если БД недоступна — job не должна ронять scheduler-loop."""
        # Нет реальной БД в unit-тестах → session_scope упадёт →
        # должно проглотиться try/except внутри.
        await cron._job_events_retention()


class TestPdnRetention:
    """_job_pdn_retention_check — фактическое обезличивание после отзыва согласия."""

    @pytest.mark.asyncio
    async def test_notifies_admin_after_actual_erasure(self) -> None:
        send = AsyncMock()
        session = AsyncMock()
        user = MagicMock()
        user.id = 100

        @asynccontextmanager
        async def fake_scope():
            yield session

        with patch("aemr_bot.services.cron.session_scope", fake_scope), \
             patch("aemr_bot.services.users.find_pending_pdn_retention",
                   AsyncMock(return_value=[42])), \
             patch("aemr_bot.services.users.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.services.users.has_open_appeals",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.services.users.erase_pdn",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.services.operators.write_audit", AsyncMock()):
            await cron._job_pdn_retention_check(send)

        texts = [call.args[0] for call in send.call_args_list]
        assert any("30 дней" in text and "42" in text for text in texts)


class TestFunnelWatchdog:
    """_job_funnel_watchdog — сброс зависших воронок."""

    @pytest.mark.asyncio
    async def test_swallows_exception_no_db(self) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock()
        await cron._job_funnel_watchdog(bot)
        # Не должно crash; bot.send_message может быть не вызван
        # (если БД упала — попадаем в except верхнего уровня)


class TestFormatAppealLines:
    """_format_appeal_lines — рендер строк для напоминалок."""

    def test_empty_list(self) -> None:
        assert cron._format_appeal_lines([]) == []

    def test_single_appeal(self) -> None:
        from datetime import datetime, timezone
        ap = MagicMock()
        ap.id = 42
        ap.created_at = datetime.now(timezone.utc)
        ap.locality = "Елизовское ГП"
        ap.user.first_name = "Иван"
        lines = cron._format_appeal_lines([ap])
        assert len(lines) == 1
        assert "#42" in lines[0]
        assert "Иван" in lines[0]

    def test_truncates_with_more_marker(self) -> None:
        from datetime import datetime, timezone
        appeals = []
        for i in range(15):
            ap = MagicMock()
            ap.id = i
            ap.created_at = datetime.now(timezone.utc)
            ap.locality = "X"
            ap.user.first_name = "Y"
            appeals.append(ap)
        lines = cron._format_appeal_lines(appeals, max_rows=10)
        assert len(lines) == 11  # 10 + 1 «… ещё 5»
        assert "ещё 5" in lines[-1]


class TestBuildScheduler:
    """build_scheduler — регистрация всех jobs."""

    def test_all_jobs_registered(self) -> None:
        """После рефакторинга должны быть зарегистрированы все основные jobs."""
        bot = MagicMock()
        sched = cron.build_scheduler(bot, AsyncMock(), AsyncMock())
        names = {j.name for j in sched.get_jobs()}
        expected = {
            "db-backup",
            "events-retention",
            "health-selfcheck",
            "monthly-stats",
            "startup-pulse",
            "pulse-offhours",
            "pulse-sunday",
            "pulse-workhours",
            "appeals-5y-retention",
            "pdn-retention",
            "funnel-watchdog",
            "open-reminder-workhours",
            "overdue-reminder-workhours",
        }
        # healthcheck-ping появляется только если settings.healthcheck_url задан
        assert expected.issubset(names), f"missing: {expected - names}"

    def test_offhours_pulse_covers_evening_gap(self) -> None:
        """Регрессия: пн–сб 18:00–21:59 не должны выпадать из пульсов."""
        bot = MagicMock()
        sched = cron.build_scheduler(bot, AsyncMock(), AsyncMock())
        job = next(j for j in sched.get_jobs() if j.name == "pulse-offhours")
        trigger_text = str(job.trigger)
        assert "18-23" in trigger_text
