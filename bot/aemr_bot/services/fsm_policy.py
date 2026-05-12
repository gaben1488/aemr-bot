from __future__ import annotations

import logging

log = logging.getLogger(__name__)

OPEN_STATUSES = {"new", "in_progress"}
DONE_STATUSES = {"answered", "closed"}


def _t(hex_value: str) -> str:
    return bytes.fromhex(hex_value).decode("utf-8")


def can_append(status: str | None) -> bool:
    return status in OPEN_STATUSES


def install() -> None:
    log.info("fsm policy installed")
