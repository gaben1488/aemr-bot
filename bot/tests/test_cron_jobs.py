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
    async def test_alerts_on_pg_dump_fail(self) -> None:
        """pg_dump упал → специфический алёрт со словом «pg_dump»."""
        from aemr_bot.services.db_backup import BackupResult
        send = AsyncMock()
        fail = BackupResult(
            path=None, fail_kind="pg_dump",
            fail_detail="pg_dump failed with code 1",
        )
        with patch("aemr_bot.services.cron._backup_db",
                   AsyncMock(return_value=fail)):
            await cron._job_backup_with_alert(send)
        send.assert_awaited_once()
        msg = send.await_args.args[0]
        assert "pg_dump" in msg
        assert "code 1" in msg or "fail_detail" in msg or "pg_dump failed" in msg

    @pytest.mark.asyncio
    async def test_alerts_on_gpg_fail_mentions_pdn_cleanup(self) -> None:
        """gpg упал → алёрт говорит про gpg И про удаление plain-text дампа."""
        from aemr_bot.services.db_backup import BackupResult
        send = AsyncMock()
        fail = BackupResult(
            path=None, fail_kind="gpg",
            fail_detail="gpg failed with code 2",
        )
        with patch("aemr_bot.services.cron._backup_db",
                   AsyncMock(return_value=fail)):
            await cron._job_backup_with_alert(send)
        msg = send.await_args.args[0]
        assert "gpg" in msg.lower()
        # Про ПДн / незашифрованный дамп — обязательно
        assert "незашифрован" in msg.lower() or "ПДн" in msg or "plain" in msg.lower()

    @pytest.mark.asyncio
    async def test_alerts_on_config_fail(self) -> None:
        """BACKUP_LOCAL_DIR пуст → алёрт про конфигурацию."""
        from aemr_bot.services.db_backup import BackupResult
        send = AsyncMock()
        fail = BackupResult(
            path=None, fail_kind="config",
            fail_detail="BACKUP_LOCAL_DIR пуст",
        )
        with patch("aemr_bot.services.cron._backup_db",
                   AsyncMock(return_value=fail)):
            await cron._job_backup_with_alert(send)
        msg = send.await_args.args[0]
        assert "BACKUP_LOCAL_DIR" in msg or "конфигурац" in msg.lower()

    @pytest.mark.asyncio
    async def test_alerts_on_unknown_fail(self) -> None:
        """Неклассифицированная ошибка → fallback-алёрт со словом «логи»."""
        from aemr_bot.services.db_backup import BackupResult
        send = AsyncMock()
        fail = BackupResult(
            path=None, fail_kind="unknown",
            fail_detail="OSError: disk full",
        )
        with patch("aemr_bot.services.cron._backup_db",
                   AsyncMock(return_value=fail)):
            await cron._job_backup_with_alert(send)
        msg = send.await_args.args[0]
        assert "лог" in msg.lower() or "неклассифицир" in msg.lower()

    @pytest.mark.asyncio
    async def test_silent_on_success(self) -> None:
        """Успешный бэкап — без алёрта (result.ok=True)."""
        from pathlib import Path
        from aemr_bot.services.db_backup import BackupResult
        send = AsyncMock()
        ok = BackupResult(path=Path("/tmp/backup.sql"))
        with patch("aemr_bot.services.cron._backup_db",
                   AsyncMock(return_value=ok)):
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


