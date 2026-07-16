"""Проверка пригодности бэкапа (services/backup_verify.py).

Проверяем ЛОГИКУ: выбор свежайшего файла, категоризацию провалов и то,
что расшифрованный дамп (открытые ПДн) никуда не пишется. Сам gpg
подменяется — вызывать настоящий в юнит-тестах незачем.
"""
from __future__ import annotations

import os

import pytest

from aemr_bot.services import backup_verify
from aemr_bot.services.backup_verify import (
    PG_DUMP_SIGNATURE,
    VerifyResult,
    _latest_backup,
    verify_latest_backup,
)

DUMP_HEAD = b"--\n-- PostgreSQL database dump\n--\n\nSET statement_timeout = 0;\n"


# --- выбор свежайшего файла -------------------------------------------


def test_latest_backup_picks_newest(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        backup_verify.settings, "backup_local_dir", str(tmp_path), raising=False
    )
    old = tmp_path / "aemr-20260101-030000.sql.gpg"
    new = tmp_path / "aemr-20260701-030000.sql.gpg"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))

    assert _latest_backup() == new


def test_latest_backup_ignores_foreign_files(tmp_path, monkeypatch) -> None:
    """Чужие файлы в /backups (README, .tmp от оборванного дампа) не
    должны выдаваться за бэкап."""
    monkeypatch.setattr(
        backup_verify.settings, "backup_local_dir", str(tmp_path), raising=False
    )
    (tmp_path / "README.txt").write_bytes(b"x")
    (tmp_path / "aemr-20260701.sql.tmp").write_bytes(b"x")
    assert _latest_backup() is None

    real = tmp_path / "aemr-20260701-030000.sql"
    real.write_bytes(b"dump")
    assert _latest_backup() == real


def test_latest_backup_none_when_dir_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        backup_verify.settings, "backup_local_dir", "/nonexistent-dir-xyz",
        raising=False,
    )
    assert _latest_backup() is None


# --- ранние выходы ----------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_returns_config(monkeypatch) -> None:
    monkeypatch.setattr(
        backup_verify.settings, "backup_verify_enabled", False, raising=False
    )
    result = await verify_latest_backup()
    assert result.ok is False
    assert result.fail_kind == "config"


@pytest.mark.asyncio
async def test_no_backup_file_returns_no_backup(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        backup_verify.settings, "backup_verify_enabled", True, raising=False
    )
    monkeypatch.setattr(
        backup_verify.settings, "backup_local_dir", str(tmp_path), raising=False
    )
    result = await verify_latest_backup()
    assert result.ok is False
    assert result.fail_kind == "no_backup"


# --- главный сценарий: фраза ------------------------------------------


@pytest.fixture
def verify_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        backup_verify.settings, "backup_verify_enabled", True, raising=False
    )
    monkeypatch.setattr(
        backup_verify.settings, "backup_local_dir", str(tmp_path), raising=False
    )
    monkeypatch.setattr(
        backup_verify.settings, "backup_gpg_passphrase", "x" * 16, raising=False
    )
    return tmp_path


@pytest.mark.asyncio
async def test_encrypted_backup_without_passphrase_is_decrypt_fail(
    verify_env,
) -> None:
    """Зашифрованный дамп + пустая BACKUP_GPG_PASSPHRASE — ровно та
    катастрофа, ради которой проверка написана: копии есть, открыть
    нечем."""
    import aemr_bot.services.backup_verify as bv

    bv.settings.backup_gpg_passphrase = ""
    (verify_env / "aemr-20260701-030000.sql.gpg").write_bytes(b"encrypted")

    result = await verify_latest_backup()
    assert result.ok is False
    assert result.fail_kind == "decrypt"
    assert "BACKUP_GPG_PASSPHRASE" in result.fail_detail


@pytest.mark.asyncio
async def test_wrong_passphrase_reported_as_decrypt(verify_env, monkeypatch) -> None:
    """gpg вернул ненулевой код — фраза не подходит либо файл обрезан
    (не сошёлся контроль целостности). Обе беды в одной категории:
    оператору в любом случае надо снимать свежий бэкап."""
    (verify_env / "aemr-20260701-030000.sql.gpg").write_bytes(b"enc")

    async def boom(src, passphrase, *, timeout):
        raise RuntimeError("gpg вернул код 2: парольная фраза не подходит")

    monkeypatch.setattr(backup_verify, "_decrypt_to_devnull", boom)

    result = await verify_latest_backup()
    assert result.ok is False
    assert result.fail_kind == "decrypt"
    assert "не подходит" in result.fail_detail


@pytest.mark.asyncio
async def test_happy_path_encrypted(verify_env, monkeypatch) -> None:
    src = verify_env / "aemr-20260701-030000.sql.gpg"
    src.write_bytes(b"enc" * 100)

    async def fake_decrypt(path, passphrase, *, timeout):
        return DUMP_HEAD, 50_000

    monkeypatch.setattr(backup_verify, "_decrypt_to_devnull", fake_decrypt)

    result = await verify_latest_backup()
    assert result.ok is True
    assert result.backup_name == "aemr-20260701-030000.sql.gpg"
    assert result.decrypted_bytes == 50_000
    assert result.fail_kind == ""


