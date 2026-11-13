from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from aemr_bot.db.models import ResidentDataCopy, User
from aemr_bot.services import data_erasure, users
from tests._erasure_helpers import erasure_database  # noqa: F401


@pytest.mark.asyncio
async def test_restore_resident_pre_erasure_preserves_clean_later_export(erasure_db):
    db = erasure_db
    old_bot = SimpleNamespace(
        send_message=AsyncMock(
            return_value=SimpleNamespace(
                message=SimpleNamespace(body=SimpleNamespace(mid="old-report"))
            )
        )
    )
    await data_erasure.tracked_send(old_bot, kind="bulk_export", text="old report")
    async with db.scope() as session:
        await users.erase_pdn_detailed(session, 42)
    bot = SimpleNamespace(
        send_message=AsyncMock(
            return_value=SimpleNamespace(
                message=SimpleNamespace(body=SimpleNamespace(mid="clean-post-erasure"))
            )
        )
    )
    await data_erasure.tracked_send(bot, kind="bulk_export", text="clean report")
    async with db.scope() as session:
        session.add(
            User(
                max_user_id=42,
                first_name="Restored",
                created_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
    for _ in range(2):
        async with db.scope() as session:
            await data_erasure.replay_restore_tombstones(session)
            report = await session.scalar(
                select(ResidentDataCopy).where(ResidentDataCopy.message_id == "clean-post-erasure")
            )
            old = await session.scalar(
                select(ResidentDataCopy).where(ResidentDataCopy.message_id == "old-report")
            )
            assert not report.erase_requested and report.state == "retained"
            assert old.erase_requested and old.state == "manual"
            assert await users.find_by_max_id(session, 42) is None
