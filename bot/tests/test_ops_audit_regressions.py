"""Failure paths reproduced during the operations audit."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from aemr_bot import network
from aemr_bot.services import backup_verify, db_backup


def test_pg_dump_decodes_url_credentials(monkeypatch):
    monkeypatch.setattr(db_backup.settings, "database_url",
                        "postgresql+asyncpg://a%40b:p%2F%25%23@db/a%20b")
    env = db_backup._build_pg_env()
    assert (env["PGUSER"], env["PGPASSWORD"], env["PGDATABASE"]) == (
        "a@b", "p/%#", "a b",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase", ["a" * 65536, "long-passphrase\nsecond-line"])
async def test_invalid_passphrase_rejected_before_spawn(monkeypatch, tmp_path, phrase):
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(db_backup.BackupGpgError, match="one line"):
        await db_backup._run_pg_dump_encrypted(tmp_path / "dump", {}, phrase)
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_backup_directory_failure_is_result(monkeypatch, tmp_path):
    target = tmp_path / "file"
    target.write_text("not a directory")
    monkeypatch.setattr(db_backup.settings, "backup_local_dir", str(target))
    monkeypatch.setattr(db_backup.settings, "backup_allow_unencrypted", True)
    result = await db_backup.backup_db()
    assert not result.ok
    assert result.fail_kind == "unknown"


@pytest.mark.asyncio
async def test_verify_discovery_failure_is_result(monkeypatch, tmp_path):
    monkeypatch.setattr(backup_verify.settings, "backup_local_dir", str(tmp_path))
    monkeypatch.setattr(backup_verify.settings, "backup_verify_enabled", True)

    def fail():
        raise PermissionError("unreadable directory")

    monkeypatch.setattr(backup_verify, "_latest_backup", fail)
    result = await backup_verify.verify_latest_backup()
    assert not result.ok
    assert result.fail_kind == "no_backup"


def test_empty_proxy_secret_fails_closed(monkeypatch, tmp_path):
    secret = tmp_path / "proxy"
    secret.write_text(" \n")
    monkeypatch.setattr(network.settings, "outbound_proxy", None)
    monkeypatch.setattr(network.settings, "outbound_proxy_file", str(secret))
    with pytest.raises(ValueError, match="empty"):
        network.apply_firewall_env()


@pytest.mark.asyncio
async def test_subprocess_cancel_kills_and_reaps():
    class Process:
        pid = 123
        killed = False
        reaped = False

        async def wait(self):
            if not self.killed:
                await asyncio.Future()
            self.reaped = True
            return -9

        def kill(self):
            self.killed = True

    proc = Process()
    task = asyncio.create_task(db_backup._wait_proc(proc, 60, "test"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed and proc.reaped


def test_manual_systemd_installer_uses_selected_checkout(tmp_path):
    import os
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    repo = tmp_path / 'custom checkout%with"quote'
    (repo / "infra").mkdir(parents=True)
    (repo / "infra/aemr-bot.service").write_text(
        (root / "infra/aemr-bot.service").read_text()
    )
    commands = tmp_path / "commands"
    commands.mkdir()
    for command in ("docker", "systemctl"):
        stub = commands / command
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o700)
    unit = tmp_path / "installed.service"
    script = tmp_path / "install.sh"
    script.write_text(
        (root / "scripts/install-systemd-autostart.sh").read_text()
        .replace('if [ "$EUID" -ne 0 ]; then', 'if false; then')
        .replace('UNIT_DST="/etc/systemd/system/aemr-bot.service"', f'UNIT_DST="{unit}"')
    )
    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True,
        env={**os.environ, "REPO_DIR": str(repo),
             "PATH": str(commands) + os.pathsep + os.environ["PATH"]},
        check=False, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    escaped = str(repo).replace('"', '\\"').replace('%', '%%')
    assert f'WorkingDirectory="{escaped}/infra"' in unit.read_text()


@pytest.mark.asyncio
async def test_cancelled_backup_removes_partial_file(monkeypatch, tmp_path):
    monkeypatch.setattr(db_backup.settings, "backup_local_dir", str(tmp_path))
    monkeypatch.setattr(db_backup.settings, "backup_gpg_passphrase", None)
    monkeypatch.setattr(db_backup.settings, "backup_allow_unencrypted", True)

    async def dump(path, env):
        path.write_bytes(b"partial")
        raise asyncio.CancelledError

    monkeypatch.setattr(db_backup, "_run_pg_dump", dump)
    with pytest.raises(asyncio.CancelledError):
        await db_backup.backup_db()
    assert list(tmp_path.iterdir()) == []
