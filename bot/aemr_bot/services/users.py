from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aemr_bot.db.models import DialogState, User


async def get_or_create(session: AsyncSession, max_user_id: int, first_name: str | None = None) -> User:
    user = await session.scalar(select(User).where(User.max_user_id == max_user_id))
    if user is None:
        user = User(max_user_id=max_user_id, first_name=first_name)
        session.add(user)
        await session.flush()
    return user


async def has_consent(session: AsyncSession, max_user_id: int) -> bool:
    user = await session.scalar(select(User).where(User.max_user_id == max_user_id))
    return bool(user and user.consent_pdn_at)


async def set_consent(session: AsyncSession, max_user_id: int) -> None:
    await session.execute(
        update(User)
        .where(User.max_user_id == max_user_id)
        .values(consent_pdn_at=datetime.now(timezone.utc))
    )


async def set_phone(session: AsyncSession, max_user_id: int, phone: str) -> None:
    await session.execute(
        update(User).where(User.max_user_id == max_user_id).values(phone=phone)
    )


async def set_first_name(session: AsyncSession, max_user_id: int, first_name: str) -> None:
    await session.execute(
        update(User).where(User.max_user_id == max_user_id).values(first_name=first_name)
    )


async def set_state(session: AsyncSession, max_user_id: int, state: DialogState, data: dict | None = None) -> None:
    values: dict = {"dialog_state": state.value}
    if data is not None:
        values["dialog_data"] = data
    await session.execute(update(User).where(User.max_user_id == max_user_id).values(**values))


async def reset_state(session: AsyncSession, max_user_id: int) -> None:
    await session.execute(
        update(User)
        .where(User.max_user_id == max_user_id)
        .values(dialog_state=DialogState.IDLE.value, dialog_data={})
    )


async def update_dialog_data(session: AsyncSession, max_user_id: int, patch: dict) -> dict:
    user = await session.scalar(select(User).where(User.max_user_id == max_user_id))
    if user is None:
        return {}
    data = dict(user.dialog_data or {})
    data.update(patch)
    user.dialog_data = data
    await session.flush()
    return data


async def find_stuck_in_summary(
    session: AsyncSession,
    idle_seconds: int,
    limit: int = 1000,
) -> list[int]:
    """Return max_user_id of users stuck in AWAITING_SUMMARY past idle_seconds.

    Limit guards against pathological cases (e.g. 10k stuck rows after a long
    outage would otherwise produce 10k bot API calls during startup recovery).
    """
    threshold = datetime.now(timezone.utc) - timedelta(seconds=idle_seconds)
    result = await session.scalars(
        select(User.max_user_id)
        .where(
            User.dialog_state == DialogState.AWAITING_SUMMARY.value,
            User.updated_at <= threshold,
        )
        .limit(limit)
    )
    return list(result)


async def erase_pdn(session: AsyncSession, max_user_id: int) -> bool:
    """Anonymize the user and revoke the PDN consent (152-FZ art. 9 §2)."""
    result = await session.execute(
        update(User)
        .where(User.max_user_id == max_user_id)
        .values(
            first_name="Удалено",
            phone=None,
            consent_pdn_at=None,
            dialog_state=DialogState.IDLE.value,
            dialog_data={},
        )
    )
    return result.rowcount > 0
