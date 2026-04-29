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

# Whitelist of editable keys with their accepted Python types and any extra rules.
# /setting <key> <value> rejects anything outside this map.
SCHEMA: dict[str, dict] = {
    "welcome_text": {"type": str, "min_len": 1, "max_len": 4000},
    "consent_text": {"type": str, "min_len": 1, "max_len": 4000},
    "policy_url": {"type": str, "url": True},
    "electronic_reception_url": {"type": str, "url": True},
    "udth_schedule_url": {"type": str, "url": True},
    "appointment_text": {"type": str, "min_len": 1, "max_len": 2000},
    "emergency_contacts": {"type": list, "min_items": 1, "item_keys": {"name", "phone"}},
    "topics": {"type": list, "min_items": 1, "max_items": 30, "item_type": str},
}


def validate(key: str, value: Any) -> tuple[bool, str]:
    """Return (ok, message). Message is the reason on failure or 'ok' on success."""
    if key not in SCHEMA:
        return False, f"Unknown key '{key}'. Allowed: {sorted(SCHEMA)}"
    rule = SCHEMA[key]
    expected = rule["type"]
    if not isinstance(value, expected):
        return False, f"Expected type {expected.__name__}, got {type(value).__name__}"
    if expected is str:
        if "min_len" in rule and len(value) < rule["min_len"]:
            return False, f"String too short, min_len={rule['min_len']}"
        if "max_len" in rule and len(value) > rule["max_len"]:
            return False, f"String too long, max_len={rule['max_len']}"
        if rule.get("url") and not (value.startswith("https://") or value.startswith("http://")):
            return False, "URL must start with http:// or https://"
    if expected is list:
        if "min_items" in rule and len(value) < rule["min_items"]:
            return False, f"List too short, min_items={rule['min_items']}"
        if "max_items" in rule and len(value) > rule["max_items"]:
            return False, f"List too long, max_items={rule['max_items']}"
        if "item_type" in rule and not all(isinstance(it, rule["item_type"]) for it in value):
            return False, f"All items must be {rule['item_type'].__name__}"
        if "item_keys" in rule:
            for it in value:
                if not isinstance(it, dict) or not rule["item_keys"].issubset(it):
                    return False, f"Each item must be an object with keys: {rule['item_keys']}"
    return True, "ok"


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
