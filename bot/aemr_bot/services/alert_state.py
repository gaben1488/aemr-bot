"""Durable, privacy-free suppression of repeated incident notifications.

A claim is committed before contacting MAX. A crash or a timeout therefore
leaves an uncertain result, which must be investigated rather than resent.
"""
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from aemr_bot.db.models import Setting

_PREFIX = "__incident_alert:"


def _key(name: str) -> str:
    return _PREFIX + name


async def claim(session, name: str) -> bool:
    stmt = (insert(Setting).values(key=_key(name), value="attempting")
            .on_conflict_do_nothing(index_elements=[Setting.key])
            .returning(Setting.key))
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def finish(session, name: str, *, confirmed: bool) -> None:
    await session.execute(update(Setting).where(Setting.key == _key(name)).values(
        value="confirmed" if confirmed else "unknown"
    ))


async def get(session, name: str) -> str | None:
    value = await session.scalar(select(Setting.value).where(Setting.key == _key(name)))
    return "unknown" if value == "attempting" else value


async def clear(session, name: str) -> None:
    await session.execute(delete(Setting).where(Setting.key == _key(name)))


async def claim_snapshot(session, name: str, signature: str) -> bool:
    """Claim a changed digest before sending it, including after a restart."""
    stmt = insert(Setting).values(key=_key(name), value=signature)
    returning_stmt = stmt.on_conflict_do_update(
        index_elements=[Setting.key], set_={"value": signature},
        where=Setting.value.is_distinct_from(signature),
    ).returning(Setting.key)
    return (await session.execute(returning_stmt)).scalar_one_or_none() is not None
