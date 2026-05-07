"""Помощники для загрузки файлов поверх bot.upload_media и сериализации
файлового вложения.

MAX ждёт двухшагового потока (Макс.docx §8): получить URL для загрузки,
отправить туда байты файла, затем приложить `{type, payload: {token}}` к
сообщению. Библиотека maxapi оборачивает это в
`bot.upload_media(InputMedia | InputMediaBuffer)`, возвращая
AttachmentUpload с `.payload.token`.

И PRIVACY.pdf (кэшируется при старте), и XLSX-выгрузки по запросу
используют этот модуль, чтобы форма вызова была одинаковой в одном месте.
"""

from __future__ import annotations

import logging
from pathlib import Path
from tempfile import NamedTemporaryFile

log = logging.getLogger(__name__)


async def upload_path(bot, path: Path) -> str | None:
    """Загрузить файл с диска. Возвращает файловый токен MAX или None при сбое."""
    try:
        from maxapi.enums.upload_type import UploadType
        from maxapi.types.input_media import InputMedia
    except Exception:
        log.exception("maxapi upload symbols unavailable")
        return None

    try:
        media = InputMedia(path=str(path), type=UploadType.FILE)
        result = await bot.upload_media(media)
    except Exception:
        log.exception("upload_media failed for %s", path)
        return None

    payload = getattr(result, "payload", None)
    token = getattr(payload, "token", None) if payload is not None else None
    return str(token) if token else None


async def upload_bytes(bot, content: bytes, suffix: str = ".bin") -> str | None:
    """Загрузить блоб из памяти. Пишет во временный файл, потому что в
    этой версии библиотеки InputMedia принимает путь; удаляет файл
    после загрузки."""
    try:
        from maxapi.enums.upload_type import UploadType
        from maxapi.types.input_media import InputMediaBuffer
    except Exception:
        log.exception("maxapi upload symbols unavailable")
        return None

    # Предпочитаем InputMediaBuffer, если в версии есть рабочая сигнатура.
    # Иначе откатываемся на временный файл.
    if InputMediaBuffer is not None:
        try:
            media = InputMediaBuffer(buffer=content, type=UploadType.FILE)
            result = await bot.upload_media(media)
            payload = getattr(result, "payload", None)
            token = getattr(payload, "token", None) if payload is not None else None
            if token:
                return str(token)
        except TypeError:
            pass
        except Exception:
            log.exception("InputMediaBuffer upload failed; falling back to disk")

    with NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        return await upload_path(bot, tmp_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def file_attachment(token: str):
    """Собрать вложение типа file для `bot.send_message(attachments=...)`.

    Прежние версии этой функции возвращали обычный dict. На новых
    релизах maxapi (где send_message специально обрабатывает dict-элементы)
    это работало, но в версии, закреплённой в нашем контейнере, падало с
    `AttributeError: 'dict' object has no attribute 'model_dump'`: та
    версия итерирует вложения и безусловно зовёт `att.model_dump()`,
    то есть каждый элемент должен быть моделью Pydantic. Возврат
    AttachmentUpload напрямую работает в обоих случаях. send_message
    либо вызывает `att.model_dump()` (старый путь), либо распознаёт его
    как AttachmentUpload через isinstance (новый путь).
    """
    from maxapi.enums.upload_type import UploadType
    from maxapi.types.attachments.upload import AttachmentPayload, AttachmentUpload

    return AttachmentUpload(
        type=UploadType.FILE,
        payload=AttachmentPayload(token=token),
    )
