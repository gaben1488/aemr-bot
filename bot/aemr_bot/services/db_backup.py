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
    os.pipe, чтобы она не попала в argv и в shell. Сами процессы
    соединяем через ОС-pipe (Unix way) — asyncio StreamReader как
    stdin не работает в Python 3.12 (нет .fileno() у StreamReader).
    """
    # Pipe для passphrase. Запись делаем через executor, потому что
    # синхронный os.write блокирует event-loop: при passphrase >64KB
    # или забитом OS pipe-буфере (если gpg ещё не запущен и не читает
    # на той стороне) write зависнет, а с ним замрёт весь бот. Для
    # типичных 30-символьных паролей буфер заведомо больше — но это
    # не повод полагаться на «обычно норм».
    pp_r, pp_w = os.pipe()
    try:
        await asyncio.to_thread(os.write, pp_w, passphrase.encode() + b"\n")
    finally:
        os.close(pp_w)

    # Pipe для данных pg_dump → gpg
    data_r, data_w = os.pipe()

    try:
        dump = await asyncio.create_subprocess_exec(
            "pg_dump", "--no-owner", "--no-acl",
            stdout=data_w,
            env=env,
            pass_fds=(data_w,),
        )
    except Exception:
        os.close(pp_r)
        os.close(data_r)
        os.close(data_w)
        raise
    # write-конец data-pipe больше не нужен в нашем процессе:
    # его держит pg_dump.
    os.close(data_w)

    # Контейнер с read_only: true — у gpg нет права создавать
    # ~/.gnupg для своих ключей. Перенаправляем HOMEDIR в TMPDIR
    # (контейнер монтирует tmpfs:/tmp:128m), что безопасно: tmpfs
    # видна только нашему процессу, и режим 0o700 закрывает доступ
    # другим UID. Жёсткое имя «.gnupg» под TMPDIR — стандартный
    # путь gpg-инсталляции.
    gpg_home = os.path.join(os.environ.get("TMPDIR", "/tmp"), ".gnupg")  # nosec
    os.makedirs(gpg_home, mode=0o700, exist_ok=True)

    try:
        gpg = await asyncio.create_subprocess_exec(
            "gpg",
            "--homedir", gpg_home,
            "--batch", "--yes",
            "--passphrase-fd", str(pp_r),
            "--symmetric", "--cipher-algo", "AES256",
            "-o", str(out_path),
            stdin=data_r,
            pass_fds=(pp_r, data_r),
        )
    finally:
        # read-концы держат gpg-дочка; в родителе закрываем.
        os.close(pp_r)
        os.close(data_r)

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

    # Параноидальная проверка: пустая строка в env "" даст truthy False,
    # но если кто-то поставит passphrase из 1 символа — gpg запустится
    # с тривиально расшифровываемым ключом и создаст формально
    # зашифрованный файл, который легко вскрыть
    # с расширением .sql.gpg. Минимум 12 символов — иначе бэкап без
    # шифрования с предупреждением в лог.
    passphrase = (settings.backup_gpg_passphrase or "").strip()
    if passphrase and len(passphrase) < 12:
        log.error(
            "BACKUP_GPG_PASSPHRASE длиной %d — слишком короткая для AES-256. "
            "Бэкап НЕ зашифрован. Установите фразу ≥12 символов.",
            len(passphrase),
        )
        passphrase = ""  # nosec B105 - это сброс небезопасной фразы, не секрет.
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
            log.warning("не удалось удалить неполный файл бэкапа %s", out.name, exc_info=True)
        return None

    _rotate_backups(target_dir, settings.backup_keep_count, suffix)

    try:
        await _upload_to_s3(out)
    except Exception:
        log.exception("backup s3 upload failed (local copy still intact)")

    return out
