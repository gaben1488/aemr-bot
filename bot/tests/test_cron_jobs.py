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


class TestPulseNotifyToggleGate:
    """_job_pulse: гейт `admin_notify_pulse` (services/notify_toggles.py) —
    независимый от quiet hours, действует круглосуточно."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.services import notify_toggles
        notify_toggles.reset_cache_for_tests()
        yield
        notify_toggles.reset_cache_for_tests()

    @pytest.mark.asyncio
    async def test_pulse_suppressed_when_toggle_disabled(self) -> None:
        send = AsyncMock()
        fake_scope = _make_fake_session_scope()
        with patch("aemr_bot.services.cron.session_scope", fake_scope), \
             patch(
                 "aemr_bot.services.quiet_hours.refresh_cache_from_db",
                 AsyncMock(),
             ), \
             patch(
                 "aemr_bot.services.notify_toggles.refresh_cache_from_db",
                 AsyncMock(),
             ), \
             patch(
                 "aemr_bot.services.notify_toggles.is_enabled",
                 side_effect=lambda key: key != "admin_notify_pulse",
             ):
            await cron._job_pulse(send)
        send.assert_not_called()

    @pytest.mark.asyncio
    async def test_pulse_sent_when_toggle_enabled(self) -> None:
        send = AsyncMock()
        fake_scope = _make_fake_session_scope()
        with patch("aemr_bot.services.cron.session_scope", fake_scope), \
             patch(
                 "aemr_bot.services.quiet_hours.refresh_cache_from_db",
                 AsyncMock(),
             ), \
             patch(
                 "aemr_bot.services.notify_toggles.refresh_cache_from_db",
                 AsyncMock(),
             ), \
             patch(
                 "aemr_bot.services.notify_toggles.is_enabled",
                 return_value=True,
             ):
            await cron._job_pulse(send)
        send.assert_called_once()


def _make_fake_session_scope():
    """Тонкий async context manager для `session_scope()` в тестах
    гейтов — возвращает MagicMock как сессию, ничего не коммитит."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _scope():
        yield MagicMock()

    return _scope


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
             patch("aemr_bot.services.users.find_by_max_id",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.services.users.has_open_appeals",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.services.users.erase_pdn",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.services.operators.write_audit", AsyncMock()):
            await cron._job_pdn_retention_check(send)

        texts = [call.args[0] for call in send.call_args_list]
        assert any("30 дней" in text and "42" in text for text in texts)

    @pytest.mark.asyncio
    async def test_open_appeals_do_not_postpone_erasure(self) -> None:
        """Открытые обращения НЕ откладывают уничтожение ПДн.

        Регресс: житель с обращением в NEW/IN_PROGRESS пропускался «до
        следующего дня» — и так бессрочно, пока оператор не ответит.
        Но выборка берёт тех, у кого 30 дней УЖЕ истекли (152-ФЗ ст. 21
        ч. 5), то есть пропуск тянул срок за пределы закона, причём
        молча. Закон не даёт продлевать срок из-за нерасторопности
        оператора: 30 дней и есть окно на финальный ответ.

        Теперь erase_pdn вызывается и при открытых обращениях (он сам их
        закроет), а оператор получает явное предупреждение, что они
        закрылись без ответа.
        """
        send = AsyncMock()
        session = AsyncMock()
        user = MagicMock()
        user.id = 100
        erase = AsyncMock(return_value=True)

        @asynccontextmanager
        async def fake_scope():
            yield session

        with patch("aemr_bot.services.cron.session_scope", fake_scope), \
             patch("aemr_bot.services.users.find_pending_pdn_retention",
                   AsyncMock(return_value=[42])), \
             patch("aemr_bot.services.users.find_by_max_id",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.services.users.has_open_appeals",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.services.users.erase_pdn", erase), \
             patch("aemr_bot.services.operators.write_audit", AsyncMock()):
            await cron._job_pdn_retention_check(send)

        # Главное: уничтожение состоялось, а не отложилось.
        erase.assert_awaited_once()
        texts = [call.args[0] for call in send.call_args_list]
        # И оператор предупреждён, что обращения закрыты без ответа.
        assert any("БЕЗ ответа" in text for text in texts)

    @pytest.mark.asyncio
    async def test_vanished_user_no_phantom_no_audit(self) -> None:
        """Житель исчез между find_pending_pdn_retention() и итерацией
        (успел /forget): find_by_max_id → None → continue. Не создаём
        фантомного User, не зовём erase_pdn, не пишем лишний audit."""
        send = AsyncMock()
        session = AsyncMock()

        @asynccontextmanager
        async def fake_scope():
            yield session

        erase = AsyncMock(return_value=True)
        write_audit = AsyncMock()
        get_or_create = AsyncMock()
        with patch("aemr_bot.services.cron.session_scope", fake_scope), \
             patch("aemr_bot.services.users.find_pending_pdn_retention",
                   AsyncMock(return_value=[42])), \
             patch("aemr_bot.services.users.find_by_max_id",
                   AsyncMock(return_value=None)) as find_by_max_id, \
             patch("aemr_bot.services.users.get_or_create", get_or_create), \
             patch("aemr_bot.services.users.has_open_appeals",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.services.users.erase_pdn", erase), \
             patch("aemr_bot.services.operators.write_audit", write_audit):
            await cron._job_pdn_retention_check(send)

        find_by_max_id.assert_awaited_once()
        get_or_create.assert_not_called()  # никакого фантома
        erase.assert_not_called()
        write_audit.assert_not_called()
        # Ни одного «фактически обезличено» уведомления.
        texts = [call.args[0] for call in send.call_args_list]
        assert not any("обезличен" in text.lower() for text in texts)


class TestFunnelWatchdog:
    """_job_funnel_watchdog — сброс зависших воронок."""

    @pytest.mark.asyncio
    async def test_swallows_exception_no_db(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock()
        send_admin_text = AsyncMock()
        with caplog.at_level(logging.ERROR, logger="aemr_bot.services.cron"), \
             patch("aemr_bot.services.cron.session_scope",
                   side_effect=RuntimeError("db down")):
            result = await cron._job_funnel_watchdog(bot, send_admin_text)
        # Падение БД проглочено верхним try/except: ни житель, ни
        # служебная группа уведомлений не получают, ошибка — в лог.
        assert result is None
        bot.send_message.assert_not_called()
        send_admin_text.assert_not_called()
        assert any(
            "funnel_watchdog crashed" in rec.message for rec in caplog.records
        )

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


class TestNotifyToggleGates:
    """Гейты `admin_notify_*` для monthly-report / open-reminder /
    overdue-reminder (services/notify_toggles.py) — выключен тумблер →
    job не считает данные и не шлёт сообщение; включён → работает
    как раньше."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.services import notify_toggles
        notify_toggles.reset_cache_for_tests()
        yield
        notify_toggles.reset_cache_for_tests()

    # ── monthly report ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_monthly_report_suppressed_when_disabled(self) -> None:
        send_doc = AsyncMock()
        with patch(
            "aemr_bot.services.notify_toggles.is_enabled", return_value=False,
        ):
            await cron._job_monthly_report(send_doc)
        send_doc.assert_not_called()

    @pytest.mark.asyncio
    async def test_monthly_report_sent_when_enabled(self) -> None:
        send_doc = AsyncMock()
        with patch(
            "aemr_bot.services.notify_toggles.is_enabled", return_value=True,
        ), patch.object(cron, "session_scope") as scope, patch.object(
            cron.stats_service, "build_xlsx",
            AsyncMock(return_value=(b"xlsx-bytes", "Июль 2026", 5)),
        ):
            scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            await cron._job_monthly_report(send_doc)
        send_doc.assert_awaited_once()
        kwargs = send_doc.await_args.kwargs
        assert kwargs["content"] == b"xlsx-bytes"
        assert "5 обращений" in kwargs["caption"]

    # ── open reminder ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_open_reminder_suppressed_when_disabled(self) -> None:
        bot = AsyncMock()
        with patch(
            "aemr_bot.services.notify_toggles.is_enabled", return_value=False,
        ), patch.object(cron, "is_workday", return_value=True) as workday:
            await cron._job_working_hours_open_reminder(bot)
        # Гейт срабатывает ДО проверки рабочего дня — считать не нужно.
        workday.assert_not_called()
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_open_reminder_runs_when_enabled(self) -> None:
        bot = AsyncMock()
        with patch(
            "aemr_bot.services.notify_toggles.is_enabled", return_value=True,
        ), patch.object(cron, "is_workday", return_value=True), patch.object(
            cron, "session_scope",
        ) as scope, patch.object(
            cron.appeals_service, "list_unanswered", AsyncMock(return_value=[]),
        ):
            scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            await cron._job_working_hours_open_reminder(bot)
        # Список пуст → тишина (по контракту job'а), но исключений нет
        # и гейт не заблокировал выполнение раньше времени.
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_open_reminder_calls_is_overdue_once_per_appeal(self) -> None:
        """FIX (perf): раньше in_sla/overdue считались двумя list-comp'ами
        → is_overdue звался ДВАЖДЫ на каждое обращение (а он итерирует по
        рабочим дням от created_at). Теперь один проход: ровно 1 вызов на
        обращение, разбиение in_sla/overdue корректно."""
        from datetime import datetime, timedelta

        bot = AsyncMock()
        now = datetime.now(cron.TZ)
        appeals = []
        for i in range(4):
            ap = MagicMock()
            ap.id = 100 + i
            ap.created_at = now - timedelta(hours=i + 1)
            ap.user = MagicMock(first_name=f"U{i}")
            ap.locality = "Елизово"
            appeals.append(ap)

        # Чётные — просрочены, нечётные — в SLA (проверяем корректность разбиения).
        def fake_is_overdue(created_at, now_arg, hrs):
            idx = [a.created_at for a in appeals].index(created_at)
            return idx % 2 == 0

        is_overdue = MagicMock(side_effect=fake_is_overdue)

        with patch(
            "aemr_bot.services.notify_toggles.is_enabled", return_value=True,
        ), patch.object(cron, "is_workday", return_value=True), patch.object(
            cron, "session_scope",
        ) as scope, patch.object(
            cron.appeals_service, "list_unanswered",
            AsyncMock(return_value=appeals),
        ), patch.object(
            cron.sla_service, "is_overdue", is_overdue,
        ), patch.object(
            cron, "_send_with_open_tickets_button", AsyncMock(),
        ) as send_btn:
            scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            await cron._job_working_hours_open_reminder(bot)

        # Ровно один вызов is_overdue на обращение (не два).
        assert is_overdue.call_count == len(appeals)
        # Разбиение корректно: 2 просрочено (idx 0,2), 2 в SLA (idx 1,3).
        send_btn.assert_awaited_once()
        text = send_btn.await_args.args[1]
        assert "в SLA — 2" in text and "просрочено — 2" in text

    # ── overdue reminder ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_overdue_reminder_suppressed_when_disabled(self) -> None:
        bot = AsyncMock()
        with patch(
            "aemr_bot.services.notify_toggles.is_enabled", return_value=False,
        ), patch.object(cron, "is_workday", return_value=True) as workday:
            await cron._job_working_hours_overdue_reminder(bot)
        workday.assert_not_called()
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_overdue_reminder_runs_when_enabled(self) -> None:
        bot = AsyncMock()
        with patch(
            "aemr_bot.services.notify_toggles.is_enabled", return_value=True,
        ), patch.object(cron, "is_workday", return_value=True), patch.object(
            cron, "session_scope",
        ) as scope, patch.object(
            cron.appeals_service, "find_overdue_unanswered",
            AsyncMock(return_value=[]),
        ):
            scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            await cron._job_working_hours_overdue_reminder(bot)
        bot.send_message.assert_not_called()


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
