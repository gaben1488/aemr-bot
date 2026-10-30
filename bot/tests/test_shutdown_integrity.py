"""Real scheduler cancellation and application resource-lifetime regressions."""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from apscheduler.events import EVENT_SCHEDULER_SHUTDOWN
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aemr_bot.services import cron


@pytest.mark.asyncio
async def test_installed_asyncio_executor_cancels_even_when_wait_true() -> None:
    """The 3.x API docs' wait promise does not apply to its asyncio executor."""
    started, cancelled, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def job() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(job)
    scheduler.add_listener(lambda event: stopped.set(), EVENT_SCHEDULER_SHUTDOWN)
    scheduler.start()
    await asyncio.wait_for(started.wait(), 1)
    scheduler.shutdown(wait=True)
    await asyncio.wait_for(stopped.wait(), 1)
    await asyncio.wait_for(cancelled.wait(), 1)


@pytest.mark.asyncio
async def test_cron_drain_finishes_current_job_and_stops_future_jobs(monkeypatch) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    writes: list[str] = []

    async def job(send) -> None:
        started.set()
        await release.wait()
        writes.append("committed")

    monkeypatch.setattr(cron, "_job_threat_intel_refresh", job)
    monkeypatch.setattr("aemr_bot.handlers.appeal_runtime.recover_unpublished_appeals", AsyncMock())
    scheduler = cron.build_scheduler(MagicMock(), AsyncMock(), AsyncMock())
    scheduler.start()
    await asyncio.wait_for(started.wait(), 1)
    stopping = asyncio.create_task(scheduler.shutdown_gracefully(timeout=1))
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    assert await stopping == []
    assert writes == ["committed"]
    assert not scheduler.running


