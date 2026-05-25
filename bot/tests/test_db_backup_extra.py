"""Расширенные тесты services/db_backup — backup_db, _run_pg_dump,
_upload_to_s3 через моки asyncio.subprocess.

Существующий test_db_backup.py покрывает _build_pg_env и _rotate_backups.
Здесь добавляем покрытие async-частей: запуск pg_dump, обёртка backup_db,
условие пропуска S3 без credentials."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from aemr_bot.services import db_backup


class _FakeProc:
    """Минимальный mock asyncio.subprocess.Process."""

    def __init__(self, returncode: int = 0) -> None:
        self._rc = returncode

    async def wait(self) -> int:
        return self._rc


class TestRunPgDump:
    @pytest.mark.asyncio
    async def test_creates_file_and_runs_pg_dump(self, tmp_path: Path) -> None:
        out = tmp_path / "dump.sql"
        env = {"PGHOST": "x"}

        async def fake_create(*args, **kwargs):
            return _FakeProc(returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_create):
            await db_backup._run_pg_dump(out, env)
        # Файл должен быть создан и закрыт без ошибки.
        assert out.exists()

    @pytest.mark.asyncio
    async def test_raises_categorized_on_nonzero_exit(self, tmp_path: Path) -> None:
        """pg_dump exit != 0 → BackupPgDumpError (категоризированное),
        не голое RuntimeError. Категория нужна выше слоем для точного
        admin-алёрта."""
        out = tmp_path / "dump.sql"

        async def fake_create(*args, **kwargs):
            return _FakeProc(returncode=2)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_create):
            with pytest.raises(db_backup.BackupPgDumpError, match="pg_dump failed"):
                await db_backup._run_pg_dump(out, {})


class TestUploadToS3:
    @pytest.mark.asyncio
    async def test_skips_when_credentials_missing(self, tmp_path: Path) -> None:
        """Без access_key/secret_key/endpoint/bucket — тихо ничего не делает."""
        out = tmp_path / "dump.sql"
        out.write_bytes(b"data")
        with patch.object(db_backup.settings, "backup_s3_bucket", ""), \
             patch.object(db_backup.settings, "backup_s3_endpoint", ""), \
             patch.object(db_backup.settings, "backup_s3_access_key", ""), \
             patch.object(db_backup.settings, "backup_s3_secret_key", ""):
            # Не должно бросить и не должно вызывать subprocess.
            with patch("asyncio.create_subprocess_exec") as mock_proc:
                await db_backup._upload_to_s3(out)
            mock_proc.assert_not_called()

    @pytest.mark.asyncio
    async def test_runs_rclone_when_creds_set(self, tmp_path: Path) -> None:
        out = tmp_path / "dump.sql"
        out.write_bytes(b"data")

        called_with = {}

        async def fake_create(*args, **kwargs):
            called_with["args"] = args
            called_with["env"] = kwargs.get("env")
            return _FakeProc(returncode=0)

        with patch.object(db_backup.settings, "backup_s3_bucket", "my-bucket"), \
             patch.object(db_backup.settings, "backup_s3_endpoint", "https://s3.example"), \
             patch.object(db_backup.settings, "backup_s3_access_key", "AKEY"), \
             patch.object(db_backup.settings, "backup_s3_secret_key", "SECRET"):
            with patch("asyncio.create_subprocess_exec", side_effect=fake_create):
                await db_backup._upload_to_s3(out)

        # Команда rclone, в env пробрасываются ключи.
        assert "rclone" in called_with["args"]
        env = called_with["env"]
        assert env["RCLONE_CONFIG_BACKUPS3_ACCESS_KEY_ID"] == "AKEY"
        assert env["RCLONE_CONFIG_BACKUPS3_SECRET_ACCESS_KEY"] == "SECRET"
        # Секреты НЕ должны попадать в argv.
        assert not any("SECRET" in str(a) for a in called_with["args"])

    @pytest.mark.asyncio
    async def test_raises_on_rclone_failure(self, tmp_path: Path) -> None:
        out = tmp_path / "dump.sql"
        out.write_bytes(b"data")

        async def fake_create(*args, **kwargs):
            return _FakeProc(returncode=1)

        with patch.object(db_backup.settings, "backup_s3_bucket", "b"), \
             patch.object(db_backup.settings, "backup_s3_endpoint", "https://s3"), \
             patch.object(db_backup.settings, "backup_s3_access_key", "k"), \
             patch.object(db_backup.settings, "backup_s3_secret_key", "s"):
            with patch("asyncio.create_subprocess_exec", side_effect=fake_create):
                with pytest.raises(RuntimeError, match="rclone failed"):
                    await db_backup._upload_to_s3(out)


class TestBackupDb:
    """Главная функция backup_db с разными сценариями через моки."""

    @pytest.mark.asyncio
    async def test_config_fail_when_no_local_dir(self) -> None:
        with patch.object(db_backup.settings, "backup_local_dir", ""):
            result = await db_backup.backup_db()
        assert result.ok is False
        assert result.fail_kind == "config"
        assert result.path is None
        assert "BACKUP_LOCAL_DIR" in result.fail_detail

    @pytest.mark.asyncio
    async def test_writes_plain_sql_when_no_passphrase(
        self, tmp_path: Path
    ) -> None:
        async def fake_dump(out_path, env):
            # Имитируем запись pg_dump.
            out_path.write_bytes(b"-- dump --")

        with patch.object(db_backup.settings, "backup_local_dir", str(tmp_path)), \
             patch.object(db_backup.settings, "backup_gpg_passphrase", ""), \
             patch.object(db_backup.settings, "backup_allow_unencrypted", True), \
             patch.object(db_backup.settings, "backup_keep_count", 5), \
             patch.object(db_backup.settings, "backup_s3_bucket", ""), \
             patch.object(db_backup.settings, "backup_s3_endpoint", ""), \
             patch.object(db_backup.settings, "backup_s3_access_key", ""), \
             patch.object(db_backup.settings, "backup_s3_secret_key", ""):
            with patch.object(db_backup, "_run_pg_dump", side_effect=fake_dump) as dump, \
                 patch.object(db_backup, "_run_pg_dump_encrypted") as enc, \
                 patch.object(db_backup, "_build_pg_env", return_value={}):
                result = await db_backup.backup_db()
            dump.assert_called_once()
            enc.assert_not_called()
        assert result.ok is True
        assert result.path is not None
        assert result.path.exists()
        # Без passphrase — расширение .sql, не .sql.gpg.
        assert result.path.suffix == ".sql"

    @pytest.mark.asyncio
    async def test_uses_encrypted_path_when_passphrase_long_enough(
        self, tmp_path: Path
    ) -> None:
        async def fake_enc(out_path, env, passphrase):
            out_path.write_bytes(b"encrypted")

        with patch.object(db_backup.settings, "backup_local_dir", str(tmp_path)), \
             patch.object(db_backup.settings, "backup_gpg_passphrase", "very-long-pass-1234"), \
             patch.object(db_backup.settings, "backup_keep_count", 5), \
             patch.object(db_backup.settings, "backup_s3_bucket", ""), \
             patch.object(db_backup.settings, "backup_s3_endpoint", ""), \
             patch.object(db_backup.settings, "backup_s3_access_key", ""), \
             patch.object(db_backup.settings, "backup_s3_secret_key", ""):
            with patch.object(db_backup, "_run_pg_dump_encrypted", side_effect=fake_enc) as enc, \
                 patch.object(db_backup, "_run_pg_dump") as plain, \
                 patch.object(db_backup, "_build_pg_env", return_value={}):
                result = await db_backup.backup_db()
            enc.assert_called_once()
            plain.assert_not_called()
        assert result.ok is True
        assert result.path is not None
        assert result.path.suffix == ".gpg"

    @pytest.mark.asyncio
    async def test_short_passphrase_falls_back_to_plain(
        self, tmp_path: Path
    ) -> None:
        """Passphrase < 12 chars → не шифруем (warning в лог)."""
        async def fake_dump(out_path, env):
            out_path.write_bytes(b"plain")

        with patch.object(db_backup.settings, "backup_local_dir", str(tmp_path)), \
             patch.object(db_backup.settings, "backup_gpg_passphrase", "short"), \
             patch.object(db_backup.settings, "backup_allow_unencrypted", True), \
             patch.object(db_backup.settings, "backup_keep_count", 5), \
             patch.object(db_backup.settings, "backup_s3_bucket", ""), \
             patch.object(db_backup.settings, "backup_s3_endpoint", ""), \
             patch.object(db_backup.settings, "backup_s3_access_key", ""), \
             patch.object(db_backup.settings, "backup_s3_secret_key", ""):
            with patch.object(db_backup, "_run_pg_dump", side_effect=fake_dump) as dump, \
                 patch.object(db_backup, "_run_pg_dump_encrypted") as enc, \
                 patch.object(db_backup, "_build_pg_env", return_value={}):
                result = await db_backup.backup_db()
            dump.assert_called_once()
            enc.assert_not_called()
        assert result.ok is True
        assert result.path is not None
        assert result.path.suffix == ".sql"

    @pytest.mark.asyncio
    async def test_sec2_refuses_plain_without_opt_in(
        self, tmp_path: Path
    ) -> None:
        """SEC #2: пустой passphrase + НЕТ BACKUP_ALLOW_UNENCRYPTED → отказ.
        152-ФЗ: plain-text дамп с PII нельзя писать на диск без явного
        opt-in в dev/local."""
        with patch.object(db_backup.settings, "backup_local_dir", str(tmp_path)), \
             patch.object(db_backup.settings, "backup_gpg_passphrase", ""), \
             patch.object(db_backup.settings, "backup_allow_unencrypted", False):
            with patch.object(db_backup, "_run_pg_dump") as dump:
                result = await db_backup.backup_db()
            dump.assert_not_called()
        assert result.ok is False
        assert result.fail_kind == "config"
        assert "152" in result.fail_detail or "passphrase" in result.fail_detail.lower()

    @pytest.mark.asyncio
    async def test_pg_dump_fail_categorized(
        self, tmp_path: Path
    ) -> None:
        """pg_dump упал с BackupPgDumpError → result.fail_kind='pg_dump',
        путь None, detail содержит код выхода."""
        async def fake_dump_fail(out_path, env):
            raise db_backup.BackupPgDumpError("pg_dump failed with code 5")

        with patch.object(db_backup.settings, "backup_local_dir", str(tmp_path)), \
             patch.object(db_backup.settings, "backup_gpg_passphrase", ""), \
             patch.object(db_backup.settings, "backup_allow_unencrypted", True), \
             patch.object(db_backup.settings, "backup_keep_count", 5):
            with patch.object(db_backup, "_run_pg_dump", side_effect=fake_dump_fail), \
                 patch.object(db_backup, "_build_pg_env", return_value={}):
                result = await db_backup.backup_db()
        assert result.ok is False
        assert result.fail_kind == "pg_dump"
        assert result.path is None
        assert "code 5" in result.fail_detail

    @pytest.mark.asyncio
    async def test_gpg_fail_categorized_and_plain_dump_removed(
        self, tmp_path: Path
    ) -> None:
        """gpg упал → result.fail_kind='gpg'; незашифрованный plain-text
        дамп удалён (152-ФЗ: не оставляем ПДн на диске без шифрования)."""
        async def fake_enc_fail(out_path, env, passphrase):
            # Имитируем частичный plain-дамп, который оставил pg_dump
            out_path.write_bytes(b"-- partial plain dump --")
            raise db_backup.BackupGpgError("gpg failed with code 2")

        with patch.object(db_backup.settings, "backup_local_dir", str(tmp_path)), \
             patch.object(db_backup.settings, "backup_gpg_passphrase", "long-enough-1234"), \
             patch.object(db_backup.settings, "backup_keep_count", 5):
            with patch.object(db_backup, "_run_pg_dump_encrypted",
                              side_effect=fake_enc_fail), \
                 patch.object(db_backup, "_build_pg_env", return_value={}):
                result = await db_backup.backup_db()
        assert result.ok is False
        assert result.fail_kind == "gpg"
        assert "code 2" in result.fail_detail
        # Plain-дамп НЕ должен остаться на диске после gpg-fail.
        leftover = list(tmp_path.iterdir())
        assert leftover == [], f"остался незашифрованный файл: {leftover}"

    @pytest.mark.asyncio
    async def test_unknown_exception_categorized(
        self, tmp_path: Path
    ) -> None:
        """Любая другая ошибка (FS, OS) → fail_kind='unknown'."""
        async def fake_dump_fail(out_path, env):
            raise OSError("disk full")

        with patch.object(db_backup.settings, "backup_local_dir", str(tmp_path)), \
             patch.object(db_backup.settings, "backup_gpg_passphrase", ""), \
             patch.object(db_backup.settings, "backup_allow_unencrypted", True), \
             patch.object(db_backup.settings, "backup_keep_count", 5):
            with patch.object(db_backup, "_run_pg_dump", side_effect=fake_dump_fail), \
                 patch.object(db_backup, "_build_pg_env", return_value={}):
                result = await db_backup.backup_db()
        assert result.ok is False
        assert result.fail_kind == "unknown"
        assert "OSError" in result.fail_detail or "disk full" in result.fail_detail

    @pytest.mark.asyncio
    async def test_s3_upload_failure_does_not_break_local_copy(
        self, tmp_path: Path
    ) -> None:
        """Если S3 upload упал, локальная копия должна остаться, и
        backup_db должен вернуть путь к ней (а не None)."""
        async def fake_dump(out_path, env):
            out_path.write_bytes(b"data")

        async def fake_upload(out_path):
            raise RuntimeError("rclone unreachable")

        with patch.object(db_backup.settings, "backup_local_dir", str(tmp_path)), \
             patch.object(db_backup.settings, "backup_gpg_passphrase", ""), \
             patch.object(db_backup.settings, "backup_allow_unencrypted", True), \
             patch.object(db_backup.settings, "backup_keep_count", 5), \
             patch.object(db_backup.settings, "backup_s3_bucket", ""), \
             patch.object(db_backup.settings, "backup_s3_endpoint", ""), \
             patch.object(db_backup.settings, "backup_s3_access_key", ""), \
             patch.object(db_backup.settings, "backup_s3_secret_key", ""):
            with patch.object(db_backup, "_run_pg_dump", side_effect=fake_dump), \
                 patch.object(db_backup, "_upload_to_s3", side_effect=fake_upload), \
                 patch.object(db_backup, "_build_pg_env", return_value={}):
                result = await db_backup.backup_db()
        assert result.ok is True
        assert result.path is not None
        assert result.path.exists()
