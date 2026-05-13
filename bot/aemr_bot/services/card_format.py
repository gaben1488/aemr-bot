from datetime import datetime

from zoneinfo import ZoneInfo

from aemr_bot.config import settings
from aemr_bot.db.models import Appeal, MessageDirection, User
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
    "file": "файлов",
}


def _local(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def attachments_summary_line(attachments: list[dict]) -> str:
    """Однострочная сводка по вложениям гражданина для карточки в
    админ-чате. Возвращает пустую строку, если показать нечего.
    """
    counts = count_by_type(attachments or [])
    if not counts:
        return ""
    parts: list[str] = []
    for kind, label in _ATTACHMENT_LABELS.items():
        n = counts.get(kind, 0)
        if n:
            parts.append(f"{label} {n}")
    if not parts:
        return ""
    return "Вложения: " + ", ".join(parts)


def _clip(text: str, limit: int = 900) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _loaded_messages(appeal: Appeal) -> list:
    """Берём только уже загруженные messages.

    У SQLAlchemy lazy-load в async-коде может упасть MissingGreenlet, если
    случайно обратиться к незагруженной связи. Поэтому смотрим в __dict__:
    selectinload положит туда список, а незагруженную связь не трогаем.
    """
    messages = getattr(appeal, "__dict__", {}).get("messages")
    return list(messages or [])


def admin_followups_block(appeal: Appeal) -> str:
    followups = [
        msg for msg in _loaded_messages(appeal)
        if getattr(msg, "direction", None) == MessageDirection.FROM_USER.value
    ]
    if not followups:
        return ""

    hidden_count = max(0, len(followups) - 5)
    visible = followups[-5:]
    title = "Дополнение к обращению:" if len(visible) == 1 else "Дополнения к обращению:"
    lines = ["────────────────", title]
    if hidden_count:
        lines.append(f"Ранее было ещё {hidden_count} дополнений.")
    for idx, msg in enumerate(visible, start=1):
        text = (getattr(msg, "text", None) or "").strip()
        attachments = getattr(msg, "attachments", None) or []
        attach_line = attachments_summary_line(attachments)
        body = _clip(text) if text else "Без текста."
        if attach_line:
            body = f"{body}\n{attach_line}"
        if len(visible) == 1:
            lines.append(body)
        else:
            lines.append(f"{idx}. {body}")
    return "\n".join(lines)


def admin_card(appeal: Appeal, user: User) -> str:
    """Карточка обращения в служебной группе — единый стиль независимо от
    того, есть вложения или нет.

    Раньше было «то 2, то 3 разделителя в зависимости от наличия фото».
    Теперь блок «Вложения» всегда добавляется внутри тела через ту же
    линию, что и остальные секции, без второй декоративной полосы.
    """
    body = ADMIN_CARD_TEMPLATE.format(
        number=appeal.id,
        name=user.first_name or "—",
        phone=user.phone or "—",
        locality=appeal.locality or "—",
        address=appeal.address or "—",
        topic=appeal.topic or "—",
        summary=appeal.summary or "—",
        answer_limit=settings.answer_max_chars,
    )
    summary_line = attachments_summary_line(appeal.attachments or [])
    if summary_line:
        body = f"{body}\n{summary_line}"
    followups = admin_followups_block(appeal)
    if followups:
        body = f"{body}\n\n{followups}"
    return body


def admin_followup(appeal: Appeal, user: User, text: str) -> str:
    return ADMIN_FOLLOWUP_TEMPLATE.format(
        number=appeal.id,
        name=user.first_name or "—",
        text=text,
    )


def citizen_reply(appeal: Appeal, reply_text: str) -> str:
    """Обернуть текстовый ответ оператора в формальную рамку письма,
    чтобы гражданин видел, кто ответил и по какому обращению, а не
    голое сообщение в личке с ботом."""
    from aemr_bot.texts import CITIZEN_REPLY_TEMPLATE

    return CITIZEN_REPLY_TEMPLATE.format(
        number=appeal.id,
        created_at=_local(appeal.created_at),
        topic=appeal.topic or "—",
        locality=appeal.locality or "—",
        address=appeal.address or "—",
        reply_text=reply_text,
    )


def user_card(appeal: Appeal) -> str:
    emoji, label = STATUS_LABELS.get(appeal.status, ("•", appeal.status))
    return APPEAL_CARD_TEMPLATE.format(
        number=appeal.id,
        created_at=_local(appeal.created_at),
        status_emoji=emoji,
        status_label=label,
        locality=appeal.locality or "—",
        address=appeal.address or "—",
        topic=appeal.topic or "—",
        summary=appeal.summary or "—",
    )


def appeal_list_label(appeal: Appeal) -> str:
    emoji, label = STATUS_LABELS.get(appeal.status, ("•", appeal.status))
    summary_preview = (appeal.summary or "").replace("\n", " ")[:32]
    return f"{emoji} #{appeal.id} · {label} · {_local(appeal.created_at)} · {summary_preview}"
