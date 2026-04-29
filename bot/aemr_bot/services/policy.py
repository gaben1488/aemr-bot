"""Upload privacy-policy PDF to MAX once and reuse the token.

MAX API flow (verified against love-apples/maxapi/bot.py):
  1. POST /uploads?type=file → temporary upload URL
  2. PUT/POST file bytes to that URL
  3. Server returns a token; pass it as attachment payload in send_message

We hide all of that behind bot.upload_media(InputMedia | InputMediaBuffer)
which returns AttachmentUpload (type, payload.token). We persist the token
in settings under 'policy_pdf_token' so the upload happens once per
deployment.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import settings_store

log = logging.getLogger(__name__)

POLICY_PDF_REL = "PRIVACY.pdf"
POLICY_TOKEN_KEY = "policy_pdf_token"


def _resolve_pdf_path() -> Path:
    """The PDF is copied into the image at /app/seed/PRIVACY.pdf (see Dockerfile)."""
    return cfg.seed_dir / POLICY_PDF_REL


async def _do_upload(bot) -> str | None:
    """Upload PRIVACY.pdf via the maxapi high-level helper. Returns token or None."""
    path = _resolve_pdf_path()
    if not path.exists():
        log.warning("policy PDF not found at %s — skipping upload", path)
        return None

    try:
        from maxapi.enums.upload_type import UploadType  # type: ignore
        from maxapi.types.input_media import InputMedia  # type: ignore
    except Exception:
        log.exception("maxapi upload symbols unavailable")
        return None

    try:
        media = InputMedia(type=UploadType.FILE, path=str(path))
    except TypeError:
        try:
            media = InputMedia(type=UploadType.FILE, source=str(path))
        except Exception:
            log.exception("failed to construct InputMedia")
            return None

    try:
        result = await bot.upload_media(media)
    except Exception:
        log.exception("bot.upload_media failed for %s", path)
        return None

    payload = getattr(result, "payload", None)
    token = getattr(payload, "token", None) if payload is not None else None
    if not token:
        log.warning("upload_media returned no token: %r", result)
        return None
    return token


async def ensure_uploaded(bot, *, force: bool = False) -> str | None:
    """Ensure the policy PDF is uploaded; return its current token or None."""
    async with session_scope() as session:
        token = await settings_store.get(session, POLICY_TOKEN_KEY)
    if token and not force:
        return token

    token = await _do_upload(bot)
    if token is None:
        return None

    async with session_scope() as session:
        await settings_store.set_value(session, POLICY_TOKEN_KEY, token)
    log.info("policy PDF uploaded; token cached")
    return token


def build_file_attachment(token: str, filename: str = "Политика_конфиденциальности.pdf") -> dict:
    """Serialize a file attachment in the shape MAX accepts in /messages."""
    return {"type": "file", "payload": {"token": token}}
