"""Perf-resilience кластер: warmup, /livez per-poll hang, long-poll
timeout, backup-таймауты.

Покрывает четыре поведенческих фикса (см. PR-описание Волны 2):

(a) Geo-warmup — строго фоновый прогрев geo-индексов: НЕ блокирует старт
    polling, ошибки глушатся (fail-open), реально греет lru_cache.
(b) /livez per-task hang — last_poll_at freshness как второй, независимый
    от heartbeat сигнал живости: /livez краснеет при застойном last_poll,
    даже когда heartbeat свежий; webhook-режим fail-open (poll пуст).
(c) Long-poll timeout — клиентский ClientTimeout.total разведён с
    серверным long-poll hold: build_bot поднимает потолок сессии выше
    polling_timeout + запас.
(d) Backup-таймауты — pg_dump/gpg/rclone оборачиваются в asyncio.wait_for;
    по таймауту процесс убивается+reap'ается, backup отдаёт fail_kind.

Тесты pure-юнит: без реального Postgres/pg_dump/rclone, без сети, без
живого long-poll к MAX. Все внешние процессы — фейки.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# (a) Geo-warmup: строго фоновый, не блокирует старт, fail-open.
# ---------------------------------------------------------------------------
class TestGeoWarmup:
    @pytest.mark.asyncio
    async def test_warm_calls_all_three_loaders_and_find_address(self) -> None:
        """Прогрев дёргает три lru_cache-загрузчика + find_address по
        центру ЕМО — иначе первый житель оплатил бы холодную загрузку
        2.6 МБ прямо в handler'е."""
        from aemr_bot import main

        with (
            patch("aemr_bot.services.geo._load_localities") as m_loc,
            patch("aemr_bot.services.geo._load_buildings_index") as m_bld,
            patch("aemr_bot.services.geo._load_streets_index") as m_str,
            patch("aemr_bot.services.geo.find_address") as m_addr,
        ):
            await main._warm_geo_indexes()

        m_loc.assert_called_once()
        m_bld.assert_called_once()
        m_str.assert_called_once()
        # find_address вызван по координате внутри ЕМО (центр Елизово).
        m_addr.assert_called_once()
        (lat, lon), _ = m_addr.call_args
        assert 52.0 < lat < 54.0
        assert 157.0 < lon < 159.0

    @pytest.mark.asyncio
    async def test_warm_runs_in_thread_not_blocking_loop(self) -> None:
        """Прогрев идёт через asyncio.to_thread: блокирующая загрузка
        НЕ морозит event-loop. Доказываем тем, что параллельная корутина
        тикает, пока «тяжёлый» синхронный загрузчик спит в потоке."""
        from aemr_bot import main

        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(20):
                ticks += 1
                await asyncio.sleep(0.005)

        def slow_blocking_load() -> None:
            # Синхронный sleep — если бы это крутилось в event-loop,
            # ticker не успел бы натикать. В потоке — loop свободен.
            time.sleep(0.15)

        with (
            patch(
                "aemr_bot.services.geo._load_localities",
                side_effect=slow_blocking_load,
            ),
            patch("aemr_bot.services.geo._load_buildings_index"),
            patch("aemr_bot.services.geo._load_streets_index"),
            patch("aemr_bot.services.geo.find_address"),
        ):
            tk = asyncio.create_task(ticker())
            await main._warm_geo_indexes()
            await tk

        # Если бы load блокировал loop, ticks застрял бы у ~1.
        assert ticks >= 10, f"event-loop был заморожен прогревом (ticks={ticks})"

    @pytest.mark.asyncio
    async def test_warm_swallows_errors_fail_open(self) -> None:
        """Ошибка прогрева (нет seed-файлов / битый JSON) НЕ
        пробрасывается — geo деградирует сам, бот остаётся доступен."""
        from aemr_bot import main

        with patch(
            "aemr_bot.services.geo._load_localities",
            side_effect=FileNotFoundError("no seed"),
        ):
            # Не должно бросить.
            await main._warm_geo_indexes()

    @pytest.mark.asyncio
    async def test_warm_is_spawned_as_background_not_awaited_inline(self) -> None:
        """В main() прогрев запускается через spawn_background_task, а не
        await'ится inline до старта polling — иначе медленная загрузка
        задержала бы выход бота в online. Проверяем, что в исходнике
        вызов идёт строго через spawn_background_task(_warm_geo_indexes())."""
        import inspect

        from aemr_bot import main

        src = inspect.getsource(main.main)
        assert "spawn_background_task(_warm_geo_indexes()" in src, (
            "geo-warmup должен спавниться фоном, не await'иться в main()"
        )
        # И НЕ должен await'иться напрямую (это заблокировало бы старт).
        assert "await _warm_geo_indexes()" not in src

    def test_warm_sync_really_populates_lru_cache(self) -> None:
        """Интеграция с настоящим geo: синхронный прогрев реально
        наполняет lru_cache загрузчиков (cache hit после warmup)."""
        from aemr_bot.services import geo

        # Сбрасываем кеши, чтобы стартовать с холодного состояния.
        geo._load_localities.cache_clear()
        geo._load_buildings_index.cache_clear()
        geo._load_streets_index.cache_clear()
        assert geo._load_localities.cache_info().currsize == 0

        main = __import__("aemr_bot.main", fromlist=["_warm_geo_indexes_sync"])
        main._warm_geo_indexes_sync()

        # После прогрева кеши наполнены — первый запрос жителя будет тёплым.
        assert geo._load_localities.cache_info().currsize == 1
        assert geo._load_buildings_index.cache_info().currsize == 1
        assert geo._load_streets_index.cache_info().currsize == 1


