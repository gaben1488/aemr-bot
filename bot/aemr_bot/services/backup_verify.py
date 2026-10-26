"""Проверка ПРИГОДНОСТИ бэкапа: расшифровать поток и заглянуть внутрь.

Еженедельный `db_backup.backup_db` доказывает, что файл записался. Он не
доказывает, что файл можно ОТКРЫТЬ. Две вещи ломаются молча:

1. **Парольная фраза.** `BACKUP_GPG_PASSPHRASE` живёт только в `.env` на
   сервере. Если её перезапишут (перенос сервера, «почистили» .env) —
   все 8 хранимых копий разом превращаются в мусор, и выяснится это в
   аварию, когда восстанавливаться уже нужно, а нечем.
2. **Целостность файла.** Оборванный pipe или кончившееся место дают
   обрезанный дамп; на диске он выглядит как обычный бэкап.

Обе ловятся дёшево: `gpg --decrypt` в поток, читая вывод и выбрасывая
его. Расшифровка проверяет фразу, а встроенный в GPG контроль целостности
(MDC) — что файл не обрезан и не повреждён: повреждение даёт ненулевой
код возврата. Первые байты сверяем с сигнатурой pg_dump, чтобы убедиться,
что внутри действительно дамп.

**Почему НЕ восстановление в реальную БД.** Это проверило бы больше, но
требует места под распаковку (в контейнере /tmp — tmpfs 128 МБ, дамп
туда может не влезть) и создаёт временную базу на том же диске, что и
боевая. Полную проверку — развернуть копию до конца — администратор
проходит руками раз в квартал по регламенту (раздел «Резервное
копирование» в вике). Здесь — дешёвый сторож, который может работать
хоть каждую неделю и ничем не рискует.

Поток не пишется на диск: расшифрованный дамп — это открытые ПДн, и
единственное безопасное место для них — ничто.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from aemr_bot.config import settings

log = logging.getLogger(__name__)

# Сигнатура plain-SQL дампа. pg_dump открывает вывод строкой
# «-- PostgreSQL database dump». Ищем в первом прочитанном куске: если её
# нет — расшифровалось что-то, но не дамп.
PG_DUMP_SIGNATURE = b"PostgreSQL database dump"

# Сколько первых байт держим для сверки сигнатуры. Остальное читаем и
# сразу выбрасываем — в память не копим (дамп может быть в сотни МБ).
_HEAD_BYTES = 4096
_CHUNK = 256 * 1024


@dataclass(frozen=True)
class VerifyResult:
    """Итог проверки.

    `ok=True` — фраза подошла, файл целый, внутри дамп.
    Иначе `fail_kind`:
    - "config"    — проверка выключена или BACKUP_LOCAL_DIR пуст;
    - "no_backup" — в каталоге нет ни одного файла бэкапа;
    - "decrypt"   — gpg не расшифровал: ГЛАВНОЕ — фраза не подходит,
      либо файл повреждён/обрезан (MDC не сошёлся);
    - "signature" — расшифровалось, но это не дамп pg_dump;
    - "unknown"   — прочее.
    """
    ok: bool
    fail_kind: str = ""
    fail_detail: str = ""
    backup_name: str = ""
    size_bytes: int = 0
    decrypted_bytes: int = 0


def _latest_backup() -> Path | None:
    """Свежайший файл бэкапа в BACKUP_LOCAL_DIR (по времени изменения)."""
    if not settings.backup_local_dir:
        return None
    directory = Path(settings.backup_local_dir)
    if not directory.is_dir():
        return None
    files = [
        p for p in directory.glob("aemr-*")
        if p.is_file() and (p.name.endswith(".sql") or p.name.endswith(".sql.gpg"))
    ]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


async def _drain(stream, *, keep_head: int) -> tuple[bytes, int]:
    """Вычитать поток до конца, сохранив только первые `keep_head` байт.

    Читать поток ОБЯЗАТЕЛЬНО: если этого не делать, gpg заблокируется на
    записи в переполненный pipe и проверка зависнет. Возвращает
    (первые байты, всего прочитано).
    """
    head = b""
    total = 0
    while True:
        chunk = await stream.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if len(head) < keep_head:
            head += chunk[: keep_head - len(head)]
    return head, total


async def _decrypt_to_devnull(
    src: Path, passphrase: str, *, timeout: float
) -> tuple[bytes, int]:
    """`gpg --decrypt src` в поток; на диск ничего не пишем.

    Passphrase уходит через stdin pipe, не через argv — иначе она утечёт
    в /proc/<pid>/cmdline.
    Возвращает (первые байты дампа, размер расшифрованного).
    """
    # Контейнер read_only: приватный homedir в TMPDIR, как при создании копии.
    # nosec B108: путь не пользовательский, задаём его мы сами.
    gpg_home = os.path.join(os.environ.get("TMPDIR", "/tmp"), ".gnupg")  # nosec B108
    proc = None
    try:
        # Один бюджет на запуск, передачу фразы, чтение stdout и завершение.
        # Отдельный таймаут только на wait() не ограничивал зависший stdout.
        async with asyncio.timeout(timeout):
            os.makedirs(gpg_home, mode=0o700, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                "gpg",
                "--homedir", gpg_home,
                "--batch", "--yes",
                "--passphrase-fd", "0",
                "--decrypt", str(src),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            # stdin — отдельный pipe для фразы; она не попадает в argv/на диск.
            # Записываем после spawn: длинная фраза не заблокирует pipe до
            # появления читателя. drain() тоже входит в общий таймаут.
            if proc.stdin is None:
                raise RuntimeError("gpg stdin pipe unavailable")
            proc.stdin.write(passphrase.encode() + b"\n")
            await proc.stdin.drain()
            proc.stdin.close()
            head, total = await _drain(proc.stdout, keep_head=_HEAD_BYTES)
            rc = await proc.wait()
    except BaseException as exc:
        # CancelledError тоже требует kill+reap; отмену затем передаём выше.
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass  # процесс уже завершился между ошибкой и kill
            try:
                async with asyncio.timeout(10):
                    # После kill дочитываем pipe параллельно с wait: иначе
                    # заполненный StreamReader может удерживать transport.
                    await asyncio.gather(
                        _drain(proc.stdout, keep_head=0), proc.wait(),
                    )
            except TimeoutError:
                log.warning("backup verify: gpg не отозвался на kill за 10с")
        if isinstance(exc, TimeoutError):
            raise TimeoutError(f"gpg --decrypt timed out after {timeout:g}s") from exc
        raise

    if rc != 0:
        raise RuntimeError(
            f"gpg вернул код {rc}: парольная фраза не подходит либо файл "
            f"повреждён/обрезан (не сошёлся контроль целостности)"
        )
    return head, total


async def _read_plain_head(src: Path) -> tuple[bytes, int]:
    """Незашифрованный дамп: прочитать голову и узнать размер."""
    def _read() -> tuple[bytes, int]:
        with src.open("rb") as f:
            head = f.read(_HEAD_BYTES)
        return head, src.stat().st_size

    return await asyncio.to_thread(_read)


async def verify_latest_backup() -> VerifyResult:
    """Проверить пригодность свежайшего бэкапа. Исключений не бросает."""
    if not settings.backup_verify_enabled:
        return VerifyResult(
            ok=False, fail_kind="config",
            fail_detail="BACKUP_VERIFY_ENABLED=0 — проверка выключена.",
        )
    if not settings.backup_local_dir:
        return VerifyResult(
            ok=False, fail_kind="config",
            fail_detail="BACKUP_LOCAL_DIR пуст — нечего проверять.",
        )

    src = _latest_backup()
    if src is None:
        return VerifyResult(
            ok=False, fail_kind="no_backup",
            fail_detail=(
                f"в {settings.backup_local_dir} нет ни одного файла "
                f"aemr-*.sql[.gpg] — бэкап ни разу не снимался?"
            ),
        )

    # Файл мог исчезнуть между поиском и чтением: ротация (_rotate_backups)
    # сносит лишние копии в другом потоке. Это не поломка бэкапа — просто
    # проверять уже нечего, поэтому не даём исключению улететь наружу
    # (контракт: функция не бросает, cron-обёртка ждёт результат).
    try:
        size = src.stat().st_size
    except OSError as e:
        return VerifyResult(
            ok=False, fail_kind="no_backup", backup_name=src.name,
            fail_detail=f"файл {src.name} исчез до проверки (ротация?): {e}",
        )

    try:
        if src.name.endswith(".gpg"):
            passphrase = (settings.backup_gpg_passphrase or "").strip()
            if not passphrase:
                return VerifyResult(
                    ok=False, fail_kind="decrypt", backup_name=src.name,
                    size_bytes=size,
                    fail_detail=(
                        "бэкап зашифрован, а BACKUP_GPG_PASSPHRASE пуст — "
                        "открыть копии нечем. Так и выглядит потеря доступа "
                        "ко всему архиву."
                    ),
                )
            try:
                head, plain_size = await _decrypt_to_devnull(
                    src, passphrase, timeout=settings.backup_verify_timeout_seconds
                )
            except Exception as e:
                return VerifyResult(
                    ok=False, fail_kind="decrypt", backup_name=src.name,
                    size_bytes=size, fail_detail=str(e),
                )
        else:
            head, plain_size = await _read_plain_head(src)

        if PG_DUMP_SIGNATURE not in head:
            return VerifyResult(
                ok=False, fail_kind="signature", backup_name=src.name,
                size_bytes=size, decrypted_bytes=plain_size,
                fail_detail=(
                    "файл открылся, но внутри не дамп pg_dump (нет сигнатуры "
                    "в начале) — похоже, копия испорчена или это чужой файл"
                ),
            )

        log.info(
            "backup verify OK: %s (%d КБ на диске, %d КБ после распаковки)",
            src.name, size // 1024, plain_size // 1024,
        )
        return VerifyResult(
            ok=True, backup_name=src.name, size_bytes=size,
            decrypted_bytes=plain_size,
        )

    except Exception as e:
        log.exception("backup verify: непредвиденная ошибка")
        return VerifyResult(
            ok=False, fail_kind="unknown", backup_name=src.name,
            size_bytes=size, fail_detail=str(e),
        )