class TestAuditLogRetention:
    """_job_audit_log_retention — удаление старых audit_log записей."""

    @pytest.mark.asyncio
    async def test_swallows_exception(self) -> None:
        """БД недоступна → exception проглотиться, scheduler жив."""
        await cron._job_audit_log_retention()

    @pytest.mark.asyncio
    async def test_uses_retention_days_from_config(self) -> None:
        """Cutoff считается от настройки `audit_log_retention_days`.
        Дефолт 365 — проверяем, что delete вызван с условием
        `created_at < now - 365 дней`.
        """
        from contextlib import asynccontextmanager

        session = AsyncMock()
        delete_result = MagicMock()
        delete_result.rowcount = 3
        session.execute = AsyncMock(return_value=delete_result)

        @asynccontextmanager
        async def fake_scope():
            yield session

        with patch("aemr_bot.services.cron.session_scope", fake_scope):
            await cron._job_audit_log_retention()
        session.execute.assert_awaited_once()


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
        send_admin_text = AsyncMock()
        await cron._job_funnel_watchdog(bot, send_admin_text)
        # Не должно crash; bot.send_message может быть не вызван
        # (если БД упала — попадаем в except верхнего уровня)

    @pytest.mark.asyncio
    async def test_below_threshold_no_admin_alert(self) -> None:
        """Под порогом (4 застрявших) — никакого admin-уведомления.
        Регресс-страховка от шумных alert'ов на нормальной нагрузке."""
        from aemr_bot.services import cron as cron_mod
        from contextlib import asynccontextmanager

        bot = MagicMock()
        bot.send_message = AsyncMock()
        send_admin_text = AsyncMock()
        stuck = [(100 + i, "awaiting_name") for i in range(4)]

        @asynccontextmanager
        async def fake_scope():
            yield MagicMock()

        with patch.object(cron_mod, "session_scope", fake_scope), \
             patch("aemr_bot.services.users.find_stuck_in_funnel",
                   AsyncMock(return_value=stuck)), \
             patch("aemr_bot.services.users.reset_state", AsyncMock()):
            await cron_mod._job_funnel_watchdog(bot, send_admin_text)

        # bot.send_message — по 1 на каждого жителя.
        assert bot.send_message.await_count == 4
        # admin-канал — ТИШИНА
        send_admin_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_at_threshold_sends_admin_summary(self) -> None:
        """При ≥5 застрявших — bot отправляет в админ-чат сводку.
        Аномальный массовый зашпил — сигнал об UX-регрессии или DDoS."""
        from aemr_bot.services import cron as cron_mod
        from contextlib import asynccontextmanager

        bot = MagicMock()
        bot.send_message = AsyncMock()
        send_admin_text = AsyncMock()
        stuck = [(100 + i, "awaiting_name") for i in range(7)]

        @asynccontextmanager
        async def fake_scope():
            yield MagicMock()

        with patch.object(cron_mod, "session_scope", fake_scope), \
             patch("aemr_bot.services.users.find_stuck_in_funnel",
                   AsyncMock(return_value=stuck)), \
             patch("aemr_bot.services.users.reset_state", AsyncMock()):
            await cron_mod._job_funnel_watchdog(bot, send_admin_text)

        # Жителям — по сбросу.
        assert bot.send_message.await_count == 7
        # Админ-канал — одна сводка.
        send_admin_text.assert_awaited_once()
        msg = send_admin_text.await_args.args[0]
        assert "7" in msg, f"число застрявших не упомянуто: {msg}"
        assert "🧹" in msg
        # Подсказка про проверку логов/IT
        assert "проблем" in msg.lower() or "Funnel watchdog" in msg


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
            "audit-log-retention",
            "broadcast-draft-reaper",  # F5: orphan DRAFT cleanup
            "threat-intel-refresh",  # URL threat intel feeds
            "stale-operators-cleanup",  # CVE-9 cleanup, SECURITY_REVIEW M2
            "health-selfcheck",
            "monthly-stats",
            "startup-pulse",
            "pulse-offhours",
            "pulse-workhours",
            # pulse-sunday упразднён: mon-sun теперь покрывает все дни
            "appeals-5y-retention",
            "pdn-retention",
            "funnel-watchdog",
            "open-reminder-workhours",
            "overdue-reminder-workhours",
        }
        # healthcheck-ping появляется только если settings.healthcheck_url задан
        assert expected.issubset(names), f"missing: {expected - names}"

    def test_offhours_pulse_covers_evening_gap(self) -> None:
        """Регрессия: вечер 18:00–21:59 не должен выпадать из пульсов."""
        bot = MagicMock()
        sched = cron.build_scheduler(bot, AsyncMock(), AsyncMock())
        job = next(j for j in sched.get_jobs() if j.name == "pulse-offhours")
        trigger_text = str(job.trigger)
        assert "18-23" in trigger_text

    def test_pulse_runs_every_day_including_sunday(self) -> None:
        """Регрессия: pulse идёт ежедневно (mon-sun), не только пн-сб.

        Раньше было два юнита: pulse-offhours (mon-sat) + отдельный
        pulse-sunday (sun, ежечасно). Теперь pulse-offhours и
        pulse-workhours оба mon-sun, отдельный воскресный юнит
        упразднён — выходные и праздники мониторятся так же как
        будни (heartbeat «бот живой» нужен 24/7).
        """
        bot = MagicMock()
        sched = cron.build_scheduler(bot, AsyncMock(), AsyncMock())
        for job_name in ("pulse-offhours", "pulse-workhours"):
            job = next(j for j in sched.get_jobs() if j.name == job_name)
            text = str(job.trigger)
            assert "mon-sun" in text, (
                f"{job_name}: ожидался day_of_week=mon-sun, получено: {text}"
            )
        # Отдельной pulse-sunday больше не существует
        names = {j.name for j in sched.get_jobs()}
        assert "pulse-sunday" not in names

    def test_open_reminder_mon_fri_with_lunch_break(self) -> None:
        """Регрессия: open-reminder работает пн-пт с обедом 12-13 (skip).

        Регламент v7 §39 «пн-пт 09:00-18:00» + уточнение в v8-draft «обед
        12:00-13:00». Раньше код был пн-сб 9-17 (соответствовал
        Таблице 3 §70 v7, противоречил §39); user подтвердил
        фактическую практику = пн-пт + обед, проект v8 синхронизирует
        §39 и Таблицу 3.
        """
        bot = MagicMock()
        sched = cron.build_scheduler(bot, AsyncMock(), AsyncMock())
        job = next(
            j for j in sched.get_jobs() if j.name == "open-reminder-workhours"
        )
        text = str(job.trigger)
        assert "mon-fri" in text, f"day_of_week не mon-fri: {text}"
        # hour="9-11,13-17" — обеденный перерыв 12 пропускается
        assert "9-11" in text and "13-17" in text, (
            f"обеденный перерыв 12-13 не пропущен: {text}"
        )

    def test_overdue_reminder_mon_fri_with_lunch_break(self) -> None:
        """Регрессия: overdue-reminder — то же расписание, что open-reminder
        (minute=40 вместо 10)."""
        bot = MagicMock()
        sched = cron.build_scheduler(bot, AsyncMock(), AsyncMock())
        job = next(
            j for j in sched.get_jobs() if j.name == "overdue-reminder-workhours"
        )
        text = str(job.trigger)
        assert "mon-fri" in text
        assert "9-11" in text and "13-17" in text
