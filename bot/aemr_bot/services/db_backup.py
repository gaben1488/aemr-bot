"""Резервное копирование Postgres БД: pg_dump → опционально gpg →
локальный том → опционально S3.

Выделено из services/cron.py этапом 4 рефакторинга. cron.py импортирует
из этого модуля только `backup_db()` для job _job_backup_with_alert.

Архитектура:
- _build_pg_env() — переменные окружения PG* для pg_dump
- _rotate_backups() — ротация старых файлов
- _run_pg_dump() — простой pg_dump → файл (без шифрования)
- _run_pg_dump_encrypted() — pg_dump | gpg --symmetric → файл
- _upload_to_s3() — опциональная загрузка в S3 через rclone
- backup_db() — главная функция; возвращает Path или None
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from aemr_bot.config import settings

log = logging.getLogger(__name__)
TZ = ZoneInfo(settings.timezone)


def _build_pg_env() -> dict[str, str]:
    """Подготовить env-переменные PG* для pg_dump из DATABASE_URL.
    pg_dump читает их вместо argv-флагов — пароль не утекает через
    /proc/<pid>/cmdline.
    """
    parsed = urlparse(settings.database_url.replace("+asyncpg", ""))
    return {
        **os.environ,
        "PGHOST": parsed.hostname or "localhost",
        "PGPORT": str(parsed.port or 5432),
        "PGUSER": parsed.username or "",
        "PGPASSWORD": parsed.password or "",
        "PGDATABASE": (parsed.path or "/").lstrip("/"),
    }


def _rotate_backups(directory: Path, keep: int, suffix: str) -> None:
    """Удалить самые старые файлы бэкапов сверх `keep`.
    Сортировка по mtime по убыванию.
    """
    files = sorted(
        directory.glob(f"aemr-*{suffix}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in files[keep:]:
        try:
            old.unlink()
            log.info("backup rotated: removed %s", old.name)
        except Exception:
            log.warning("failed to remove old backup %s", old, exc_info=True)


async def _run_pg_dump(out_path: Path, env: dict[str, str]) -> None:
    """`pg_dump > out_path` через asyncio.subprocess. Используется когда
    шифрование gpg выключено.
    """
    f = await asyncio.to_thread(open, out_path, "wb")
    try:
        proc = await asyncio.create_subprocess_exec(
            "pg_dump", "--no-owner", "--no-acl",
            stdout=f,
            env=env,
        )
        rc = await proc.wait()
    finally:
        await asyncio.to_thread(f.close)
    if rc != 0:
        raise RuntimeError(f"pg_dump failed with code {rc}")


async def _run_pg_dump_encrypted(
    out_path: Path, env: dict[str, str], passphrase: str
) -> None:
    """`pg_dump | gpg --symmetric > out_path`. Парольная фраза через
    os.pipe, чтобы она не попала в argv и в shell.
    """
    r_fd, w_fd = os.pipe()
    os.write(w_fd, passphrase.encode())
    os.close(w_fd)

    dump = await asyncio.create_subprocess_exec(
        "pg_dump", "--no-owner", "--no-acl",
        stdout=asyncio.subprocess.PIPE,
        env=env,
    )
    if dump.stdout is None:
        os.close(r_fd)
        raise RuntimeError("pg_dump did not provide stdout pipe")
    try:
        gpg = await asyncio.create_subprocess_exec(
            "gpg", "--batch", "--yes",
            "--passphrase-fd", str(r_fd),
            "--symmetric", "--cipher-algo", "AES256",
            "-o", str(out_path),
            stdin=dump.stdout,  # type: ignore[arg-type]
            pass_fds=(r_fd,),
        )
    finally:
        os.close(r_fd)

    gpg_rc, dump_rc = await asyncio.gather(gpg.wait(), dump.wait())
    if gpg_rc != 0:
        raise RuntimeError(f"gpg failed with code {gpg_rc}")
    if dump_rc != 0:
        raise RuntimeError(f"pg_dump failed with code {dump_rc}")


async def _upload_to_s3(out_path: Path) -> None:
    """Опциональная загрузка в S3 через rclone.

    Учётные данные передаются через переменные окружения RCLONE_CONFIG_*,
    а не argv. Форма с argv (`access_key=...`) утекает через `ps` и
    `/proc/<pid>/cmdline` — это совсем не то место, где должны лежать
    секреты. env-форма держит их внутри процесса rclone.
    """
    if not (
        settings.backup_s3_bucket
        and settings.backup_s3_endpoint
        and settings.backup_s3_access_key
        and settings.backup_s3_secret_key
    ):
        return
    env = os.environ.copy()
    env.update({
        "RCLONE_CONFIG_BACKUPS3_TYPE": "s3",
        "RCLONE_CONFIG_BACKUPS3_PROVIDER": "Other",
        "RCLONE_CONFIG_BACKUPS3_ACCESS_KEY_ID": settings.backup_s3_access_key,
        "RCLONE_CONFIG_BACKUPS3_SECRET_ACCESS_KEY": settings.backup_s3_secret_key,
        "RCLONE_CONFIG_BACKUPS3_ENDPOINT": settings.backup_s3_endpoint,
    })
    rclone = await asyncio.create_subprocess_exec(
        "rclone", "copy", str(out_path),
        f"backups3:{settings.backup_s3_bucket}/",
        env=env,
    )
    rc = await rclone.wait()
    if rc != 0:
        raise RuntimeError(f"rclone failed with code {rc}")
    log.info("backup uploaded to s3: %s", out_path.name)


async def backup_db() -> Path | None:
    """Еженедельный pg_dump → опционально gpg → локальный том → опционально S3.

    Сделано под self-hosted: по умолчанию сохраняет только в локальный
    том (`/backups`) с ротацией. S3 и gpg включаются через переменные
    окружения. Возвращает путь к успешно записанному бэкапу либо None
    при сбое. Никогда не выбрасывает исключений — вызывающий код в
    `cron._job_backup_with_alert` проглатывает None и шлёт алёрт.
    """
    local_dir = (
        Path(settings.backup_local_dir) if settings.backup_local_dir else None
    )
    if local_dir is None:
        log.info("backup skipped: BACKUP_LOCAL_DIR is empty")
        return None

    target_dir = local_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    passphrase = settings.backup_gpg_passphrase
    encrypt = bool(passphrase)
    suffix = ".sql.gpg" if encrypt else ".sql"
    ts = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    out = target_dir / f"aemr-{ts}{suffix}"

    env = _build_pg_env()
    try:
        if encrypt:
            await _run_pg_dump_encrypted(out, env, passphrase or "")
        else:
            await _run_pg_dump(out, env)
        # Ужесточить права до 0600. Дамп содержит телефоны пользователей,
        # тексты обращений и операторский audit-лог.
        try:
            os.chmod(out, 0o600)
        except OSError:
            log.warning("could not chmod backup %s to 0600", out.name)
        log.info("backup written: %s (%d bytes)", out.name, out.stat().st_size)
    except Exception:
        log.exception("backup failed during pg_dump")
        try:
            out.unlink(missing_ok=True)
        except Exception:
            pass
        return None

    _rotate_backups(target_dir, settings.backup_keep_count, suffix)

    try:
        await _upload_to_s3(out)
    except Exception:
        log.exception("backup s3 upload failed (local copy still intact)")

    return out