# ---------------------------------------------------------------------------
# (b) /livez per-poll hang: last_poll freshness ловит вставший poll-цикл.
# ---------------------------------------------------------------------------
class TestPollWatchFreshness:
    def test_empty_is_fresh_failopen(self) -> None:
        """last_poll == 0.0 (webhook-режим или самый старт) → fresh:
        не краснит процесс, у которого long-poll'а нет вовсе."""
        from aemr_bot.health import PollWatch

        assert PollWatch().is_fresh() is True

    def test_marked_is_fresh(self) -> None:
        from aemr_bot.health import PollWatch

        pw = PollWatch()
        pw.mark()
        assert pw.is_fresh() is True

    def test_stale_is_not_fresh(self) -> None:
        from aemr_bot.health import PollWatch

        pw = PollWatch()
        pw.mark()
        pw.last_poll = time.monotonic() - 10_000.0
        assert pw.is_fresh(max_age=60.0) is False

    def test_default_max_age_uses_polling_timeout_times_factor(self) -> None:
        """Дефолтный порог = polling_timeout * livez_poll_stale_factor."""
        from aemr_bot import health
        from aemr_bot.config import settings as cfg

        pw = health.PollWatch()
        pw.mark()
        window = cfg.polling_timeout_seconds * cfg.livez_poll_stale_factor
        # Чуть внутри окна — свежий.
        pw.last_poll = time.monotonic() - (window * 0.5)
        assert pw.is_fresh() is True
        # Чуть за окном — протух.
        pw.last_poll = time.monotonic() - (window + 5.0)
        assert pw.is_fresh() is False


class TestLivezReactsToStalePoll:
    @pytest.mark.asyncio
    async def test_livez_red_when_poll_stale_even_if_heartbeat_fresh(self) -> None:
        """Ядро finding b: heartbeat свежий (его таск лишь спит и бьёт),
        но poll-цикл встал → /livez ОБЯЗАН покраснеть (503), иначе нет
        авто-рестарта зависшего процесса."""
        from aemr_bot import health

        request = SimpleNamespace(remote="127.0.0.1")
        with (
            patch.object(health.heartbeat, "is_fresh", return_value=True),
            patch.object(health.poll_watch, "is_fresh", return_value=False),
            patch(
                "aemr_bot.health._ping_db_cached",
                new=AsyncMock(side_effect=AssertionError("DB must not be pinged")),
            ),
        ):
            resp = await health._livez(request)

        assert resp.status == 503
        import json

        payload = json.loads(resp.text)
        assert payload["ok"] is False
        assert payload["heartbeat_fresh"] is True
        assert payload["poll_fresh"] is False

    @pytest.mark.asyncio
    async def test_livez_green_when_both_fresh(self) -> None:
        from aemr_bot import health

        request = SimpleNamespace(remote="127.0.0.1")
        with (
            patch.object(health.heartbeat, "is_fresh", return_value=True),
            patch.object(health.poll_watch, "is_fresh", return_value=True),
        ):
            resp = await health._livez(request)

        assert resp.status == 200
        import json

        payload = json.loads(resp.text)
        assert payload["ok"] is True
        assert payload["poll_fresh"] is True

    @pytest.mark.asyncio
    async def test_livez_webhook_failopen_when_poll_never_marked(self) -> None:
        """Webhook-режим: poll_watch ни разу не отмечался (long-poll'а
        нет). /livez НЕ должен краснеть по этому сигналу — liveness несёт
        heartbeat. Используем реальный poll_watch с last_poll=0.0."""
        from aemr_bot import health

        request = SimpleNamespace(remote="127.0.0.1")
        with (
            patch.object(health.heartbeat, "is_fresh", return_value=True),
            patch.object(health.poll_watch, "last_poll", 0.0),
        ):
            resp = await health._livez(request)

        assert resp.status == 200
        import json

        payload = json.loads(resp.text)
        assert payload["ok"] is True
        assert payload["poll_fresh"] is True

    @pytest.mark.asyncio
    async def test_external_client_gets_only_ok_flag(self) -> None:
        """Внешним (не-localhost) клиентам не отдаём operational-детали,
        но статус 503 при застойном poll'е сохраняется."""
        from aemr_bot import health

        request = SimpleNamespace(remote="203.0.113.7")
        with (
            patch.object(health.heartbeat, "is_fresh", return_value=True),
            patch.object(health.poll_watch, "is_fresh", return_value=False),
        ):
            resp = await health._livez(request)

        assert resp.status == 503
        import json

        payload = json.loads(resp.text)
        assert payload == {"ok": False}


