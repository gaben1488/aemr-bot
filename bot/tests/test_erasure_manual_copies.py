"""Manual external copies, legacy restore restrictions, and local backup cleanup."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from aemr_bot.db.models import ErasureTombstone, ResidentDataCopy
from aemr_bot.services import data_erasure, db_backup, users
from tests._erasure_helpers import (
    erasure_database,  # noqa: F401
    send_copy,
)


@pytest.mark.asyncio
async def test_legacy_pending_max_messages_are_manual_and_never_deleted(erasure_db):
    db = erasure_db
    async with db.scope() as session:
        session.add_all([
            ResidentDataCopy(kind=kind, message_id=f"old-{kind}", state="pending", erase_requested=True)
            for kind in ("resident_input", "admin_card", "bulk_export")
        ])
    bot = SimpleNamespace(delete_message=AsyncMock(return_value=SimpleNamespace(success=True)))
    assert await data_erasure.recover_deletions(bot) == 0
    bot.delete_message.assert_not_awaited()
    copies = await data_erasure.unresolved_copies()
    assert {copy["state"] for copy in copies} == {"manual"}


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_state", ["manual", "uncertain", "deleting", "unknown_v1"])
async def test_local_backup_continues_after_legacy_unknown_external_cleanup(erasure_db, monkeypatch, legacy_state):
    db = erasure_db
    async with db.scope() as session:
        await users.erase_pdn_detailed(session, 42)
        for item in (await session.scalars(select(ErasureTombstone))).all():
            item.backups_state = legacy_state
            item.last_error = "s3_cleanup_attempt_unknown"
    directory = db.path / "backups"
    old = directory / "aemr-20000101_010101.sql"
    old.write_text("private old dump")
    monkeypatch.setattr(data_erasure.settings, "backup_allow_unencrypted", True)
    monkeypatch.setattr(data_erasure.settings, "backup_gpg_passphrase", None)
    for key, value in {"bucket": "legacy", "endpoint": "https://s3.example", "access_key": "key", "secret_key": "secret"}.items():
        monkeypatch.setattr(data_erasure.settings, f"backup_s3_{key}", value)
    async def dump(path, env):
        path.write_text("sanitized new dump")
    monkeypatch.setattr(db_backup, "_run_pg_dump", dump)
    spawn = AsyncMock(side_effect=AssertionError("S3 must never be contacted"))
    monkeypatch.setattr(db_backup.asyncio, "create_subprocess_exec", spawn)
    result = await db_backup.backup_db()
    assert result.ok
    assert result.path.read_text() == "sanitized new dump"
    assert not old.exists()
    spawn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_state", ["pending", "deleting"])
async def test_restore_retires_legacy_external_deletion_queue_without_max(erasure_db, old_state):
    import json

    from sqlalchemy import delete

    db = erasure_db
    await send_copy(db, mid="legacy-restore-card")
    async with db.scope() as session:
        await users.erase_pdn_detailed(session, 42)
    for path in (db.path / "journal").glob("*.copy.json"):
        entry = json.loads(path.read_text())
        entry["state"] = old_state
        path.write_text(json.dumps(entry))
    async with db.scope() as session:
        await session.execute(delete(ResidentDataCopy))
        await data_erasure.replay_restore_tombstones(session)
        copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.message_id == "legacy-restore-card"))
        assert copy.state == "manual" and copy.erase_requested
    original_edit = AsyncMock()
    bot = SimpleNamespace(send_message=AsyncMock(), edit_message=original_edit, delete_message=AsyncMock())
    data_erasure.install_outgoing_hook(bot)
    await data_erasure.recover_deletions(bot)
    with pytest.raises(data_erasure.PersonalDataErasedError):
        await bot.edit_message(message_id="legacy-restore-card", text="stale resident details")
    original_edit.assert_not_awaited()
    bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_backup_confirmation_cannot_skip_local_cleanup(erasure_db):
    db = erasure_db
    old = db.path / "backups" / "aemr-20000101_010101.sql"
    old.write_text("private before erasure")
    async with db.scope() as session:
        await users.erase_pdn_detailed(session, 42)
        item = await session.get(ErasureTombstone, data_erasure.subject_digest(42))
        item.backups_state = "manual"
        digest, cutoff = item.subject_digest, item.erased_before.isoformat()
    assert await data_erasure.confirm_backup_cleanup(digest, expected_erased_before=cutoff)
    assert not old.exists()