@pytest.mark.asyncio
async def test_cron_timeout_waits_for_cancel_cleanup(monkeypatch) -> None:
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def job(send) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    monkeypatch.setattr(cron, "_job_threat_intel_refresh", job)
    monkeypatch.setattr("aemr_bot.handlers.appeal_runtime.recover_unpublished_appeals", AsyncMock())
    scheduler = cron.build_scheduler(MagicMock(), AsyncMock(), AsyncMock())
    scheduler.start()
    await asyncio.wait_for(started.wait(), 1)
    assert await scheduler.shutdown_gracefully(timeout=0) == ["threat-intel-refresh"]
    assert cleaned.is_set()
    assert not scheduler.running


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatcher_error", [None, RuntimeError("shutdown failed")])
@pytest.mark.parametrize("failure_point", ["seed", "journal"])
async def test_startup_failure_closes_resources_and_releases_lock(
    monkeypatch, dispatcher_error, failure_point,
) -> None:
    from aemr_bot import main
    from aemr_bot.db import session

    conn = object()
    release = AsyncMock()
    close = AsyncMock()
    dispose = AsyncMock()
    monkeypatch.setattr(main, "setup_logging", lambda: None)
    monkeypatch.setattr(main, "_preflight_check_token", AsyncMock())
    monkeypatch.setattr(main, "_register_bot_commands", AsyncMock())
    from aemr_bot.services import data_erasure
    monkeypatch.setattr(data_erasure, "prepare_startup", AsyncMock(
        side_effect=RuntimeError("journal unavailable") if failure_point == "journal" else None))
    monkeypatch.setattr(main, "_seed_settings", AsyncMock(
        side_effect=RuntimeError("db down") if failure_point == "seed" else None))
    intake = AsyncMock()
    monkeypatch.setattr(main.dp, "start_polling", intake)
    monkeypatch.setattr(main.single_instance, "acquire_single_instance_lock", AsyncMock(return_value=conn))
    monkeypatch.setattr(main.single_instance, "release_single_instance_lock", release)
    monkeypatch.setattr(main.bot, "close_session", close)
    monkeypatch.setattr(main.dp, "shutdown", AsyncMock(side_effect=dispatcher_error))
    monkeypatch.setattr(session, "engine", SimpleNamespace(dispose=dispose))

    with pytest.raises(RuntimeError, match="db down" if failure_point == "seed" else "journal unavailable"):
        await main.main()

    intake.assert_not_awaited()
    release.assert_awaited_once_with(conn)
    close.assert_awaited_once()
    dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_stops_producers_and_drains_before_closing_clients(monkeypatch) -> None:
    from aemr_bot import main
    from aemr_bot.db import session

    calls: list[str] = []

    def step(name, result=None):
        async def run(*args, **kwargs):
            calls.append(name)
            return result
        return run

    @asynccontextmanager
    async def session_scope():
        yield MagicMock()

    monkeypatch.setattr(main, "setup_logging", lambda: None)
    for name in (
        "_preflight_check_token", "_register_bot_commands", "_seed_settings",
        "_warm_cache", "_warm_geo_indexes", "_hydrate_wizards", "recover_stuck_funnels",
    ):
        monkeypatch.setattr(main, name, AsyncMock())
    from aemr_bot.services import data_erasure, db_backup, work_schedule
    monkeypatch.setattr(data_erasure, "prepare_startup", step("journal"))
    monkeypatch.setattr(work_schedule, "initialize", step("calendar_initialize"))
    monkeypatch.setattr(work_schedule, "refresh", step("calendar_refresh"))
    monkeypatch.setattr(data_erasure, "recover_deletions", AsyncMock())
    monkeypatch.setattr(db_backup, "cleanup_erased_backups", AsyncMock())
    monkeypatch.setattr(main, "session_scope", session_scope)
    monkeypatch.setattr(main.settings, "bootstrap_it_max_user_id", None)
    monkeypatch.setattr(main.settings, "bot_mode", "polling")
    monkeypatch.setattr(main.policy_service, "ensure_uploaded", AsyncMock())
    monkeypatch.setattr(main.broadcasts_service, "reap_orphaned_sending", AsyncMock(return_value=0))
    monkeypatch.setattr(main.broadcasts_service, "find_abandoned_drafts", AsyncMock(return_value=[]))
    monkeypatch.setattr(main.single_instance, "acquire_single_instance_lock", AsyncMock())
    monkeypatch.setattr(main.single_instance, "release_single_instance_lock", step("unlock"))
    monkeypatch.setattr(session, "engine", SimpleNamespace(dispose=step("database")))
    monkeypatch.setattr(main, "bot", SimpleNamespace(close_session=step("MAX")))
    monkeypatch.setattr(main, "dp", SimpleNamespace(
        start_polling=step("polling"), shutdown=step("inbox"),
    ))
    monkeypatch.setattr(main.health, "start", AsyncMock(return_value=SimpleNamespace(
        cleanup=step("webhook"),
    )))
    scheduler = SimpleNamespace(
        start=lambda: None, running=True,
        pause=lambda: calls.append("pause"), shutdown_gracefully=step("cron"),
    )
    monkeypatch.setattr(cron, "build_scheduler", lambda *args: scheduler)
    monkeypatch.setattr(cron.threat_intel, "get_store", lambda: None)
    monkeypatch.setattr(cron, "close_healthcheck_session", step("healthcheck"))
    original_drain = main.drain_background_tasks

    async def drain():
        calls.append("background")
        return await original_drain(timeout=0.2)

    monkeypatch.setattr(main, "drain_background_tasks", drain)
    await main.main()
    assert calls == [
        "journal", "calendar_initialize", "calendar_refresh", "polling",
        "pause", "webhook", "inbox", "cron", "background", "healthcheck",
        "unlock", "database", "MAX",
    ]


@pytest.mark.asyncio
async def test_sigterm_runs_finalizer_without_repeated_cancellation() -> None:
    """Exercise real SIGTERM on an isolated interpreter, without running the bot."""
    if sys.platform == "win32":
        pytest.skip("SIGTERM delivery uses the Linux deployment path")
    script = '''
import asyncio
from aemr_bot.main import _install_sigterm_handler

async def run():
    remove = _install_sigterm_handler()
    print("ready", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        print("draining", flush=True)
        await asyncio.sleep(0.1)
        remove()
        print("closed", flush=True)

try:
    asyncio.run(run())
except asyncio.CancelledError:
    pass
'''
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    assert child.stdout is not None
    try:
        assert await asyncio.wait_for(child.stdout.readline(), 5) == b"ready\n"
        os.kill(child.pid, signal.SIGTERM)
        assert await asyncio.wait_for(child.stdout.readline(), 5) == b"draining\n"
        os.kill(child.pid, signal.SIGTERM)
        stdout, stderr = await asyncio.wait_for(child.communicate(), 5)
        assert child.returncode == 0, stderr.decode()
        assert stdout == b"closed\n"
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


@pytest.mark.asyncio
async def test_healthcheck_http_client_is_closed_once(monkeypatch) -> None:
    session = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(cron, "_HEALTHCHECK_SESSION", session)
    await cron.close_healthcheck_session()
    await cron.close_healthcheck_session()
    session.close.assert_awaited_once()
    assert cron._HEALTHCHECK_SESSION is None