class TestPollingWrapperMarksOnSuccess:
    @pytest.mark.asyncio
    async def test_get_updates_wrapper_marks_poll_on_success(self) -> None:
        """Polling-обёртка отмечает poll_watch на КАЖДОМ успешном
        get_updates и проставляет наш серверный long-poll timeout."""
        from aemr_bot import health, main
        from aemr_bot.config import settings as cfg

        seen_kwargs: dict = {}

        class _FakeBot:
            async def get_updates(self, *args, **kwargs):
                seen_kwargs.update(kwargs)
                return {"updates": [], "marker": None}

        bot = _FakeBot()
        main._install_polling_timeout(bot, cfg.polling_timeout_seconds)

        health.poll_watch.last_poll = 0.0
        result = await bot.get_updates(marker=None)

        assert result == {"updates": [], "marker": None}
        assert seen_kwargs.get("timeout") == cfg.polling_timeout_seconds
        assert health.poll_watch.last_poll != 0.0, "успех должен двигать метку"

    @pytest.mark.asyncio
    async def test_get_updates_wrapper_does_not_mark_on_failure(self) -> None:
        """На исключении (timeout/сеть) метку НЕ двигаем — мёртвый цикл,
        безостановочно бросающий ошибки, не должен выглядеть живым."""
        from aemr_bot import health, main
        from aemr_bot.config import settings as cfg

        class _FailBot:
            async def get_updates(self, *args, **kwargs):
                raise asyncio.TimeoutError()

        bot = _FailBot()
        main._install_polling_timeout(bot, cfg.polling_timeout_seconds)

        sentinel = time.monotonic() - 9999.0
        health.poll_watch.last_poll = sentinel
        with pytest.raises(asyncio.TimeoutError):
            await bot.get_updates(marker=None)

        assert health.poll_watch.last_poll == sentinel, (
            "сбой не должен обновлять last_poll"
        )


