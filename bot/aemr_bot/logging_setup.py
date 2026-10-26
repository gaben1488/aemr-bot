"""Настройка логирования: stdout + персистентный ротируемый файл на диске.

Зачем файл, а не только stdout: контейнерные логи json-file драйвера удаляются
вместе с контейнером (docker compose down / podman rm) — и история теряется.
Файл логов лежит на смонтированном с хоста каталоге (LOG_DIR, по умолчанию
/var/log/aemr-bot), поэтому переживает остановку, удаление и пересборку контейнера.

Обработчики вешаются на КОРНЕВОЙ логгер, поэтому в файл попадает ВСЁ
Python-логирование — не только наш `aemr_bot`, но и библиотека maxapi
(логгеры dispatcher / bot / connection). Контейнерный stdout дополнительно
сохраняется драйвером journald (см. docker-compose.yml / Quadlet-юниты) —
это host-level журнал, тоже переживает удаление контейнера и ловит то, что
до Python-логгера не доходит (вывод alembic при старте, аварийные трейсбэки).
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aemr_bot.config import Settings, settings

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_LOG_FILE_NAME = "bot.log"


class _MaxUpdateDiagnosticsFilter(logging.Filter):
    """Keep SDK parser failure signals without copying resident payloads to logs.

    maxapi 1.2.3 logs complete unrecognized/malformed update JSON and may attach
    validation exceptions containing that JSON. Filter only that SDK module;
    unrelated dispatcher warnings and application logs remain available.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "dispatcher" and record.module == "getted_updates":
            error = "unknown-or-malformed-update"
            if record.exc_info and record.exc_info[0] is not None:
                error = record.exc_info[0].__name__
            record.msg = "MAX: событие пропущено при разборе (%s); payload не записывается"
            record.args = (error,)
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def setup_logging(s: Settings | None = None) -> None:
    """Поднять логирование: stdout всегда + файл на диске, если задан и доступен LOG_DIR.

    Идемпотентна: повторный вызов не плодит обработчики. Если каталог логов не
    примонтирован или нет прав — не падаем, остаёмся на stdout с предупреждением
    (важно для dev/CI, где /var/log/aemr-bot не существует).
    """
    s = s or settings
    sdk_log = logging.getLogger("dispatcher")
    if not any(isinstance(f, _MaxUpdateDiagnosticsFilter) for f in sdk_log.filters):
        sdk_log.addFilter(_MaxUpdateDiagnosticsFilter())
    formatter = logging.Formatter(_FORMAT)
    root = logging.getLogger()
    root.setLevel(s.log_level)
    for handler in list(root.handlers):  # идемпотентность: снять прежние обработчики
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    log = logging.getLogger("aemr_bot")
    if not s.log_dir:
        log.info("logging: stdout (файл выключен, LOG_DIR пуст)")
        return

    try:
        log_path = Path(s.log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path / _LOG_FILE_NAME,
            maxBytes=s.log_file_max_bytes,
            backupCount=s.log_file_backups,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
        log.info(
            "logging: файл %s (ротация %d × %.0f МБ) + stdout",
            log_path / _LOG_FILE_NAME,
            s.log_file_backups,
            s.log_file_max_bytes / 1_000_000,
        )
    except OSError as exc:
        # каталог не примонтирован с хоста / нет прав у UID 1000 — не критично,
        # бот продолжает писать в stdout (его подхватит journald-драйвер).
        log.warning("logging: файл логов отключён — %s недоступен (%s)", s.log_dir, exc)
