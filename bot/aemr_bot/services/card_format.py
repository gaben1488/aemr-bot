from datetime import datetime

from zoneinfo import ZoneInfo

from aemr_bot.config import settings
from aemr_bot.db.models import Appeal, User
from aemr_bot.texts import (
    ADMIN_CARD_TEMPLATE,
    ADMIN_FOLLOWUP_TEMPLATE,
    APPEAL_CARD_TEMPLATE,
    STATUS_LABELS,
)
from aemr_bot.utils.attachments import count_by_type

TZ = ZoneInfo(settings.timezone)

_ATTACHMENT_LABELS = {
    "image": "фото",
    "video": "видео",
    "audio": "голосовых",
    "file": "файлов",
    "location": "геолокация",
}


def _local(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def attachments_summary_line(attachments: list[dict]) -> str:
    """Render a single-line summary of citizen-supplied attachments for the
    admin card. Returns empty string if there's nothing presentable.
    """
    counts = count_by_type(attachments or [])
    if not counts:
        return ""
    parts: list[str] = []
    for kind, label in _ATTACHMENT_LABELS.items():
        n = counts.get(kind, 0)
        if n:
            parts.append(f"{label} {n}" if kind != "location" else label)
    if not parts:
        return ""
    return "📎 Вложения: " + ", ".join(parts)


def admin_card(appeal: Appeal, user: User) -> str:
    summary_line = attachments_summary_line(appeal.attachments or [])
    body = ADMIN_CARD_TEMPLATE.format(
        number=appeal.id,
        name=user.first_name or "—",
        phone=user.phone or "—",
        address=appeal.address or "—",
        topic=appeal.topic or "—",
        summary=appeal.summary or "—",
        answer_limit=settings.answer_max_chars,
    )
    if summary_line:
        body = f"{body}\n──────────\n{summary_line}"
    return body


def admin_followup(appeal: Appeal, user: User, text: str) -> str:
    return ADMIN_FOLLOWUP_TEMPLATE.format(
        number=appeal.id,
        name=user.first_name or "—",
        text=text,
    )


def user_card(appeal: Appeal) -> str:
    emoji, label = STATUS_LABELS.get(appeal.status, ("•", appeal.status))
    return APPEAL_CARD_TEMPLATE.format(
        number=appeal.id,
        created_at=_local(appeal.created_at),
        status_emoji=emoji,
        status_label=label,
        address=appeal.address or "—",
        topic=appeal.topic or "—",
        summary=appeal.summary or "—",
    )


def appeal_list_label(appeal: Appeal) -> str:
    emoji, label = STATUS_LABELS.get(appeal.status, ("•", appeal.status))
    summary_preview = (appeal.summary or "").replace("\n", " ")[:32]
    return f"{emoji} #{appeal.id} · {label} · {_local(appeal.created_at)} · {summary_preview}"
