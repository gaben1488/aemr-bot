from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import desc, select

from aemr_bot.db.models import Appeal, Message, MessageDirection

log = logging.getLogger(__name__)

OPEN_STATUSES = {"new", "in_progress"}
DONE_STATUSES = {"answered", "closed"}


def can_append(status: str | None) -> bool:
    return status in OPEN_STATUSES


def install() -> None:
    log.info("flow runtime installed")
