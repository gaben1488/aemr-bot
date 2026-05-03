"""Cache the privacy-policy file token in settings so the upload runs once."""

from __future__ import annotations

import logging
from pathlib import Path

from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import settings_store, uploads

log = logging.getLogger(__name__)

POLICY_PDF_REL = "PRIVACY.pdf"
POLICY_TOKEN_KEY = "policy_pdf_token"


def _resolve_pdf_path() -> Path:
    return cfg.seed_dir / POLICY_PDF_REL


async def ensure_uploaded(bot, *, force: bool = False) -> str | None:
    async with session_scope() as session:
        token = await settings_store.get(session, POLICY_TOKEN_KEY)
    if token and not force:
        return token

    path = _resolve_pdf_path()
    if not path.exists():
        log.warning("policy PDF not found at %s — skipping upload", path)
        return None

    token = await uploads.upload_path(bot, path)
    if token is None:
        return None

    async with session_scope() as session:
        await settings_store.set_value(session, POLICY_TOKEN_KEY, token)
    log.info("policy PDF uploaded; token cached")
    return token


def build_file_attachment(token: str):
    return uploads.file_attachment(token)
