import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aemr_bot.config import settings as cfg
from aemr_bot.db.models import Setting

DEFAULTS: dict[str, Any] = {
    "welcome_text": None,
    "consent_text": None,
    "policy_url": "https://example.org/privacy",
    "electronic_reception_url": "https://example.org/reception",
    "udth_schedule_url": "https://example.org/udth",
    "appointment_text": "Приём граждан проводится по предварительной записи. Телефон записи: +7 (415-31) 0-00-00.",
    "emergency_contacts": [],
    "topics": [],
}


async def get(session: AsyncSession, key: str) -> Any:
    row = await session.scalar(select(Setting).where(Setting.key == key))
    if row is not None:
        return row.value
    return DEFAULTS.get(key)


async def set_value(session: AsyncSession, key: str, value: Any) -> None:
    stmt = (
        pg_insert(Setting)
        .values(key=key, value=value)
        .on_conflict_do_update(index_elements=[Setting.key], set_={"value": value})
    )
    await session.execute(stmt)


async def list_keys(session: AsyncSession) -> list[str]:
    rows = await session.scalars(select(Setting.key))
    in_db = set(rows)
    return sorted(in_db.union(DEFAULTS.keys()))


def _read_seed_json(name: str) -> Any:
    path = cfg.seed_dir / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _read_seed_text(name: str) -> str | None:
    path = cfg.seed_dir / name
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


async def seed_if_empty(session: AsyncSession) -> None:
    """Populate settings from /seed only when key is missing."""
    existing = set(await session.scalars(select(Setting.key)))

    seed_pairs: dict[str, Any] = {}
    if (topics := _read_seed_json("topics.json")) is not None:
        seed_pairs["topics"] = topics
    if (contacts := _read_seed_json("contacts.json")) is not None:
        seed_pairs["emergency_contacts"] = contacts
    if (welcome := _read_seed_text("welcome.md")) is not None:
        seed_pairs["welcome_text"] = welcome
    if (consent := _read_seed_text("consent.md")) is not None:
        seed_pairs["consent_text"] = consent

    for k, v in seed_pairs.items():
        if k not in existing:
            await set_value(session, k, v)
