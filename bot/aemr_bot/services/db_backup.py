"""Резервное копирование Postgres БД: pg_dump → опционально gpg →
локальный том → опционально S3.

Выделено из services/cron.py этапом 4 рефакторинга. cron.py импортирует
из этого модуля `backup_db()` (возвращает BackupResult с фактом успеха
и категорией ошибки) для job _job_backup_with_alert.

Архитектура:
- _build_pg_env() — переменные окружения PG* для pg_dump
- _rotate_backups() — ротация старых файлов
- _run_pg_dump() — простой pg_dump → файл (без шифрования)
- _run_pg_dump_encrypted() — pg_dump | gpg --symmetric → файл
- _upload_to_s3() — опциональная загрузка в S3 через rclone
- backup_db() — главная функция; возвращает BackupResult

Категоризированные исключения (BackupPgDumpError / BackupGpgError) дают
вызывающему коду понять, **что именно** упало, чтобы admin-алёрт мог
быть точным («pg_dump упал» vs «дамп сделан, но gpg не зашифровал»).
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from aemr_bot.config import settings

log = logging.getLogger(__name__)
TZ = ZoneInfo(settings.timezone)


class BackupError(Exception):
    """Базовое исключение бэкапа. Не используется напрямую — только
    как родитель для категорированных под-классов."""


class BackupPgDumpError(BackupError):
    """pg_dump упал. Возможные причины: Postgres недоступен, нет места
    на диске, не та схема доступа, нет прав на чтение БД."""


class BackupGpgError(BackupError):
    """gpg-шифрование упало (дамп либо сделан, либо нет — это
    ответственность обработчика, проверять `out_path.exists()`).
    Возможные причины: неверный passphrase, нет места под /tmp,
    отсутствие gpg в контейнере."""


class BackupTimeoutError(BackupError):
    """Дочерний процесс бэкапа (pg_dump / gpg / rclone) не завершился за
    отведённый таймаут и был убит. Причина — обычно повисший внешний
    ресурс: недоступный S3-эндпоинт, зависший Postgres, заблокированный
    диск. Без таймаута `await proc.wait()` висел бы вечно, и cron-job
    никогда не вернул бы BackupResult — а значит, и admin-алёрт о провале
    бэкапа не ушёл бы."""


async def _wait_proc(
    proc: asyncio.subprocess.Process, timeout: float, label: str
) -> int:
    """`await proc.wait()` с таймаутом и гарантированным reap при провале.

    Возвращает код возврата. При превышении `timeout` процесс убивается
    (kill), затем дожидается (reap — иначе остался бы зомби и FD-утечка),
    и бросается BackupTimeoutError. Так зависший pg_dump/gpg/rclone не
    морозит backup-job навечно: вызывающий получит исключение, переведёт
    его в BackupResult(fail_kind=…) и отправит штатный admin-алёрт.
    """
    try:
        return await asyncio.wait_for(proc.wait(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError) as e:
        log.error(
            "backup: %s не завершился за %.0fс — убиваю процесс (pid=%s)",
            label,
            timeout,
            proc.pid,
        )
        try:
            proc.kill()
        except ProcessLookupError:
            pass  # уже умер между таймаутом и kill — ок
        # Reap убитого процесса, чтобы не оставить зомби и не утечь FD.
        # Второй wait_for со скромным потолком — на случай, если kill не
        # подействовал мгновенно (не должен зависнуть, но страхуемся).
        try:
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("backup: %s не отозвался на kill за 10с", label)
        raise BackupTimeoutError(
            f"{label} timed out after {timeout:.0f}s"
        ) from e


@dataclass(frozen=True)
class BackupResult:
    """Итог одной попытки бэкапа.

    `path is not None` ⇔ успех (файл записан, права 0600). Иначе
    `fail_kind` категоризирует причину для точного admin-алёрта.

    Возможные `fail_kind`:
    - "config" — `BACKUP_LOCAL_DIR` пустой (нет, куда писать).
    - "pg_dump" — `pg_dump` упал.
    - "gpg" — `pg_dump` отработал, но `gpg --symmetric` упал.
    - "unknown" — любая другая (включая FS-ошибки записи).

    Поле `fail_detail` — одна строка для логов и admin-алёрта
    (короткое описание, без stack-trace).
    """
    path: Path | None
    fail_kind: str = ""
    fail_detail: str = ""

    @property
    def ok(self) -> bool:
        return self.path is not None


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
        rc = await _wait_proc(
            proc, settings.backup_pg_dump_timeout_seconds, "pg_dump"
        )
    finally:
        await asyncio.to_thread(f.close)
    if rc != 0:
        raise BackupPgDumpError(f"pg_dump failed with code {rc}")


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
    try:
        os.makedirs(gpg_home, mode=0o700, exist_ok=True)
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
    except Exception:
        # pg_dump уже запущен и пишет в data-pipe. Если каталог gpg не
        # создался (TMPDIR занят файлом, нет прав) или сам gpg не поднялся
        # (нет бинаря), надо убить и reap'нуть dump — иначе он висит зомби
        # на EPIPE, а pp_r/data_r утекают. Та же kill+reap дисциплина, что
        # в timeout-ветке ниже.
        try:
            dump.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(dump.wait(), timeout=10.0)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        os.close(pp_r)
        os.close(data_r)
        raise
    # read-концы теперь держит gpg-дочка; в родителе закрываем.
    os.close(pp_r)
    os.close(data_r)

    # pg_dump и gpg работают конкурентно (поток данных течёт по pipe),
    # поэтому даём им один общий настенный бюджет. Берём больший из двух
    # таймаутов — этого хватает обоим. По таймауту убиваем И reap'аем оба
    # процесса (иначе зомби + утечка FD), затем бросаем BackupTimeoutError,
    # чтобы backup_db вернул BackupResult(fail_kind="unknown") и ушёл
    # штатный admin-алёрт. Без этого голый gather висел бы вечно при
    # зависшем Postgres или забитом pipe.
    budget = max(
        settings.backup_pg_dump_timeout_seconds,
        settings.backup_gpg_timeout_seconds,
    )
    try:
        gpg_rc, dump_rc = await asyncio.wait_for(
            asyncio.gather(gpg.wait(), dump.wait()), timeout=budget
        )
    except (asyncio.TimeoutError, TimeoutError) as e:
        log.error(
            "backup: pg_dump|gpg не завершились за %.0fс — убиваю оба",
            budget,
        )
        for proc, label in ((gpg, "gpg"), (dump, "pg_dump")):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except (asyncio.TimeoutError, TimeoutError):
                log.warning("backup: %s не отозвался на kill за 10с", label)
        raise BackupTimeoutError(
            f"pg_dump|gpg timed out after {budget:.0f}s"
        ) from e
    # Сначала проверяем pg_dump: если дамп не сделан, gpg-проблема
    # вторична («не было что шифровать»). Это даёт точную категорию
    # для admin-алёрта.
    if dump_rc != 0:
        raise BackupPgDumpError(f"pg_dump failed with code {dump_rc}")
    if gpg_rc != 0:
        raise BackupGpgError(f"gpg failed with code {gpg_rc}")


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
    # Сетевой шаг: повисший/недоступный S3-эндпоинт без таймаута заставил
    # бы rclone (и весь backup-job) ждать вечно. По таймауту убиваем+reap
    # и бросаем BackupTimeoutError. Здесь это не валит результат целиком —
    # backup_db ловит ошибку S3 отдельно (локальный файл уже на диске).
    rc = await _wait_proc(
        rclone, settings.backup_rclone_timeout_seconds, "rclone"
    )
    if rc != 0:
        raise RuntimeError(f"rclone failed with code {rc}")
    log.info("backup uploaded to s3: %s", out_path.name)


async def backup_db() -> BackupResult:
    """Еженедельный pg_dump → опционально gpg → локальный том → опционально S3.

    Сделано под self-hosted: по умолчанию сохраняет только в локальный
    том (`/backups`) с ротацией. S3 и gpg включаются через переменные
    окружения. Возвращает `BackupResult`: при успехе `.ok=True`,
    `.path` указывает на файл; при сбое — `.fail_kind` категоризирует
    причину (`config` / `pg_dump` / `gpg` / `unknown`), `.fail_detail` —
    короткая строка для admin-алёрта.

    Никогда не выбрасывает исключений — вызывающий код в
    `cron._job_backup_with_alert` читает `.fail_kind` и шлёт точный
    алёрт. S3-загрузка считается необязательной: её сбой не валит
    весь результат (локальный файл по-прежнему есть).
    """
    local_dir = (
        Path(settings.backup_local_dir) if settings.backup_local_dir else None
    )
    if local_dir is None:
        log.info("backup skipped: BACKUP_LOCAL_DIR is empty")
        return BackupResult(
            path=None,
            fail_kind="config",
            fail_detail="BACKUP_LOCAL_DIR пуст — нет, куда писать бэкап.",
        )

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
    # SEC #2: блокируем создание plain-text дампа без явного opt-in.
    # Дамп содержит phones / names / texts / operator audit-log — это
    # PII по 152-ФЗ. Хранить на диске или заливать в S3 без шифрования
    # = breach. Для dev/local-only можно поставить
    # BACKUP_ALLOW_UNENCRYPTED=1, но в prod должен быть GPG passphrase.
    if not encrypt and not settings.backup_allow_unencrypted:
        log.error(
            "backup отказан: BACKUP_GPG_PASSPHRASE пуст (или < 12 симв) и "
            "BACKUP_ALLOW_UNENCRYPTED не выставлен. Установите passphrase "
            "≥12 симв ИЛИ явно разрешите plain-text дамп через "
            "BACKUP_ALLOW_UNENCRYPTED=1 (только dev/local!)."
        )
        return BackupResult(
            path=None,
            fail_kind="config",
            fail_detail=(
                "BACKUP_GPG_PASSPHRASE пуст — без шифрования дамп с PII "
                "записывать запрещено (152-ФЗ). Поставьте passphrase ≥12 "
                "симв или BACKUP_ALLOW_UNENCRYPTED=1 для dev."
            ),
        )
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
    except BackupPgDumpError as e:
        log.exception("backup failed: pg_dump")
        try:
            out.unlink(missing_ok=True)
        except Exception:
            log.warning("не удалось удалить неполный файл %s", out.name, exc_info=True)
        return BackupResult(path=None, fail_kind="pg_dump", fail_detail=str(e))
    except BackupGpgError as e:
        log.exception("backup failed: gpg")
        # Незашифрованный дамп удаляем тоже — он мог осесть на диске
        # из частичного gpg-выхода; держать plain-text dump на диске
        # без явной зачистки = риск (152-ФЗ — он содержит ПДн).
        try:
            out.unlink(missing_ok=True)
        except Exception:
            log.warning("не удалось удалить неполный файл %s", out.name, exc_info=True)
        return BackupResult(path=None, fail_kind="gpg", fail_detail=str(e))
    except Exception as e:
        log.exception("backup failed: unknown")
        try:
            out.unlink(missing_ok=True)
        except Exception:
            log.warning("не удалось удалить неполный файл %s", out.name, exc_info=True)
        return BackupResult(
            path=None,
            fail_kind="unknown",
            fail_detail=f"{type(e).__name__}: {str(e)[:150]}",
        )

    _rotate_backups(target_dir, settings.backup_keep_count, suffix)

    try:
        await _upload_to_s3(out)
    except Exception:
        log.exception("backup s3 upload failed (local copy still intact)")

    return BackupResult(path=out)
