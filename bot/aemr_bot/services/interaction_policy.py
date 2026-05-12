from __future__ import annotations

_OPEN_STATUSES = {"new", "in_progress"}
_TERMINAL_STATUSES = {"answered", "closed"}


def can_followup_status(status: str | None) -> bool:
    return status in _OPEN_STATUSES


def repeat_status_word(status: str | None) -> str:
    if status == "answered":
        return "отвеченному"
    if status == "closed":
        return "закрытому"
    return "исходному"


def strip_feedback_topic(topic: str | None) -> str:
    value = (topic or "").strip()
    marker = ": обратная связь по "
    if marker in value:
        return value.split(marker, 1)[0].strip()
    return value


def repeat_topic(topic: str | None, source_status: str | None) -> str:
    base = strip_feedback_topic(topic) or "Без темы"
    value = f"{base}: обратная связь по {repeat_status_word(source_status)} вопросу"
    return value[:120]
