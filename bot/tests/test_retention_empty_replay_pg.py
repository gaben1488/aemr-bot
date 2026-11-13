import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from aemr_bot.db.models import Appeal, Message
from aemr_bot.services import appeals, data_erasure
from tests._erasure_helpers import erasure_database  # noqa: F401


@pytest.mark.asyncio
async def test_restore_empty_pre_retention_appeal_still_marks_erased(erasure_db):
    db = erasure_db
    async with db.scope() as session:
        appeal = await session.get(Appeal, db.appeal_id)
        appeal.created_at = datetime.now(UTC) - timedelta(days=365 * 7)
        appeal.closed_at = appeal.created_at
        appeal.status = "closed"
        appeal.summary = None
        appeal.address = None
        appeal.attachments = []
        await session.execute(
            update(Message).where(Message.appeal_id == appeal.id).values(text=None, attachments=[])
        )
        await data_erasure.request_appeal_cleanup(session, [appeal.id])
    journal = next((db.path / "journal").glob("*.retention.json"))
    original_cutoff = datetime.fromisoformat(json.loads(journal.read_text())["erased_before"])
    async with db.scope() as session:
        appeal = await session.get(Appeal, db.appeal_id)
        appeal.content_erased_at = None  # old snapshot predates retention
    async with db.scope() as session:
        await data_erasure.replay_restore_tombstones(session)
        appeal = await session.get(Appeal, db.appeal_id, populate_existing=True)
        result = await appeals.reopen(session, appeal.id)
        assert (appeal.content_erased_at, result) == (original_cutoff, "content_erased")
    async with db.scope() as session:
        await data_erasure.replay_restore_tombstones(session)
        assert (await session.get(Appeal, db.appeal_id)).content_erased_at == original_cutoff


@pytest.mark.asyncio
async def test_restore_pre_retention_preserves_clean_later_export(erasure_db):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sqlalchemy import select

    from aemr_bot.db.models import ResidentDataCopy

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
        appeal = await session.get(Appeal, db.appeal_id)
        appeal.created_at = datetime.now(UTC) - timedelta(days=365 * 7)
        await data_erasure.request_appeal_cleanup(session, [appeal.id])
    bot = SimpleNamespace(
        send_message=AsyncMock(
            return_value=SimpleNamespace(
                message=SimpleNamespace(body=SimpleNamespace(mid="clean-post-retention"))
            )
        )
    )
    await data_erasure.tracked_send(bot, kind="bulk_export", text="clean report")
    async with db.scope() as session:
        appeal = await session.get(Appeal, db.appeal_id)
        appeal.summary = "restored pre-retention private content"
        appeal.content_erased_at = None
    async with db.scope() as session:
        await data_erasure.replay_restore_tombstones(session)
        report = await session.scalar(
            select(ResidentDataCopy).where(ResidentDataCopy.message_id == "clean-post-retention")
        )
        assert not report.erase_requested and report.state == "retained"
        old = await session.scalar(
            select(ResidentDataCopy).where(ResidentDataCopy.message_id == "old-report")
        )
        assert old.erase_requested and old.state == "manual"
    async with db.scope() as session:
        await data_erasure.replay_restore_tombstones(session)
        report = await session.scalar(
            select(ResidentDataCopy).where(ResidentDataCopy.message_id == "clean-post-retention")
        )
        assert not report.erase_requested and report.state == "retained"