# ---------------------------------------------------------------------------
# (c) Long-poll timeout: клиентский total разведён с серверным hold.
# ---------------------------------------------------------------------------
class TestLongPollClientTimeout:
    def test_default_config_invariant_client_exceeds_server_hold(self) -> None:
        """Дефолты держат инвариант: клиентский total (max_api) ≥ серверный
        long-poll hold + запас. Иначе холостые циклы рвутся по таймауту и
        бот переподключается 24/7."""
        from aemr_bot.config import settings as cfg

        assert (
            cfg.max_api_timeout_seconds
            >= cfg.polling_timeout_seconds
            + cfg.polling_client_timeout_buffer_seconds
        )

    def test_apply_raises_ceiling_when_polling_timeout_high(self) -> None:
        """Если серверный hold поднят выше клиентского потолка,
        _apply_polling_client_timeout подтягивает total так, чтобы клиент
        ждал дольше сервера."""
        from aiohttp import ClientTimeout

        from aemr_bot import main

        # bot с низким клиентским total (30) и «поднятым» сервером (60).
        conn = SimpleNamespace(timeout=ClientTimeout(total=30.0, sock_connect=30))
        bot = SimpleNamespace(default_connection=conn)

        with patch.object(main, "settings") as st:
            st.polling_timeout_seconds = 60
            st.polling_client_timeout_buffer_seconds = 10.0
            main._apply_polling_client_timeout(bot)

        assert conn.timeout.total == 70.0
        # Прочие поля сессии сохранены.
        assert conn.timeout.sock_connect == 30

    def test_apply_does_not_lower_existing_total(self) -> None:
        """Когда текущий потолок уже выше нужного (send/edit fail-fast),
        не опускаем его — иначе сломали бы быстрый провал отправок."""
        from aiohttp import ClientTimeout

        from aemr_bot import main

        conn = SimpleNamespace(timeout=ClientTimeout(total=120.0))
        bot = SimpleNamespace(default_connection=conn)

        with patch.object(main, "settings") as st:
            st.polling_timeout_seconds = 20
            st.polling_client_timeout_buffer_seconds = 10.0
            main._apply_polling_client_timeout(bot)

        assert conn.timeout.total == 120.0  # не понижен

    def test_build_bot_polling_applies_client_timeout(self) -> None:
        """build_bot в polling-режиме поднимает клиентский total бота выше
        серверного hold (сквозная проверка через настоящий maxapi.Bot)."""
        from aemr_bot import main
        from aemr_bot.config import settings as cfg

        bot = main.build_bot()
        total = bot.default_connection.timeout.total
        assert total is not None
        assert (
            total
            >= cfg.polling_timeout_seconds
            + cfg.polling_client_timeout_buffer_seconds
        )


