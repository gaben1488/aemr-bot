"""Reproduce audit findings without changing production behavior or contacting MAX.

Historical reproductions for commit 547ae95 only; release regressions live in bot/tests.
Run on that historical checkout from bot/: uv run --no-sync pytest -c pyproject.toml ../docs/audits/flow_audit_probes_20261026.py -q
These assertions confirm existing defects, rather than specify their future fixes.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("BOT_TOKEN", "synthetic-audit-token")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ADMIN_GROUP_ID", "123")
os.environ.setdefault("TZ", "Asia/Kamchatka")

from aemr_bot.handlers import IdempotencyMiddleware, appeal_runtime
from aemr_bot.handlers import broadcast as bc
from aemr_bot.services import backup_verify, cron, idempotency
from aemr_bot.services import wizard_registry as wr
from maxapi import Bot, Dispatcher
from maxapi.types.users import User as MaxUser


@asynccontextmanager
async def session():
    yield SimpleNamespace()


def update():
    return {"update_type": "message_created", "timestamp": 1700000000000,
            "message": {"timestamp": 1700000000000,
                        "recipient": {"chat_id": 91001, "chat_type": "dialog"},
                        "sender": {"user_id": 91002, "first_name": "Synthetic", "is_bot": False},
                        "body": {"mid": "audit-message", "seq": 1, "text": "synthetic"}}}


@pytest.mark.asyncio
async def test_db_claim_failure_still_advances_polling_marker(monkeypatch):
    @asynccontextmanager
    async def broken_db():
        raise RuntimeError("synthetic DB outage")
        yield  # pragma: no cover

    monkeypatch.setattr(idempotency, "session_scope", broken_db)
    bot = Bot("synthetic-audit-token", auto_requests=False, auto_check_subscriptions=False)
    bot.get_me = AsyncMock(return_value=MaxUser(user_id=1, first_name="Audit", is_bot=True))
    dp = Dispatcher(use_create_task=True)
    dp.register_outer_middleware(IdempotencyMiddleware())
    handled = []

    @dp.message_created()
    async def handler(event):
        handled.append(event.message.body.text)

    try:
        await dp.startup(bot)
        await dp._dispatch_fetched_events({"updates": [update()], "marker": 101}, 0,
                                         skip_updates=False)
        await dp.shutdown()
        assert handled == []
        assert bot.marker_updates == 101
    finally:
        await dp.shutdown()
        await bot.close_session()


@pytest.mark.asyncio
async def test_user_lock_can_split_while_an_existing_waiter_is_pending(monkeypatch):
    monkeypatch.setattr(appeal_runtime, "_user_locks", {})
    owner = appeal_runtime.get_user_lock(42)
    await owner.acquire()
    queued, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def waiting_request():
        old_lock = appeal_runtime.get_user_lock(42)
        queued.set()
        async with old_lock:
            entered.set()
            await release.wait()

    waiter = asyncio.create_task(waiting_request())
    try:
        await queued.wait()
        owner.release()
        appeal_runtime.drop_user_lock(42)
        newcomer = appeal_runtime.get_user_lock(42)
        await newcomer.acquire()
        await asyncio.wait_for(entered.wait(), 0.5)
        assert newcomer is not owner
        assert owner.locked() and newcomer.locked()
        newcomer.release()
    finally:
        release.set()
        await waiter


@pytest.mark.asyncio
async def test_deadline_warning_is_not_checked_when_no_overdue_users(monkeypatch):
    monkeypatch.setattr(cron, "session_scope", session)
    monkeypatch.setattr(cron.users_service, "find_pending_pdn_retention", AsyncMock(return_value=[]))
    nearing = AsyncMock(return_value=[91002])
    monkeypatch.setattr(cron.users_service, "find_revoked_deadline_approaching", nearing)
    notify = AsyncMock()
    await cron._job_pdn_retention_check(notify)
    assert nearing.await_count == 0
    assert notify.await_count == 0


@pytest.mark.asyncio
async def test_gpg_timeout_does_not_bound_stdout_drain(monkeypatch, tmp_path):
    class StalledStdout:
        async def read(self, size):
            await asyncio.Event().wait()

    proc = SimpleNamespace(stdout=StalledStdout())
    waited = AsyncMock(return_value=0)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(backup_verify.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    monkeypatch.setattr(backup_verify, "_wait_proc", waited)
    # Configured 1 ms limit is never entered; only this external timeout stops it.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(backup_verify._decrypt_to_devnull(
            tmp_path / "synthetic.sql.gpg", "synthetic-passphrase", timeout=0.001), 0.08)
    assert waited.await_count == 0


@pytest.mark.asyncio
async def test_late_wizard_save_can_resurrect_cleared_durable_state(monkeypatch):
    monkeypatch.setattr(wr, "_broadcast_wizards", {})
    durable = {}
    started, release = asyncio.Event(), asyncio.Event()

    async def old_save(operator_id, snapshot):
        started.set()
        await release.wait()  # e.g. delayed connection acquisition before UPSERT
        durable[operator_id] = snapshot

    async def newer_delete(operator_id):
        durable.pop(operator_id, None)

    monkeypatch.setattr(wr, "_persist_save_broadcast", old_save)
    monkeypatch.setattr(wr, "_persist_delete_broadcast", newer_delete)
    wr.set_broadcast_wizard(7, {"step": "awaiting_confirm", "text": "synthetic"})
    wr.schedule_persist_broadcast(7)
    await started.wait()
    wr.clear_broadcast_wizard(7)
    wr.schedule_persist_broadcast(7)
    await asyncio.sleep(0)
    release.set()
    from aemr_bot.utils.background import _BACKGROUND_TASKS
    tasks = [task for task in _BACKGROUND_TASKS if task.get_name() == "wizard_registry.persist"]
    await asyncio.gather(*tasks)
    assert wr.get_broadcast_wizard(7) is None
    assert durable[7]["step"] == "awaiting_confirm"


@pytest.mark.asyncio
async def test_cancelled_broadcast_still_sends_before_first_cancel_check(monkeypatch):
    monkeypatch.setattr(bc, "session_scope", session)
    monkeypatch.setattr(bc.broadcasts_service, "record_deliveries", AsyncMock())
    monkeypatch.setattr(bc.broadcasts_service, "get_status", AsyncMock(return_value="cancelled"))
    monkeypatch.setattr(bc.broadcasts_service, "update_progress", AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())
    delivered, failed, cancelled = await bc._run_send_loop(
        bot, broadcast_id=7, body="synthetic", total=1, targets=[(1, 91002)],
        admin_mid=None, rate_delay=0, progress_step_sec=0)
    assert delivered == 1 and failed == 0 and cancelled
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_broadcast_can_finish_done_without_recorded_delivery_rows(monkeypatch):
    monkeypatch.setattr(bc, "session_scope", session)
    monkeypatch.setattr(bc, "_resolve_admin_progress_message", AsyncMock(return_value=None))
    monkeypatch.setattr(bc, "_send_final_summary", AsyncMock())
    monkeypatch.setattr(bc.broadcasts_service, "mark_started", AsyncMock())
    monkeypatch.setattr(bc.broadcasts_service, "list_subscriber_targets", AsyncMock(return_value=[(1, 91002)]))
    monkeypatch.setattr(bc.broadcasts_service, "get_by_id", AsyncMock(return_value=SimpleNamespace(attachments=[])))
    record = AsyncMock(side_effect=RuntimeError("synthetic DB write failure"))
    monkeypatch.setattr(bc.broadcasts_service, "record_deliveries", record)
    finished = AsyncMock()
    monkeypatch.setattr(bc.broadcasts_service, "mark_finished", finished)
    monkeypatch.setattr(bc.cfg, "broadcast_rate_limit_per_sec", 1000)
    bot = SimpleNamespace(send_message=AsyncMock())
    await bc._run_broadcast_impl(bot, 7, "synthetic", 1)
    assert record.await_count > 0
    assert finished.await_args.kwargs["status"].value == "done"
    assert finished.await_args.kwargs["delivered"] == 1


def test_webhook_mode_fails_at_module_import_with_pinned_sdk():
    env = dict(os.environ, BOT_TOKEN="synthetic-audit-token", BOT_MODE="webhook",
               WEBHOOK_URL="https://synthetic.invalid/max/webhook",
               WEBHOOK_SECRET="synthetic-audit-secret", DATABASE_URL="sqlite+aiosqlite:///:memory:")
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, "-c", "import aemr_bot.main"], cwd=root / "bot",
                            env=env, capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode != 0
    assert "webhook_post" in result.stderr and "AttributeError" in result.stderr


def test_failed_build_moves_head_and_next_autodeploy_skips_retry(tmp_path):
    root = Path(__file__).resolve().parents[2]
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "infra").mkdir()
    (repo / "infra" / ".env").write_text("# synthetic audit environment\n")
    state, builds = tmp_path / "head", tmp_path / "builds"
    state.write_text("old\n")
    builds.write_text("")
    tools = tmp_path / "tools"
    tools.mkdir()
    scripts = {
        "git": '''#!/bin/sh
case "$1" in
 fetch) exit 0 ;;
 rev-parse)
  if [ "$2" = HEAD ]; then cat "$AEMR_AUDIT_HEAD"; else printf 'new\\n'; fi ;;
 status) exit 0 ;;
 reset) printf '%s\\n' "$3" > "$AEMR_AUDIT_HEAD" ;;
 *) exit 0 ;;
esac
''',
        "su": '''#!/bin/sh
printf 'attempt\\n' >> "$AEMR_AUDIT_BUILDS"
exit 42
''',
        "logger": "#!/bin/sh\ncat >/dev/null\n",
        "install": "#!/bin/sh\nexit 0\n",
        "chown": "#!/bin/sh\nexit 0\n",
    }
    for name, body in scripts.items():
        path = tools / name
        path.write_text(body)
        path.chmod(0o700)
    # Only substitute filesystem locations; the deployment control flow is unchanged.
    content = (root / "scripts/auto-deploy.sh").read_text()
    content = content.replace("REPO_DIR=/home/aemr/aemr-bot", f"REPO_DIR={repo}")
    content = content.replace("/tmp/aemr-bot.env.bak.", str(tmp_path / "env.bak."))
    copy = tmp_path / "deploy.sh"
    copy.write_text(content)
    env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"],
               AEMR_AUDIT_HEAD=str(state), AEMR_AUDIT_BUILDS=str(builds),
               AUTODEPLOY_ENABLED="1", REQUIRE_SIGNED_COMMITS="", REQUIRE_CANONICAL_MARKER="")
    first = subprocess.run(["bash", str(copy)], env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, check=False, timeout=5)
    assert first.returncode == 42
    assert state.read_text().strip() == "new"
    second = subprocess.run(["bash", str(copy)], env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, check=False, timeout=5)
    assert second.returncode == 0
    assert builds.read_text().splitlines() == ["attempt"]
