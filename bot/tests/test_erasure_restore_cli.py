"""Offline restore must never silently inherit the ambient production target."""
import importlib.util
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[2] / "scripts" / "replay_erasure_tombstones.py"
    spec = importlib.util.spec_from_file_location("restore_erasure_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_restore_requires_explicit_offline_target_without_echoing_credentials(capsys):
    module = _module()
    secret_url = "postgresql+asyncpg://user:secret-password@example.invalid/restored"
    with pytest.raises(SystemExit) as error:
        module.parse_args(["--database-url", secret_url])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "secret-password" not in captured.err + captured.out
    assert "--confirm-offline" in captured.err
    args = module.parse_args(["--database-url", secret_url, "--confirm-offline"])
    assert args.database_url == secret_url and args.confirm_offline


def test_restore_refuses_ambient_database_default(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://production.example/live")
    with pytest.raises(SystemExit):
        _module().parse_args(["--confirm-offline"])


def test_restore_accepts_explicit_environment_target_without_putting_password_in_argv(monkeypatch):
    monkeypatch.setenv("QUARANTINE_DB_URL", "postgresql+asyncpg://user:private@example.invalid/quarantine")
    args = _module().parse_args(["--database-env", "QUARANTINE_DB_URL", "--confirm-offline"])
    assert args.database_url.endswith("/quarantine")
