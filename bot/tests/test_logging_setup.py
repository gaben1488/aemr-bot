"""Тесты персистентного логирования (aemr_bot.logging_setup): файл на диске
переживает контейнер, ловит и сторонние логгеры (maxapi), грейсфул без каталога."""

import logging
from logging.handlers import RotatingFileHandler

import pytest

from aemr_bot import logging_setup
from aemr_bot.config import Settings


@pytest.fixture
def restore_root_logger():
    """Снять/вернуть обработчики корневого логгера, чтобы тесты не пачкали глобальное состояние."""
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved:
        root.addHandler(h)
    root.setLevel(level)


def _settings(monkeypatch, **env) -> Settings:
    base = {"BOT_TOKEN": "t", "DATABASE_URL": "sqlite+aiosqlite:///:memory:"}
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, str(v))
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _file_handlers():
    return [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]


def _stdout_handlers():
    # stdout = StreamHandler, который НЕ FileHandler (RotatingFileHandler — подкласс StreamHandler)
    return [
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]


def test_file_logging_writes_to_disk(monkeypatch, tmp_path, restore_root_logger):
    logging_setup.setup_logging(_settings(monkeypatch, LOG_DIR=str(tmp_path)))
    logging.getLogger("aemr_bot").info("проверка записи")
    for h in logging.getLogger().handlers:
        h.flush()
    log_file = tmp_path / "bot.log"
    assert log_file.is_file()
    assert "проверка записи" in log_file.read_text(encoding="utf-8")


def test_file_handler_is_rotating_with_settings(monkeypatch, tmp_path, restore_root_logger):
    logging_setup.setup_logging(
        _settings(monkeypatch, LOG_DIR=str(tmp_path), LOG_FILE_MAX_BYTES="5000", LOG_FILE_BACKUPS="3")
    )
    fhs = _file_handlers()
    assert len(fhs) == 1
    assert fhs[0].maxBytes == 5000
    assert fhs[0].backupCount == 3


def test_captures_third_party_loggers(monkeypatch, tmp_path, restore_root_logger):
    # обработчик на КОРНЕВОМ логгере → в файл попадают и логи библиотеки maxapi
    logging_setup.setup_logging(_settings(monkeypatch, LOG_DIR=str(tmp_path)))
    logging.getLogger("dispatcher").warning("сообщение из maxapi")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "сообщение из maxapi" in (tmp_path / "bot.log").read_text(encoding="utf-8")


def test_no_file_when_log_dir_empty(monkeypatch, restore_root_logger):
    logging_setup.setup_logging(_settings(monkeypatch, LOG_DIR=""))
    assert not _file_handlers()         # файлового обработчика нет
    assert len(_stdout_handlers()) == 1  # stdout остался


def test_graceful_when_dir_unwritable(monkeypatch, tmp_path, restore_root_logger):
    # LOG_DIR ведёт внутрь существующего ФАЙЛА → mkdir даст OSError → не падаем
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    logging_setup.setup_logging(_settings(monkeypatch, LOG_DIR=str(blocker / "sub")))
    assert not _file_handlers()          # файловый обработчик не добавлен
    assert len(_stdout_handlers()) == 1  # stdout работает


def test_idempotent(monkeypatch, tmp_path, restore_root_logger):
    s = _settings(monkeypatch, LOG_DIR=str(tmp_path))
    logging_setup.setup_logging(s)
    logging_setup.setup_logging(s)
    assert len(_file_handlers()) == 1
    assert len(_stdout_handlers()) == 1
