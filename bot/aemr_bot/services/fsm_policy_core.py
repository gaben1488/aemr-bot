from __future__ import annotations

OPEN_STATUSES = {"new", "in_progress"}
DONE_STATUSES = {"answered", "closed"}


def can_append_to_status(status: str | None) -> bool:
    return status in OPEN_STATUSES


def source_status_word(status: str | None) -> str:
    if status == "answered":
        return "answered"
    if status == "closed":
        return "closed"
    return "source"


def strip_feedback_topic(topic: str | None) -> str:
    value = (topic or "").strip()
    marker = ": feedback on "
    if marker in value:
        return value.split(marker, 1)[0].strip()
    return value


def feedback_topic(topic: str | None, source_status: str | None) -> str:
    base = strip_feedback_topic(topic) or "No topic"
    value = f"{base}: feedback on {source_status_word(source_status)} appeal"
    if len(value) > 120:
        return value[:117].rstrip() + "..."
    return value
