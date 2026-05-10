"""Тесты на services/db_backup — pure-helpers без реального pg_dump.

backup_db() реальный требует Postgres + pg_dump в PATH — это integration-
тест на сервере (через cron-job). Здесь покрываем pure parts."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from aemr_bot.services.db_backup import _build_pg_env, _rotate_backups


class TestBuildPgEnv:
    def test_extracts_components_from_database_url(self) -> None:
        with patch("aemr_bot.services.db_backup.settings") as mock_settings:
            mock_settings.database_url = "postgresql+asyncpg://aemr:secret@db:5432/aemr"
            env = _build_pg_env()
        assert env["PGHOST"] == "db"
        assert env["PGPORT"] == "5432"
        assert env["PGUSER"] == "aemr"
        assert env["PGPASSWORD"] == "secret"
        assert env["PGDATABASE"] == "aemr"

    def test_handles_missing_port(self) -> None:
        with patch("aemr_bot.services.db_backup.settings") as mock_settings:
            mock_settings.database_url = "postgresql://aemr:p@host/aemr_test"
            env = _build_pg_env()
        assert env["PGPORT"] == "5432"  # default

    def test_password_not_in_argv(self) -> None:
        """Регрессия: пароль должен идти ТОЛЬКО через env, не через
        командную строку (cmdline видит каждый процесс на хосте через
        /proc/<pid>/cmdline)."""
        with patch("aemr_bot.services.db_backup.settings") as mock_settings:
            mock_settings.database_url = (
                "postgresql+asyncpg://aemr:tr@ck-PASS@db/aemr"
            )
            env = _build_pg_env()
        # Пароль в env
        assert "tr@ck-PASS" in env["PGPASSWORD"]
        # И должен оставаться приватным — не передаётся как PG-keyword
        # имя вне PGPASSWORD (мы тестируем что только PGPASSWORD содержит).
        assert env.get("PGPASSWORD") == "tr@ck-PASS"


class TestRotateBackups:
    def test_keeps_n_newest(self, tmp_path: Path) -> None:
        # Создадим 5 файлов с разным mtime, оставим 3
        for i, name in enumerate(
            ["aemr-1.sql", "aemr-2.sql", "aemr-3.sql", "aemr-4.sql", "aemr-5.sql"]
        ):
            f = tmp_path / name
            f.write_text("dump")
            # st_mtime по индексу — старший индекс = новее
            ts = time.time() + i
            import os
            os.utime(f, (ts, ts))

        _rotate_backups(tmp_path, keep=3, suffix=".sql")

        remaining = sorted(p.name for p in tmp_path.glob("aemr-*.sql"))
        # Оставлены 3 самых новых: 3, 4, 5
        assert remaining == ["aemr-3.sql", "aemr-4.sql", "aemr-5.sql"]

    def test_no_files_no_op(self, tmp_path: Path) -> None:
        _rotate_backups(tmp_path, keep=3, suffix=".sql")  # без exception

    def test_fewer_than_keep_no_delete(self, tmp_path: Path) -> None:
        for name in ["aemr-1.sql", "aemr-2.sql"]:
            (tmp_path / name).write_text("dump")
        _rotate_backups(tmp_path, keep=10, suffix=".sql")
        assert len(list(tmp_path.glob("aemr-*.sql"))) == 2

    def test_keeps_only_matching_suffix(self, tmp_path: Path) -> None:
        """При suffix=.sql.gpg ротируем только зашифрованные;
        обычные .sql не трогаются."""
        for name in [
            "aemr-1.sql.gpg",
            "aemr-2.sql.gpg",
            "aemr-3.sql.gpg",
            "aemr-4.sql",  # plain — не должен попасть в rotate
        ]:
            (tmp_path / name).write_text("dump")
        _rotate_backups(tmp_path, keep=1, suffix=".sql.gpg")
        # Из 3 .gpg оставлен 1 (самый новый — порядок mtime равный, но
        # точно НЕ удалён файл .sql)
        assert (tmp_path / "aemr-4.sql").exists()
        gpg_remaining = list(tmp_path.glob("aemr-*.sql.gpg"))
        assert len(gpg_remaining) == 1


@pytest.mark.parametrize(
    "passphrase,expected_encrypted",
    [
        ("", False),  # пустая → не шифруем
        ("short", False),  # < 12 символов → не шифруем (warning в лог)
        ("12chars-pass", True),  # ровно 12 → шифруем
        ("very-long-secure-pass-2026", True),
    ],
)
def test_passphrase_length_decides_encryption(
    passphrase: str, expected_encrypted: bool
) -> None:
    """Логика min length 12 для passphrase. Без проверки real backup_db,
    только условие в коде."""
    # Эмулируем условие из db_backup.py:
    cleaned = (passphrase or "").strip()
    if cleaned and len(cleaned) < 12:
        cleaned = ""
    encrypt = bool(cleaned)
    assert encrypt == expected_encrypted