# ---------------------------------------------------------------------------
# (d) Backup-таймауты: повисший процесс убивается+reap, отдаётся fail_kind.
# ---------------------------------------------------------------------------
class _HangingProc:
    """Фейк asyncio-процесса, чей wait() висит, пока не позовут kill()."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.killed = False
        self._done = asyncio.Event()

    async def wait(self) -> int:
        await self._done.wait()
        return -9 if self.killed else 0

    def kill(self) -> None:
        self.killed = True
        self._done.set()


class _ImmediateProc:
    """Фейк процесса, завершающегося сразу с заданным кодом."""

    def __init__(self, rc: int = 0, pid: int = 111) -> None:
        self._rc = rc
        self.pid = pid
        self.killed = False

    async def wait(self) -> int:
        return self._rc

    def kill(self) -> None:  # pragma: no cover - не должен вызываться
        self.killed = True


class TestWaitProcTimeout:
    @pytest.mark.asyncio
    async def test_hang_is_killed_and_raises_timeout(self) -> None:
        """Повисший proc.wait() убивается по таймауту, reap'ается и
        бросает BackupTimeoutError — не висит вечно."""
        from aemr_bot.services import db_backup

        proc = _HangingProc()
        with pytest.raises(db_backup.BackupTimeoutError):
            await db_backup._wait_proc(proc, timeout=0.05, label="pg_dump")

        assert proc.killed is True, "зависший процесс должен быть убит"

    @pytest.mark.asyncio
    async def test_fast_proc_returns_rc_without_kill(self) -> None:
        """Быстрый процесс не трогаем — возвращаем его код."""
        from aemr_bot.services import db_backup

        proc = _ImmediateProc(rc=0)
        rc = await db_backup._wait_proc(proc, timeout=5.0, label="rclone")
        assert rc == 0
        assert proc.killed is False

    @pytest.mark.asyncio
    async def test_wait_proc_timeout_is_bounded(self) -> None:
        """Таймаут реально ограничивает время ожидания (не виснет)."""
        from aemr_bot.services import db_backup

        proc = _HangingProc()
        t0 = time.monotonic()
        with pytest.raises(db_backup.BackupTimeoutError):
            await db_backup._wait_proc(proc, timeout=0.05, label="gpg")
        elapsed = time.monotonic() - t0
        # С запасом на reap (≤10с) — но точно не «вечно».
        assert elapsed < 5.0


class TestEncryptedPipelineTimeout:
    @pytest.mark.asyncio
    async def test_encrypted_pg_dump_gpg_hang_kills_both(self) -> None:
        """Зашифрованный путь (pg_dump | gpg) при зависании обоих процессов
        убивает И reap'ает оба по общему бюджету и бросает
        BackupTimeoutError — иначе голый gather висел бы вечно."""
        from aemr_bot.services import db_backup

        dump = _HangingProc(pid=501)
        gpg = _HangingProc(pid=502)

        async def fake_exec(*args, **kwargs):
            prog = args[0] if args else ""
            return gpg if prog == "gpg" else dump

        with patch.object(db_backup, "settings") as st:
            st.backup_pg_dump_timeout_seconds = 0.05
            st.backup_gpg_timeout_seconds = 0.05
            with patch(
                "asyncio.create_subprocess_exec", side_effect=fake_exec
            ):
                with pytest.raises(db_backup.BackupTimeoutError):
                    await db_backup._run_pg_dump_encrypted(
                        __import__("pathlib").Path("/tmp/x.sql.gpg"),
                        {"PGHOST": "h"},
                        "passphrase-12chars",
                    )

        assert dump.killed is True, "pg_dump должен быть убит по таймауту"
        assert gpg.killed is True, "gpg должен быть убит по таймауту"


class TestBackupDbSurfacesTimeoutFailKind:
    @pytest.mark.asyncio
    async def test_unencrypted_pg_dump_hang_returns_fail_kind(self, tmp_path) -> None:
        """Сквозь backup_db: повисший pg_dump (unencrypted путь) даёт
        BackupResult с fail_kind (не падает, не висит) → существующий
        категоризированный admin-алёрт сработает."""
        from aemr_bot.services import db_backup

        hanging = _HangingProc()

        async def fake_exec(*args, **kwargs):
            return hanging

        with patch.object(db_backup, "settings") as st:
            st.backup_local_dir = str(tmp_path)
            st.backup_gpg_passphrase = None
            st.backup_allow_unencrypted = True  # разрешаем plain для теста
            st.backup_keep_count = 8
            st.backup_pg_dump_timeout_seconds = 0.05
            st.backup_gpg_timeout_seconds = 0.05
            st.backup_rclone_timeout_seconds = 0.05
            st.database_url = "postgresql://u:p@h:5432/db"
            st.backup_s3_bucket = None
            st.backup_s3_endpoint = None
            st.backup_s3_access_key = None
            st.backup_s3_secret_key = None
            with patch(
                "asyncio.create_subprocess_exec",
                side_effect=fake_exec,
            ):
                result = await db_backup.backup_db()

        assert result.ok is False
        assert result.path is None
        # Timeout проваливается в generic-ветку → fail_kind="unknown".
        assert result.fail_kind == "unknown"
        assert result.fail_detail  # непустое описание для алёрта
        assert hanging.killed is True

    @pytest.mark.asyncio
    async def test_s3_upload_hang_does_not_kill_whole_backup(
        self, tmp_path
    ) -> None:
        """Повисший rclone убивается по таймауту, но локальный бэкап уже
        записан — backup_db всё равно отдаёт .ok=True (S3 опционален)."""
        from aemr_bot.services import db_backup

        # pg_dump — мгновенный успех; rclone — висит.
        dump = _ImmediateProc(rc=0)
        hanging_rclone = _HangingProc()
        calls = {"n": 0}

        async def fake_exec(*args, **kwargs):
            calls["n"] += 1
            prog = args[0] if args else ""
            if prog == "rclone":
                return hanging_rclone
            return dump

        with patch.object(db_backup, "settings") as st:
            st.backup_local_dir = str(tmp_path)
            st.backup_gpg_passphrase = None
            st.backup_allow_unencrypted = True
            st.backup_keep_count = 8
            st.backup_pg_dump_timeout_seconds = 5.0
            st.backup_gpg_timeout_seconds = 5.0
            st.backup_rclone_timeout_seconds = 0.05
            st.database_url = "postgresql://u:p@h:5432/db"
            st.backup_s3_bucket = "bucket"
            st.backup_s3_endpoint = "https://s3.example"
            st.backup_s3_access_key = "ak"
            st.backup_s3_secret_key = "sk"
            # pg_dump пишет в файл через open() в потоке — реально создаём
            # пустой файл, чтобы out.stat() и chmod не падали.
            with patch(
                "asyncio.create_subprocess_exec",
                side_effect=fake_exec,
            ):
                result = await db_backup.backup_db()

        # Локальный бэкап успешен несмотря на повисший S3.
        assert result.ok is True
        assert result.path is not None
        assert result.path.exists()
        # rclone был убит по таймауту.
        assert hanging_rclone.killed is True
