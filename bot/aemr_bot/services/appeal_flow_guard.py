from __future__ import annotations

import logging

log = logging.getLogger(__name__)

OPEN_STATUSES = {"new", "in_progress"}
DONE_STATUSES = {"answered", "closed"}


def is_open_status(status: str | None) -> bool:
    return status in OPEN_STATUSES


def install() -> None:
    log.info("appeal flow guard installed")
