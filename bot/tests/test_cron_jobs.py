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

import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# В Docker / CI этот импорт работает; локально без asyncpg — skip.
# Cron импортирует db.session, которая создаёт engine при импорте;
# без asyncpg-драйвера или с пустым DATABASE_URL — падает.
asyncpg = pytest.importorskip("asyncpg", reason="cron-тесты требуют asyncpg драйвер для импорта")

from datetime import UTC

from aemr_bot.services import cron


@pytest.fixture(autouse=True)
def _isolate_notification_caches():
    from aemr_bot.services import notify_toggles, quiet_hours
    notify_toggles.reset_cache_for_tests()
    quiet_hours.reset_cache_for_tests()
    yield
    notify_toggles.reset_cache_for_tests()
    quiet_hours.reset_cache_for_tests()


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
    @pytest.mark.asyncio
    async def test_pulse_sends_text_and_reserves_only_once_daily(self, session):
        send = AsyncMock(return_value="confirmed-mid")
        await cron._job_pulse(send)
        await cron._job_pulse(send)
        send.assert_awaited_once()
        assert "Пульс" in send.await_args.args[0]
        assert "Время проверки" in send.await_args.args[0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outcome", [None, TimeoutError("accepted? unknown")])
    async def test_unknown_daily_pulse_is_not_retried(self, session, outcome):
        send = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
        await cron._job_pulse(send)
        await cron._job_pulse(send)
        send.assert_awaited_once()




class TestPulseNotifyToggleGate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("enabled", [False, True])
    async def test_pulse_toggle_from_database(self, session, enabled):
        from aemr_bot.services import settings_store
        await settings_store.set_value(session, "admin_notify_pulse", enabled)
        await session.commit()
        send = AsyncMock(return_value="confirmed-mid")
        await cron._job_pulse(send)
        assert send.await_count == int(enabled)






class TestStartupPulse:
    @pytest.mark.asyncio
    async def test_startup_pulse_sends_recovery_text(self):
        send = AsyncMock(return_value="confirmed-mid")
        await cron._job_startup_pulse(send)
        send.assert_awaited_once()
        assert "Рестарт" in send.await_args.args[0]
        assert "процесс бота запущен заново" in send.await_args.args[0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outcome", [None, TimeoutError("accepted? unknown")])
    async def test_startup_unknown_result_has_one_attempt(self, outcome):
        send = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
        await cron._job_startup_pulse(send)
        send.assert_awaited_once()




class TestBackupWithAlert:
    """_job_backup_with_alert — обёртка над _backup_db с алёртами."""

    @pytest.fixture(autouse=True)
    def enable_backups(self, monkeypatch):
        monkeypatch.setattr(cron.settings, "backup_local_dir", "/backups")

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
    async def test_swallows_exception(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Если БД недоступна — job не должна ронять scheduler-loop."""
        # Детерминированно роняем session_scope, а не полагаемся на
        # отсутствие БД: исключение обязано проглотиться try/except и
        # уйти в лог (а не пробросить наружу и убить scheduler-loop).
        with caplog.at_level(logging.ERROR, logger="aemr_bot.services.cron"), \
             patch("aemr_bot.services.cron.session_scope",
                   side_effect=RuntimeError("db down")):
            result = await cron._job_events_retention()
        assert result is None
        assert any(
            "events retention failed" in rec.message for rec in caplog.records
        )


class TestAuditLogRetention:
    """_job_audit_log_retention — удаление старых audit_log записей."""

    @pytest.mark.asyncio
    async def test_swallows_exception(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """БД недоступна → exception проглотиться, scheduler жив."""
        with caplog.at_level(logging.ERROR, logger="aemr_bot.services.cron"), \
             patch("aemr_bot.services.cron.session_scope",
                   side_effect=RuntimeError("db down")):
            result = await cron._job_audit_log_retention()
        assert result is None
        assert any(
            "audit_log retention failed" in rec.message
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_two_windows_ordinary_and_erasure(self) -> None:
        """Два окна: обычные записи по `audit_log_retention_days`,
        подтверждения уничтожения — по `audit_erasure_retention_days`.

        Записи об уничтожении ПДн — доказательство исполнения обязанности
        (152-ФЗ ст. 21 ч. 5). Стирать их через год, когда предъявлять
        нужно три, — значит остаться в момент проверки ни с чем. Поэтому
        DELETE ровно два: один исключает ERASURE_ACTIONS, второй берёт
        только их.
        """

        session = AsyncMock()
        delete_result = MagicMock()
        delete_result.rowcount = 3
        session.execute = AsyncMock(return_value=delete_result)

        @asynccontextmanager
        async def fake_scope():
            yield session

        with patch("aemr_bot.services.cron.session_scope", fake_scope):
            await cron._job_audit_log_retention()

        assert session.execute.await_count == 2

    def test_erasure_actions_cover_all_destruction_paths(self) -> None:
        """ERASURE_ACTIONS перечисляет ВСЕ пути уничтожения ПДн.

        Если появится новый путь (например, массовое удаление), а в этот
        кортеж его не добавят, подтверждение будет стёрто через год
        вместо трёх — тихо, без единой ошибки. Тест фиксирует состав.
        """
        assert set(cron.ERASURE_ACTIONS) == {
            "auto_erase_pdn_retention",  # крон через 30 дней после отзыва
            "erase",  # оператор удалил данные жителя
            "self_erase",  # житель нажал /forget
            "self_consent_revoke",  # отзыв согласия — старт 30-дневного срока
        }

    def test_erasure_retention_not_shorter_than_ordinary(self) -> None:
        """Срок подтверждений не может быть короче общего срока аудита."""
        from aemr_bot.config import settings as cfg

        assert cfg.audit_erasure_retention_days >= cfg.audit_log_retention_days


class TestPdnRetention:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("has_open", [False, True])
    async def test_retention_erases_revoked_resident_even_with_open_appeal(self, session, has_open):
        from datetime import datetime, timedelta

        from sqlalchemy import select

        from aemr_bot.db.models import Appeal, AuditLog, User
        user = User(max_user_id=780001, first_name="Житель", consent_revoked_at=datetime.now(UTC)-timedelta(days=30))
        session.add(user)
        await session.flush()
        appeal = Appeal(user_id=user.id, status="new" if has_open else "answered", summary="Текст", attachments=[])
        session.add(appeal)
        await session.commit()
        send = AsyncMock()
        await cron._job_pdn_retention_check(send, warn=False)
        session.expire_all()
        assert await session.scalar(select(User).where(User.max_user_id == 780001)) is None
        audit = await session.scalar(select(AuditLog).where(AuditLog.action == "auto_erase_pdn_retention"))
        assert audit is not None and audit.target is None and audit.operator_max_user_id is None
        assert send.await_count == int(has_open)
        if has_open:
            assert "без ответа" in send.await_args.args[0]
            assert "780001" not in send.await_args.args[0]
            assert send.await_args.kwargs["critical"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("has_open", [False, True])
    async def test_deadline_warning_lists_appeals_without_resident_ids(self, session, has_open):
        from datetime import datetime, timedelta

        from aemr_bot.db.models import Appeal, User
        user = User(max_user_id=780002, consent_revoked_at=datetime.now(UTC)-timedelta(days=24))
        session.add(user)
        await session.flush()
        appeal = Appeal(user_id=user.id, status="new" if has_open else "answered", summary="Текст", attachments=[])
        session.add(appeal)
        await session.commit()
        send = AsyncMock()
        await cron._warn_about_approaching_pdn_deadline(send)
        assert send.await_count == int(has_open)
        if has_open:
            assert f"№ {appeal.id}" in send.await_args.args[0]
            assert "780002" not in send.await_args.args[0]
            assert "закрыты без ответа" in send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_vanished_candidate_does_not_create_user_or_audit(self, session):
        erase = AsyncMock()
        audit = AsyncMock()
        with (patch.object(cron.users_service, "find_pending_pdn_retention", AsyncMock(return_value=[780003])),
              patch.object(cron.users_service, "erase_pdn", erase),
              patch.object(cron.operators_service, "write_audit", audit)):
            await cron._job_pdn_retention_check(AsyncMock(), warn=False)
        erase.assert_not_awaited()
        audit.assert_not_awaited()




class TestFunnelWatchdog:
    @pytest.mark.asyncio
    async def test_database_error_is_logged_without_sending(self, caplog):
        bot = MagicMock(send_message=AsyncMock())
        send = AsyncMock()
        with (caplog.at_level(logging.ERROR, logger="aemr_bot.services.cron"),
              patch.object(cron, "session_scope", side_effect=RuntimeError("db down"))):
            await cron._job_funnel_watchdog(bot, send)
        bot.send_message.assert_not_awaited()
        send.assert_not_awaited()
        assert "funnel_watchdog crashed" in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("count", [4, 7])
    async def test_stale_users_reset_and_only_mass_stall_alerts_admin(self, session, count):
        from datetime import datetime, timedelta

        from sqlalchemy import select

        from aemr_bot.db.models import User
        for i in range(count):
            session.add(User(max_user_id=781000+i, dialog_state="awaiting_name",
                             updated_at=datetime.now(UTC)-timedelta(hours=25)))
        await session.commit()
        bot = MagicMock(send_message=AsyncMock())
        send = AsyncMock(return_value="confirmed-mid")
        await cron._job_funnel_watchdog(bot, send)
        assert bot.send_message.await_count == count
        states = list(await session.scalars(select(User.dialog_state).where(User.max_user_id >= 781000)))
        assert states == ["idle"] * count
        assert send.await_count == int(count >= 5)
        if count >= 5:
            assert f"{count}/{count}" in send.await_args.args[0]
            assert send.await_args.kwargs["critical"] is True

    @pytest.mark.asyncio
    async def test_stale_appeal_text_is_paused_instead_of_discarded(self, session):
        from datetime import datetime, timedelta

        from sqlalchemy import select

        from aemr_bot.db.models import User
        session.add(User(max_user_id=781100, dialog_state="awaiting_followup_text",
                         dialog_data={"summary_chunks": ["Сохранить текст"]},
                         updated_at=datetime.now(UTC)-timedelta(hours=25)))
        await session.commit()
        bot = MagicMock(send_message=AsyncMock())
        await cron._job_funnel_watchdog(bot, AsyncMock())
        saved = await session.scalar(select(User).where(User.max_user_id == 781100))
        assert saved.dialog_state == "idle"
        assert saved.appeal_draft["data"]["summary_chunks"] == ["Сохранить текст"]
        assert "Черновик сохранён" in bot.send_message.await_args.kwargs["text"]




class TestNotifyToggleGates:
    @pytest.mark.asyncio
    async def test_monthly_report_suppressed_when_disabled(self):
        send = AsyncMock()
        with patch("aemr_bot.services.notify_toggles.is_enabled", return_value=False):
            await cron._job_monthly_report(send)
        send.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("current_epoch", [True, False])
    async def test_monthly_report_uses_complete_export_and_erasure_guard(self, session, current_epoch):
        from aemr_bot.services import complete_stats, privacy_epoch
        send = AsyncMock()
        with (patch("aemr_bot.services.notify_toggles.is_enabled", return_value=True),
              patch.object(complete_stats, "build_xlsx_with_epoch", AsyncMock(return_value=(b"xlsx-bytes", "за месяц", 5, 42))) as build,
              patch.object(privacy_epoch, "hold_export_if_current", AsyncMock(return_value=current_epoch)) as guard):
            await cron._job_monthly_report(send)
        assert build.await_args.args[1] == "month"
        start, end, _ = build.await_args.kwargs["window"]
        assert start < end and start.tzinfo is not None and end.tzinfo is not None
        assert guard.await_args.args[1] == 42
        assert send.await_count == int(current_epoch)
        if current_epoch:
            assert send.await_args.kwargs["content"] == b"xlsx-bytes"
            assert "5 обращений" in send.await_args.kwargs["caption"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("job_name,toggle", [
        ("_job_working_hours_open_reminder", "admin_notify_open_reminder"),
        ("_job_working_hours_overdue_reminder", "admin_notify_overdue_reminder"),
    ])
    @pytest.mark.parametrize("case", ["working", "quiet", "disabled", "lunch", "weekend", "holiday", "before_open", "after_close"])
    async def test_reminder_uses_live_database_schedule_and_notification_gates(self, session, monkeypatch, job_name, toggle, case):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from aemr_bot.services import (
            notify_toggles,
            queue_panel,
            quiet_hours,
            settings_store,
            work_schedule,
        )
        instant = datetime(2026, 6, 20 if case == "weekend" else 16,
                           {"lunch": 12, "before_open": 7, "after_close": 19}.get(case, 8),
                           30, tzinfo=ZoneInfo("Asia/Kamchatka"))
        class FixedDate(datetime):
            @classmethod
            def now(cls, tz=None):
                return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
        monkeypatch.setattr(cron, "datetime", FixedDate)
        monkeypatch.setattr(quiet_hours, "datetime", FixedDate)
        value = dict(work_schedule.environment(), start=8, end=19,
                     holidays=["2026-06-16"] if case == "holiday" else [])
        await settings_store.set_value(session, work_schedule.KEY, value)
        await settings_store.set_value(session, toggle, case != "disabled")
        await settings_store.set_value(session, "admin_quiet_hours_enabled", case == "quiet")
        await settings_store.set_value(session, "admin_quiet_hours_start", 0)
        await settings_store.set_value(session, "admin_quiet_hours_end", 23)
        await session.commit()
        await notify_toggles.refresh_cache_from_db(session)
        await quiet_hours.refresh_cache_from_db(session)
        with patch.object(queue_panel, "refresh", AsyncMock()) as refresh:
            await getattr(cron, job_name)(object())
        assert refresh.await_count == int(case == "working")
        if case not in {"quiet", "disabled"}:
            assert work_schedule.active()["start"] == 8




class TestFormatAppealLines:
    """_format_appeal_lines — рендер строк для напоминалок."""

    def test_empty_list(self) -> None:
        assert cron._format_appeal_lines([]) == []

    def test_single_appeal(self) -> None:
        from datetime import datetime
        ap = MagicMock()
        ap.id = 42
        ap.sla_schedule_at_start = None
        ap.sla_anchor_at = None
        ap.created_at = datetime.now(UTC)
        ap.locality = "Елизовское ГП"
        ap.user.first_name = "Иван"
        lines = cron._format_appeal_lines([ap])
        assert len(lines) == 1
        assert "#42" in lines[0]
        assert "Иван" in lines[0]

    def test_truncates_with_more_marker(self) -> None:
        from datetime import datetime
        appeals = []
        for i in range(15):
            ap = MagicMock()
            ap.id = i
            ap.sla_schedule_at_start = None
            ap.sla_anchor_at = None
            ap.created_at = datetime.now(UTC)
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
            "pulse-hourly",  # 24/7 каждый час на :05
            "pulse-workhours-extra",  # пн-пт 9-17 доп. на :35
            "appeals-5y-retention",
            "pdn-retention",
            "funnel-watchdog",
            "open-reminder-workhours",
            "overdue-reminder-workhours",
        }
        # healthcheck-ping появляется только если settings.healthcheck_url задан
        assert expected.issubset(names), f"missing: {expected - names}"

    def test_pulse_hourly_covers_all_hours_and_days(self) -> None:
        """Регрессия: базовый `pulse-hourly` идёт каждый час 24/7.

        Раньше было два пересекающихся юнита (`pulse-offhours` для
        часов 0-8,18-23 и `pulse-workhours` для 9-17) — путаное
        разделение, плюс в субботу-воскресенье 9-17 ошибочно срабатывал
        workhours-юнит как будто рабочий день. Теперь один базовый
        24/7 юнит даёт ровно один сигнал в час всегда, включая ночь,
        выходные и праздники.
        """
        bot = MagicMock()
        sched = cron.build_scheduler(bot, AsyncMock(), AsyncMock())
        job = next(j for j in sched.get_jobs() if j.name == "pulse-hourly")
        trigger_text = str(job.trigger)
        # Должно быть без ограничений по часам или дням недели —
        # только минута :05.
        assert "hour" not in trigger_text or "hour='*'" in trigger_text
        assert "minute='5'" in trigger_text or "minute=5" in trigger_text.replace("'", "")

    def test_pulse_workhours_extra_only_mon_fri_9_17(self) -> None:
        """Регрессия: доп-сигнал в рабочее время ТОЛЬКО пн-пт 9-17.

        В выходные (сб-вс) ничего дополнительного — только базовый
        `pulse-hourly`. Юзер указал что выходные не должны получать
        «рабочую» плотность мониторинга.
        """
        bot = MagicMock()
        sched = cron.build_scheduler(bot, AsyncMock(), AsyncMock())
        job = next(j for j in sched.get_jobs() if j.name == "pulse-workhours-extra")
        trigger_text = str(job.trigger)
        assert "mon-fri" in trigger_text
        assert "9-17" in trigger_text
        assert "35" in trigger_text

    def test_old_pulse_jobs_dont_exist(self) -> None:
        """Старые имена `pulse-offhours` / `pulse-workhours` /
        `pulse-sunday` упразднены — заменены на `pulse-hourly` +
        `pulse-workhours-extra`."""
        bot = MagicMock()
        sched = cron.build_scheduler(bot, AsyncMock(), AsyncMock())
        names = {j.name for j in sched.get_jobs()}
        for old in ("pulse-offhours", "pulse-workhours", "pulse-sunday"):
            assert old not in names, f"старый job {old!r} должен быть упразднён"

    def test_open_reminder_trigger_reaches_changed_working_window(self):
        from datetime import datetime
        sched = cron.build_scheduler(MagicMock(), AsyncMock(), AsyncMock())
        job = next(j for j in sched.get_jobs() if j.name == "open-reminder-workhours")
        # Even Saturday and an hour outside the former 9–18 window must
        # tick: the live DB schedule/calendar decides whether to refresh.
        instant = datetime(2026, 6, 20, 7, 0, tzinfo=cron.TZ)
        fire = job.trigger.get_next_fire_time(None, instant)
        assert fire == instant.replace(minute=10)

    def test_overdue_reminder_trigger_reaches_changed_working_window(self):
        from datetime import datetime
        sched = cron.build_scheduler(MagicMock(), AsyncMock(), AsyncMock())
        job = next(j for j in sched.get_jobs() if j.name == "overdue-reminder-workhours")
        # Even Saturday and an hour outside the former 9–18 window must
        # tick: the live DB schedule/calendar decides whether to refresh.
        instant = datetime(2026, 6, 20, 7, 0, tzinfo=cron.TZ)
        fire = job.trigger.get_next_fire_time(None, instant)
        assert fire == instant.replace(minute=40)


class TestThreatIntelRefresh:
    """_job_threat_intel_refresh — обновление базы угроз + critical-алёрт,
    если данные устарели (источники недоступны >6 ч)."""

    @pytest.mark.asyncio
    async def test_stale_sends_critical_alert(self) -> None:
        from types import SimpleNamespace

        send = AsyncMock()
        stale_store = SimpleNamespace(
            is_stale=lambda: True,
            staleness_age_seconds=lambda: 7 * 3600,
        )
        with patch(
            "aemr_bot.services.threat_intel.refresh_all",
            AsyncMock(return_value={}),
        ), patch(
            "aemr_bot.services.threat_intel.get_store",
            return_value=stale_store,
        ), patch("asyncio.sleep", AsyncMock()):
            await cron._job_threat_intel_refresh(send)
        send.assert_awaited()
        assert send.await_args.kwargs.get("critical") is True
        assert "не обновлялась" in send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_fresh_no_alert(self) -> None:
        from types import SimpleNamespace

        send = AsyncMock()
        fresh_store = SimpleNamespace(
            is_stale=lambda: False,
            staleness_age_seconds=lambda: 100.0,
        )
        with patch(
            "aemr_bot.services.threat_intel.refresh_all",
            AsyncMock(return_value={"URLhaus": 10}),
        ), patch(
            "aemr_bot.services.threat_intel.get_store",
            return_value=fresh_store,
        ):
            await cron._job_threat_intel_refresh(send)
        send.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_refresh_keeps_period_calendars_after_live_schedule_change(session, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from aemr_bot.db.models import User
    from aemr_bot.services import appeals, queue_panel, settings_store, work_schedule

    tz = ZoneInfo("Asia/Kamchatka")
    instant = datetime(2026, 6, 16, 10, 30, tzinfo=tz)
    anchor = instant.replace(hour=9)
    class FixedDate(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
    monkeypatch.setattr(queue_panel, "datetime", FixedDate)
    user = User(max_user_id=782000)
    session.add(user)
    await session.flush()
    old = dict(work_schedule.environment(), start=9, end=18, holidays=[])
    await settings_store.set_value(session, work_schedule.KEY, old)
    first = await appeals.create_appeal(session, user, "Адрес", "Дорога", "Первое", [])
    first.created_at = anchor
    first.sla_hours_at_start = 1
    await settings_store.set_value(session, work_schedule.KEY, dict(old, start=10))
    second = await appeals.create_appeal(session, user, "Адрес", "Дорога", "Второе", [])
    second.created_at = anchor
    second.sla_hours_at_start = 1
    await session.commit()
    await work_schedule.refresh(session)
    text, nonempty = await queue_panel._snapshot()
    assert nonempty
    assert f"⚠️ Просрочен первый ответ: 1\n#{first.id}" in text
    assert f"🆕 Ожидают первого ответа: 1\n#{second.id}" in text
