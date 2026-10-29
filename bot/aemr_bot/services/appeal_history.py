"""Readable pages of an appeal without truncating its stored correspondence."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aemr_bot.db.models import MessageDirection
from aemr_bot.services.card_format import _local
from aemr_bot.utils.text_pages import split_text_units
from aemr_bot.utils.url_defang import defang_for_admin

# Budget UTF-16 units, conservatively allowing for MAX's 4000-character
# transport limit, header, navigation, and non-BMP characters.
_CONTENT_UNITS = 2600


@dataclass(frozen=True)
class HistoryPage:
    entry: int
    message_id: int
    label: str
    date: datetime | None
    content: str
    part: int
    parts: int
    attachments: int

    def render(self, appeal_id: int) -> str:
        date = f' · {_local(self.date)}' if self.date else ''
        part = f' · часть {self.part}/{self.parts}' if self.parts > 1 else ''
        attach = f'\n📎 Вложения: {self.attachments}' if self.attachments else ''
        return f'📖 Обращение № {appeal_id}\n{self.label}{date}{part}\n\n{self.content or "Без текста"}{attach}'


def pages_for(appeal: Any, *, operator: bool) -> list[HistoryPage]:
    """One or more bounded screens per message; messages remain in order."""
    entries = [(
        'Обращение', getattr(appeal, 'created_at', None),
        getattr(appeal, 'summary', None) or '', len(getattr(appeal, 'attachments', None) or []),
        True, 0,
    )]
    messages = list(getattr(appeal, 'messages', None) or [])
    messages.sort(key=lambda m: (getattr(m, 'created_at', None) or datetime.min.replace(tzinfo=UTC),
                                 getattr(m, 'id', 0) or 0))
    for message in messages:
        direction = getattr(message, 'direction', '')
        # Служебные записи не являются перепиской с жителем. Старые базы
        # могут содержать такие значения, даже если новый код их не пишет.
        if not operator and direction not in {
            MessageDirection.FROM_USER.value, MessageDirection.FROM_OPERATOR.value,
        }:
            continue
        from_user = direction == MessageDirection.FROM_USER.value
        label = ('Дополнение жителя' if operator else 'Ваше дополнение') if from_user else (
            'Ответ оператора' if operator else 'Ответ администрации'
        ) if direction == MessageDirection.FROM_OPERATOR.value else 'Запись'
        entries.append((label, getattr(message, 'created_at', None), getattr(message, 'text', None) or '',
                        len(getattr(message, 'attachments', None) or []), from_user,
                        getattr(message, 'id', 0) or 0))

    result: list[HistoryPage] = []
    for entry, (label, date, text, attachments, from_user, message_id) in enumerate(entries):
        # For operators retain the existing protection against clickable links.
        if operator and from_user:
            text = defang_for_admin(text) or ''
        pieces = split_text_units(text, _CONTENT_UNITS)
        result.extend(HistoryPage(entry, message_id, label, date, part, index + 1, len(pieces), attachments)
                      for index, part in enumerate(pieces))
    return result


def attachments_for(appeal: Any, message_id: int, *, operator: bool) -> list[dict] | None:
    """Return only the selected record's attachments, never another appeal's."""
    if message_id == 0:
        return list(getattr(appeal, 'attachments', None) or [])
    for message in getattr(appeal, 'messages', None) or []:
        if getattr(message, 'id', None) != message_id:
            continue
        if not operator and getattr(message, 'direction', '') not in {
            MessageDirection.FROM_USER.value, MessageDirection.FROM_OPERATOR.value,
        }:
            return None
        return list(getattr(message, 'attachments', None) or [])
    return None
