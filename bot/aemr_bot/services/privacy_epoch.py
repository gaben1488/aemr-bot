"""Serialize export delivery with erasure without storing resident identifiers."""
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from aemr_bot.db.models import PrivacyEpoch

_LOCK_KEY = 0x41454D5250444E


async def read(session) -> int:
    value = await session.scalar(select(PrivacyEpoch.generation).where(PrivacyEpoch.id == 1))
    return int(value or 0)


async def mark_erasure(session) -> None:
    """Exclusive transaction lock covers deletion through its commit."""
    if session.bind.dialect.name == "postgresql":
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})
    statement = insert(PrivacyEpoch).values(id=1, generation=1).on_conflict_do_update(
        index_elements=[PrivacyEpoch.id], set_={"generation": PrivacyEpoch.generation + 1}
    )
    await session.execute(statement)


async def hold_export_if_current(session, expected: int) -> bool:
    """Hold a shared lock through file upload/send, only for a fresh snapshot."""
    if session.bind.dialect.name == "postgresql":
        await session.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": _LOCK_KEY})
    return await read(session) == expected