@pytest.mark.asyncio
async def test_decrypts_but_not_a_dump_is_signature_fail(
    verify_env, monkeypatch
) -> None:
    """Расшифровалось, но внутри не дамп — копия испорчена или это чужой
    файл. Молча считать это успехом нельзя."""
    (verify_env / "aemr-20260701-030000.sql.gpg").write_bytes(b"enc")

    async def fake_decrypt(path, passphrase, *, timeout):
        return b"\x00\x01\x02 not a dump at all", 42

    monkeypatch.setattr(backup_verify, "_decrypt_to_devnull", fake_decrypt)

    result = await verify_latest_backup()
    assert result.ok is False
    assert result.fail_kind == "signature"


@pytest.mark.asyncio
async def test_plain_backup_read_without_gpg(verify_env, monkeypatch) -> None:
    """Незашифрованный дамп (.sql) читается напрямую, gpg не зовём."""
    (verify_env / "aemr-20260701-030000.sql").write_bytes(DUMP_HEAD + b"x" * 500)

    async def never(*a, **kw):
        raise AssertionError("gpg не должен вызываться для .sql")

    monkeypatch.setattr(backup_verify, "_decrypt_to_devnull", never)

    result = await verify_latest_backup()
    assert result.ok is True
    assert result.decrypted_bytes == len(DUMP_HEAD) + 500


@pytest.mark.asyncio
async def test_truncated_plain_dump_fails_signature(verify_env) -> None:
    (verify_env / "aemr-20260701-030000.sql").write_bytes(b"")
    result = await verify_latest_backup()
    assert result.ok is False
    assert result.fail_kind == "signature"


@pytest.mark.asyncio
async def test_any_decrypt_error_is_decrypt_not_unknown(
    verify_env, monkeypatch
) -> None:
    """Что бы ни сломалось на расшифровке — вывод для оператора один:
    открыть копию не удалось. Не размазываем это по категориям."""
    (verify_env / "aemr-20260701-030000.sql.gpg").write_bytes(b"enc")

    async def boom(path, passphrase, *, timeout):
        raise MemoryError("внезапно")

    monkeypatch.setattr(backup_verify, "_decrypt_to_devnull", boom)

    result = await verify_latest_backup()
    assert result.ok is False
    assert result.fail_kind == "decrypt"


@pytest.mark.asyncio
async def test_unexpected_error_outside_decrypt_is_unknown(
    verify_env, monkeypatch
) -> None:
    """Поломка вне расшифровки (чтение plain-файла) — «unknown»."""
    (verify_env / "aemr-20260701-030000.sql").write_bytes(DUMP_HEAD)

    async def boom(src):
        raise OSError("диск отвалился")

    monkeypatch.setattr(backup_verify, "_read_plain_head", boom)

    result = await verify_latest_backup()
    assert result.ok is False
    assert result.fail_kind == "unknown"


@pytest.mark.asyncio
async def test_file_rotated_away_mid_check_does_not_raise(
    verify_env, monkeypatch
) -> None:
    """Ротация снесла файл между поиском и чтением — возвращаем результат,
    а не исключение (cron-обёртка ждёт результат)."""
    ghost = verify_env / "aemr-20260701-030000.sql.gpg"
    ghost.write_bytes(b"enc")

    real_latest = backup_verify._latest_backup

    def latest_then_delete():
        found = real_latest()
        if found is not None:
            found.unlink()  # исчез ровно перед stat()
        return found

    monkeypatch.setattr(backup_verify, "_latest_backup", latest_then_delete)

    result = await verify_latest_backup()
    assert result.ok is False
    assert result.fail_kind == "no_backup"
    assert "исчез" in result.fail_detail


# --- поток ------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_keeps_head_and_counts_all() -> None:
    """Поток вычитывается целиком (иначе gpg встанет на переполненном
    pipe), но в память кладём только голову."""
    class FakeStream:
        def __init__(self) -> None:
            self.chunks = [b"A" * 10, b"B" * 10, b""]
            self.i = 0

        async def read(self, n: int) -> bytes:
            chunk = self.chunks[self.i]
            self.i += 1
            return chunk

    head, total = await backup_verify._drain(FakeStream(), keep_head=15)
    assert total == 20
    assert head == b"A" * 10 + b"B" * 5
    assert len(head) == 15


def test_signature_constant_matches_pg_dump_output() -> None:
    assert PG_DUMP_SIGNATURE in DUMP_HEAD


def test_verify_result_defaults() -> None:
    r = VerifyResult(ok=False, fail_kind="decrypt", fail_detail="нет фразы")
    assert r.backup_name == ""
    assert r.size_bytes == 0
    assert r.decrypted_bytes == 0
