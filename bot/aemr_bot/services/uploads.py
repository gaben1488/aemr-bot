"""Upload helpers around bot.upload_media + file attachment serialization.

MAX expects a two-step flow (Макс.docx §8): get an upload URL, push the file
bytes to it, then attach `{type, payload: {token}}` to a message. The maxapi
library wraps that as `bot.upload_media(InputMedia | InputMediaBuffer)`,
returning an AttachmentUpload with `.payload.token`.

Both PRIVACY.pdf (cached at startup) and the on-demand XLSX exports use this
module so the call shape stays consistent in one place.
"""

from __future__ import annotations

import logging
from pathlib import Path
from tempfile import NamedTemporaryFile

log = logging.getLogger(__name__)


async def upload_path(bot, path: Path) -> str | None:
    """Upload a file from disk. Returns the MAX file token, or None on failure."""
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
    """Upload an in-memory blob. Writes to a temp file because InputMedia
    in this library version takes a path; deletes it after upload."""
    try:
        from maxapi.enums.upload_type import UploadType
        from maxapi.types.input_media import InputMediaBuffer
    except Exception:
        log.exception("maxapi upload symbols unavailable")
        return None

    # Prefer InputMediaBuffer if the version exposes a usable signature;
    # fall back to a temp file otherwise.
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


def file_attachment(token: str) -> dict:
    """Serialize a file-type attachment for /messages."""
    return {"type": "file", "payload": {"token": token}}
