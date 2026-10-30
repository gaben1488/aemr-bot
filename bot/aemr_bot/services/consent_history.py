"""Human-readable pages of the actual consent shown to a resident."""
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from aemr_bot.config import settings
from aemr_bot.utils.text_pages import split_text_units


@dataclass(frozen=True)
class ConsentPage:
    text: str
    date: str
    edition: int
    editions: int
    part: int
    parts: int


def pages_for(records: list) -> list[ConsentPage]:
    valid = [r for r in records if isinstance(r, dict) and isinstance(r.get("text"), str)]
    result: list[ConsentPage] = []
    for edition, record in enumerate(valid, 1):
        try:
            date = datetime.fromisoformat(record["accepted_at"]).astimezone(
                ZoneInfo(settings.timezone)
            ).strftime("%d.%m.%Y %H:%M")
        except (KeyError, ValueError, TypeError):
            date = "время неизвестно"
        parts = split_text_units(record["text"], 2600)
        result.extend(ConsentPage(part, date, edition, len(valid), index + 1, len(parts))
                      for index, part in enumerate(parts))
    return result
