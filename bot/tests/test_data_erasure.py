"""Durable external copy cleanup and restore quarantine regressions."""
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from aemr_bot.db.models import Appeal, Base, ErasureTombstone, Message, ResidentDataCopy, User
from aemr_bot.services import data_erasure, db_backup, users


@pytest_asyncio.fixture
async def erasure_db(monkeypatch, tmp_path):
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def scope():
        async with factory.begin() as session:
            yield session

    monkeypatch.setattr(data_erasure, "session_scope", scope)
    monkeypatch.setattr("aemr_bot.db.session.session_scope", scope)
    monkeypatch.setattr(data_erasure.settings, "erasure_state_dir", str(tmp_path / "journal"))
    monkeypatch.setattr(data_erasure.settings, "log_dir", str(tmp_path / "logs"))
    monkeypatch.setattr(data_erasure.settings, "backup_local_dir", str(tmp_path / "backups"))
    monkeypatch.setattr(data_erasure.settings, "backup_s3_bucket", None)
    (tmp_path / "backups").mkdir()
    async with scope() as session:
        resident = User(max_user_id=42, first_name="Иван", phone="+79123456789", consent_pdn_at=datetime.now(UTC))
        other = User(max_user_id=43, first_name="Анна", consent_pdn_at=datetime.now(UTC))
        session.add_all([resident, other])
        await session.flush()
        appeal = Appeal(user_id=resident.id, summary="Private", admin_message_id="first", last_admin_card_mid="last")
        unrelated = Appeal(user_id=other.id, summary="Preserve")
        session.add_all([appeal, unrelated])
        await session.flush()
        session.add(Message(appeal_id=appeal.id, direction="from_user", text="Private", max_message_id="incoming"))
        appeal_id, unrelated_id = appeal.id, unrelated.id
    yield SimpleNamespace(scope=scope, appeal_id=appeal_id, other_id=unrelated_id, path=tmp_path)
    await engine.dispose()


async def _send(db, *, mid="sent", kind="admin_card", appeal_id=None):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid=mid)))))
    await data_erasure.tracked_send(bot, kind=kind, appeal_id=appeal_id or db.appeal_id, text="Private")
    return bot


@pytest.mark.asyncio
async def test_erasure_inventories_known_history_and_preserves_unrelated_data(erasure_db):
    db = erasure_db
    await _send(db)
    await _send(db, mid="unrelated", appeal_id=db.other_id)
    async with db.scope() as session:
        await users.erase_pdn_detailed(session, 42)
    async with db.scope() as session:
        assert await users.find_by_max_id(session, 42) is None
        assert (await session.get(Appeal, db.appeal_id)).summary is None
        assert (await session.get(Appeal, db.other_id)).summary == "Preserve"
        copies = list((await session.scalars(select(ResidentDataCopy))).all())
        requested = {copy.message_id for copy in copies if copy.erase_requested}
        assert requested == {"sent", "first", "last", "incoming"}
        assert next(copy for copy in copies if copy.message_id == "unrelated").state == "retained"
    journal = list((db.path / "journal").glob("*.json"))
    assert journal
    assert all('"max_user_id"' not in path.read_text() and "Иван" not in path.read_text() for path in journal)


@pytest.mark.asyncio
async def test_delete_claim_is_durable_and_unknown_outcome_never_retries(erasure_db):
    db = erasure_db
    await _send(db)
    async with db.scope() as session:
        await users.erase_pdn_detailed(session, 42)
    bot = SimpleNamespace(delete_message=AsyncMock(side_effect=TimeoutError("unknown")))
    assert await data_erasure.recover_deletions(bot) == 0
    attempts = bot.delete_message.await_count
    assert attempts == 4
    assert await data_erasure.recover_deletions(bot) == 0
    assert bot.delete_message.await_count == attempts
    status = await data_erasure.diagnostics()
    assert status["uncertain"] == 4


@pytest.mark.asyncio
async def test_parallel_workers_delete_each_known_copy_once(erasure_db):
    db = erasure_db
    async with db.scope() as session:
        await users.erase_pdn_detailed(session, 42)
    bot = SimpleNamespace(delete_message=AsyncMock(return_value=SimpleNamespace(success=True)))
    assert sum(await asyncio.gather(data_erasure.recover_deletions(bot), data_erasure.recover_deletions(bot))) == 3
    assert bot.delete_message.await_count == 3


@pytest.mark.asyncio
async def test_unknown_send_is_visible_and_only_recovered_mid_can_be_queued(erasure_db):
    db = erasure_db
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError("unknown")))
    with pytest.raises(TimeoutError):
        await data_erasure.tracked_send(bot, kind="relay", appeal_id=db.appeal_id, text="Private")
    async with db.scope() as session:
        await users.erase_pdn_detailed(session, 42)
        copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.kind == "relay"))
        copy_id = copy.id
        assert copy.message_id is None and copy.state == "uncertain" and copy.erase_requested
    assert await data_erasure.reconcile_copy(copy_id, recovered_message_id="found-in-max")
    bot.delete_message = AsyncMock(side_effect=TimeoutError())
    await data_erasure.recover_deletions(bot)
    assert not await data_erasure.reconcile_copy(copy_id, recovered_message_id="found-in-max")
    assert await data_erasure.reconcile_copy(copy_id, confirmed_absent=True)


@pytest.mark.asyncio
async def test_erase_waits_for_inflight_copy_inventory(erasure_db):
    db = erasure_db
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed_send(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid="racing")))
    bot = SimpleNamespace(send_message=delayed_send)
    sending = asyncio.create_task(data_erasure.tracked_send(bot, kind="relay", appeal_id=db.appeal_id, text="Private"))
    await entered.wait()
    async def erase():
        async with db.scope() as session:
            await users.erase_pdn_detailed(session, 42)
    erasing = asyncio.create_task(erase())
    await asyncio.sleep(.03)
    assert not erasing.done()
    release.set()
    await asyncio.gather(sending, erasing)
    async with db.scope() as session:
        copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.message_id == "racing"))
        assert copy.erase_requested and copy.state == "pending"


@pytest.mark.asyncio
async def test_restore_replays_independent_tombstone_without_erasing_new_identity(erasure_db):
    db = erasure_db
    # Journal survives DB restore even when restored DB has no tombstones.
    cutoff = datetime.now(UTC) - timedelta(days=1)
    data_erasure._write_tombstone(data_erasure.subject_digest(42), cutoff)
    async with db.scope() as session:
        resident = await users.find_by_max_id(session, 42)
        resident.created_at = cutoff - timedelta(days=1)
        other = await users.find_by_max_id(session, 43)
        other.created_at = cutoff + timedelta(hours=1)
    async with db.scope() as session:
        assert await data_erasure.replay_restore_tombstones(session) == 1
        assert await users.find_by_max_id(session, 42) is None
        assert await users.find_by_max_id(session, 43) is not None


@pytest.mark.asyncio
async def test_backup_cleanup_preserves_non_dumps_and_surfaces_s3_version_limit(erasure_db, monkeypatch):
    db = erasure_db
    directory = db.path / "backups"
    (directory / "aemr-20200101_010101.sql.gpg").write_text("encrypted")
    (directory / "unrelated.txt").write_text("keep")
    async with db.scope() as session:
        await users.erase_pdn_detailed(session, 42)
    assert await db_backup.cleanup_erased_backups() == 2  # resident + appeal tombstones
    assert list(directory.iterdir()) == [directory / "unrelated.txt"]
    async with db.scope() as session:
        states = (await session.scalars(select(ErasureTombstone.backups_state))).all()
        assert set(states) == {"done"}


@pytest.mark.asyncio
async def test_log_cleanup_redacts_known_tokens_preserving_unrelated(erasure_db):
    db = erasure_db
    directory = db.path / "logs"
    directory.mkdir()
    path = directory / "bot.log"
    path.write_text("resident=42 Иван +79123456789\nresident=43 Анна\ncount=142\n")
    async with db.scope() as session:
        await users.erase_pdn_detailed(session, 42)
    assert path.read_text() == "resident=[erased] [erased] [erased]\nresident=43 Анна\ncount=142\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["revoke", "unsubscribe"])
async def test_restore_keeps_withdrawals_effective_and_preserves_newer_consent(erasure_db, action):
    from aemr_bot.services import broadcasts
    db = erasure_db
    async with db.scope() as session:
        resident = await users.find_by_max_id(session, 42)
        resident.subscribed_broadcast = True
        resident.consent_broadcast_at = datetime.now(UTC) - timedelta(days=2)
        if action == "revoke":
            await users.revoke_consent(session, 42)
        else:
            await broadcasts.set_subscription(session, 42, False)
    # Simulate older DB restore (journal remains independently current).
    async with db.scope() as session:
        resident = await users.find_by_max_id(session, 42)
        resident.consent_pdn_at = datetime.now(UTC) - timedelta(days=2)
        resident.subscribed_broadcast = True
        resident.consent_broadcast_at = datetime.now(UTC) - timedelta(days=2)
    async with db.scope() as session:
        assert await data_erasure.replay_restore_tombstones(session) == 0
        resident = await users.find_by_max_id(session, 42)
        assert not resident.subscribed_broadcast
        if action == "revoke":
            assert resident.consent_pdn_at is None
    # A genuinely new consent must survive a later replay of the same ledger.
    async with db.scope() as session:
        resident = await users.find_by_max_id(session, 42)
        resident.consent_pdn_at = datetime.now(UTC) + timedelta(hours=1)
        resident.consent_broadcast_at = resident.consent_pdn_at
        resident.subscribed_broadcast = True
    async with db.scope() as session:
        await data_erasure.replay_restore_tombstones(session)
        resident = await users.find_by_max_id(session, 42)
        assert resident.consent_pdn_at is not None and resident.subscribed_broadcast


@pytest.mark.asyncio
async def test_any_erasure_invalidates_aggregate_exports_without_deleting_other_sources(erasure_db):
    db = erasure_db
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid="export")))))
    await data_erasure.tracked_send(bot, kind="bulk_export", text="report")
    async with db.scope() as session:
        await users.erase_pdn_detailed(session, 42)
        exported = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.kind == "bulk_export"))
        assert exported.erase_requested and exported.state == "pending"
        assert (await session.get(Appeal, db.other_id)).summary == "Preserve"


@pytest.mark.asyncio
async def test_retention_queues_only_eligible_appeals_and_removes_reply_drafts(erasure_db):
    from aemr_bot.db.models import Operator, OperatorReplyDraft
    from aemr_bot.services import appeals
    db = erasure_db
    old = datetime.now(UTC) - timedelta(days=365 * 7)
    async with db.scope() as session:
        operator = Operator(max_user_id=100, full_name="IT", role="it")
        session.add(operator)
        await session.flush()
        item = await session.get(Appeal, db.appeal_id)
        item.created_at = old
        item.closed_at = old
        item.status = "closed"
        message = await session.scalar(select(Message).where(Message.appeal_id == item.id))
        message.created_at = old
        session.add(OperatorReplyDraft(operator_id=operator.id, appeal_id=item.id, token="draft", is_final=True, data={"text": "private"}))
    async with db.scope() as session:
        assert await appeals.purge_old_appeals_content(session, years=5) == (1, 1)
        assert await session.scalar(select(OperatorReplyDraft)) is None
        assert (await session.get(Appeal, db.other_id)).summary == "Preserve"
        copies = list((await session.scalars(select(ResidentDataCopy))).all())
        assert {copy.message_id for copy in copies if copy.erase_requested} == {"first", "last", "incoming"}
    # Restore an old pre-retention payload, then replay independent ledger.
    async with db.scope() as session:
        item = await session.get(Appeal, db.appeal_id)
        item.summary = "restored-private"
    async with db.scope() as session:
        await data_erasure.replay_restore_tombstones(session)
        assert (await session.get(Appeal, db.appeal_id)).summary is None
        assert (await session.get(Appeal, db.other_id)).summary == "Preserve"


@pytest.mark.asyncio
async def test_s3_cleanup_requires_explicit_version_confirmation_before_backup(erasure_db, monkeypatch):
    db = erasure_db
    monkeypatch.setattr(data_erasure.settings, "backup_s3_bucket", "example")
    monkeypatch.setattr(data_erasure.settings, "backup_s3_endpoint", "https://s3.example")
    monkeypatch.setattr(data_erasure.settings, "backup_s3_access_key", "key")
    monkeypatch.setattr(data_erasure.settings, "backup_s3_secret_key", "secret")
    async with db.scope() as session:
        await users.erase_pdn_detailed(session, 42)
    process = SimpleNamespace(wait=AsyncMock(return_value=0))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(db_backup.asyncio, "create_subprocess_exec", spawn)
    assert await db_backup.cleanup_erased_backups() == 0
    assert spawn.await_count == 1
    result = await db_backup.backup_db()
    assert result.path is None and result.fail_kind == "erasure"
    # No second S3 mutation after a completed attempt whose history needs review.
    assert spawn.await_count == 1
    async with db.scope() as session:
        instructions = (await session.execute(select(ErasureTombstone.subject_digest, ErasureTombstone.erased_before))).all()
    for digest, cutoff in instructions:
        assert not await data_erasure.confirm_backup_cleanup(digest, expected_erased_before=(cutoff - timedelta(seconds=1)).isoformat())
        assert await data_erasure.confirm_backup_cleanup(digest, expected_erased_before=cutoff.isoformat())


@pytest.mark.asyncio
async def test_outgoing_hook_tracks_private_screens_without_double_tracking_explicit_sends(erasure_db):
    db = erasure_db
    original = AsyncMock(side_effect=[
        SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid="screen"))),
        SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid="part"))),
    ])
    bot = SimpleNamespace(send_message=original, edit_message=AsyncMock())
    data_erasure.install_outgoing_hook(bot)
    data_erasure.install_outgoing_hook(bot)
    await bot.send_message(user_id=42, text="profile")
    await data_erasure.tracked_send(bot, kind="operator_part", appeal_id=db.appeal_id, user_id=42, text="reply")
    async with db.scope() as session:
        copies = list((await session.scalars(select(ResidentDataCopy))).all())
        assert [(item.kind, item.message_id) for item in copies] == [("citizen_screen", "screen"), ("operator_part", "part")]
        await users.erase_pdn_detailed(session, 42)
    with pytest.raises(RuntimeError, match="queued for erasure"):
        await bot.edit_message(message_id="screen", text="old profile")
    await bot.edit_message(message_id="unrelated-menu", text="public info")


@pytest.mark.asyncio
async def test_inbox_records_resident_source_mid_atomically_and_deduplicates(erasure_db, monkeypatch):
    from aemr_bot.db.models import InboxEvent
    from aemr_bot.services import inbox
    db = erasure_db
    monkeypatch.setattr(inbox, "session_scope", db.scope)
    payload = {"update_type": "message_created", "message": {
        "sender": {"user_id": 42}, "recipient": {"chat_id": 99999},
        "body": {"mid": "inbound", "text": "private"},
    }}
    await inbox.accept([payload, payload])
    await inbox.accept([payload])
    async with db.scope() as session:
        copies = list((await session.scalars(select(ResidentDataCopy))).all())
        receipts = list((await session.scalars(select(InboxEvent))).all())
        assert len(copies) == len(receipts) == 1
        assert copies[0].kind == "resident_input" and copies[0].message_id == "inbound"
        await users.erase_pdn_detailed(session, 42)
        assert copies[0].erase_requested is True


@pytest.mark.asyncio
async def test_restore_replay_does_not_extend_original_erasure_cutoff(erasure_db):
    db = erasure_db
    cutoff = datetime.now(UTC) - timedelta(days=1)
    digest = data_erasure.subject_digest(42)
    data_erasure._write_tombstone(digest, cutoff)
    async with db.scope() as session:
        resident = await users.find_by_max_id(session, 42)
        resident.created_at = cutoff - timedelta(days=1)
    async with db.scope() as session:
        await data_erasure.replay_restore_tombstones(session)
    async with db.scope() as session:
        session.add(User(max_user_id=42, first_name="Новый", created_at=cutoff + timedelta(hours=1)))
    async with db.scope() as session:
        assert await data_erasure.replay_restore_tombstones(session) == 0
        assert await users.find_by_max_id(session, 42) is not None


@pytest.mark.asyncio
async def test_stale_detached_appeal_cannot_send_after_erasure_even_with_explicit_old_id(erasure_db):
    db = erasure_db
    async with db.scope() as session:
        await users.erase_pdn_detailed(session, 42)
    bot = SimpleNamespace(send_message=AsyncMock())
    with pytest.raises(RuntimeError, match="source already erased"):
        await data_erasure.tracked_send(bot, kind="operator_part", appeal_id=db.appeal_id,
                                       max_user_ids=[42], user_id=42, text="stale private")
    bot.send_message.assert_not_awaited()
    with pytest.raises(RuntimeError, match="Resident already erased"):
        await data_erasure.tracked_send(bot, kind="citizen_screen", max_user_ids=[42], user_id=42, text="stale profile")
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_refuses_missing_journal_for_existing_db_and_initializes_only_explicit_install(erasure_db):
    with pytest.raises(RuntimeError, match="Existing DB requires"):
        await data_erasure.prepare_startup()
    await data_erasure.initialize_journal(allow_existing_database=True)
    assert await data_erasure.prepare_startup() == 0


@pytest.mark.asyncio
async def test_retention_journal_does_not_erase_reused_id_created_after_original_cutoff(erasure_db):
    db = erasure_db
    cutoff = datetime.now(UTC) - timedelta(days=1)
    key = data_erasure._key()
    import hashlib
    import hmac
    digest = hmac.new(key, f"appeal:{db.appeal_id}".encode(), hashlib.sha256).hexdigest()
    path = db.path / "journal" / f"{digest}.retention.json"
    import json
    path.write_text(json.dumps({"appeal_id": db.appeal_id, "erased_before": cutoff.isoformat()}))
    async with db.scope() as session:
        item = await session.get(Appeal, db.appeal_id)
        item.created_at = cutoff + timedelta(hours=1)
    async with db.scope() as session:
        assert await data_erasure.replay_restore_tombstones(session) == 0
        assert (await session.get(Appeal, db.appeal_id)).summary == "Private"


@pytest.mark.asyncio
async def test_independent_copy_inventory_survives_old_db_restore_without_resident(erasure_db):
    from sqlalchemy import delete
    db = erasure_db
    await _send(db, mid="post-dump-card")
    async with db.scope() as session:
        await users.erase_pdn_detailed(session, 42)
    bot = SimpleNamespace(delete_message=AsyncMock(side_effect=TimeoutError()))
    await data_erasure.recover_deletions(bot)
    async with db.scope() as session:
        await session.execute(delete(ResidentDataCopy))
        await session.execute(delete(ErasureTombstone))
        assert await users.find_by_max_id(session, 42) is None
    async with db.scope() as session:
        assert await data_erasure.replay_restore_tombstones(session) == 0
        copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.message_id == "post-dump-card"))
        assert copy is not None and copy.erase_requested and copy.state == "uncertain"
        assert await session.scalar(select(ErasureTombstone.subject_digest)) is not None
    before = bot.delete_message.await_count
    await data_erasure.recover_deletions(bot)
    assert bot.delete_message.await_count == before


@pytest.mark.asyncio
async def test_private_chat_id_context_and_generic_admin_hook_preserve_positional_sdk_arguments(erasure_db, monkeypatch):
    from aemr_bot.services import admin_bus
    from aemr_bot.utils.resident_lifecycle import current_subject, guard
    db = erasure_db
    calls = []
    async def sdk_send(chat_id=None, user_id=None, text=None, **kwargs):
        calls.append((chat_id, user_id, text))
        return SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid=f"screen-{len(calls)}")))
    bot = SimpleNamespace(send_message=sdk_send, edit_message=AsyncMock())
    admin_bus.install_outgoing_tracker_hook(bot)
    data_erasure.install_outgoing_hook(bot)
    async with guard(42):
        await bot.send_message(chat_id=99999, text="private contact prompt")
        async with guard(43):
            assert current_subject.get() == 43
        assert current_subject.get() == 42
        await bot.send_message(None, 42, "positional private")
    assert current_subject.get() is None
    assert calls == [(99999, None, "private contact prompt"), (None, 42, "positional private")]
    async with db.scope() as session:
        copies = list((await session.scalars(select(ResidentDataCopy))).all())
        assert len(copies) == 2
        assert all(copy.subjects == [data_erasure.subject_digest(42)] for copy in copies)


@pytest.mark.asyncio
async def test_restore_advances_appeal_sequence_beyond_independent_records(erasure_db):
    import json
    db = erasure_db
    data_erasure._key()
    (db.path / "journal" / "past.retention.json").write_text(json.dumps({
        "appeal_id": 999, "erased_before": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
    }))
    async with db.scope() as session:
        await data_erasure.replay_restore_tombstones(session)
        resident = await users.find_by_max_id(session, 43)
        appeal = Appeal(user_id=resident.id, summary="new unrelated")
        session.add(appeal)
        await session.flush()
        assert appeal.id > 999


@pytest.mark.asyncio
async def test_retention_cleared_appeal_cannot_reopen_or_accept_new_parts(erasure_db):
    from aemr_bot.handlers import appeal_funnel
    from aemr_bot.services import appeals
    from tests._helpers import make_event
    db = erasure_db
    async with db.scope() as session:
        item = await session.get(Appeal, db.appeal_id)
        item.status = "closed"
        await data_erasure.request_appeal_cleanup(session, [item.id])
        await session.refresh(item)
        assert item.content_erased_at is not None
        assert await appeals.reopen(session, item.id) == "content_erased"
        with pytest.raises(appeals.ContentErasedError):
            await appeals.add_user_message(session, item, text="new private")
        with pytest.raises(appeals.ContentErasedError):
            await appeals.add_operator_message(session, item, text="new reply", operator_id=None, max_message_id=None)
    # A resident draft is retained when the target is cleared during composition.
    event = make_event(user_id=42)
    async with db.scope() as session:
        resident = await users.find_by_max_id(session, 42)
        resident.dialog_state = "awaiting_followup_text"
        resident.dialog_data = {"appeal_id": db.appeal_id, "draft_token": "draft", "summary_chunks": ["new private"]}
    from unittest.mock import patch
    @asynccontextmanager
    async def current_user(uid):
        async with db.scope() as session:
            yield session, await users.find_by_max_id(session, uid)
    with patch.object(appeal_funnel, "current_user", current_user):
        await appeal_funnel.submit_followup(event, SimpleNamespace(attachments=[]), "new private", 42, draft_token="draft")
    assert "Черновик" in event.message.answer.await_args.args[0]
    async with db.scope() as session:
        resident = await users.find_by_max_id(session, 42)
        assert resident.dialog_data["summary_chunks"] == ["new private"]


@pytest.mark.asyncio
async def test_expired_open_appeal_leaves_operational_queues_but_preserves_history_and_draft(erasure_db, monkeypatch):
    from aemr_bot.handlers import appeal_funnel
    from aemr_bot.services import appeals
    from tests._helpers import make_event
    db = erasure_db
    old = datetime.now(UTC) - timedelta(days=365 * 7)
    async with db.scope() as session:
        item = await session.get(Appeal, db.appeal_id)
        item.created_at = old
        message = await session.scalar(select(Message).where(Message.appeal_id == item.id))
        message.created_at = old
        resident = await users.find_by_max_id(session, 42)
        resident.dialog_state = "awaiting_followup_text"
        resident.dialog_data = {"appeal_id": item.id, "draft_token": "draft", "summary_chunks": ["new private"]}
    async with db.scope() as session:
        assert await appeals.purge_old_appeals_content(session) == (1, 1)
        resident = await users.find_by_max_id(session, 42)
        assert (await session.get(Appeal, db.appeal_id)).status == "new"
        assert await appeals.count_open(session) == 1
        assert [item.id for item in await appeals.list_unanswered(session)] == [db.other_id]
        assert [item.id for item in await appeals.list_unanswered_with_messages(session)] == [db.other_id]
        assert await appeals.list_unanswered_for_user(session, resident.id) == []
        assert await appeals.find_active_for_user(session, resident.id) is None
        assert await appeals.find_overdue_unanswered(session, sla_hours=1) == []
        assert [item.id for item in await appeals.list_for_user(session, resident.id)] == [db.appeal_id]
        assert resident.dialog_data["summary_chunks"] == ["new private"]
    @asynccontextmanager
    async def current_user(uid):
        async with db.scope() as session:
            yield session, await users.find_by_max_id(session, uid)
    monkeypatch.setattr(appeal_funnel, "current_user", current_user)
    event = make_event(user_id=42)
    await appeal_funnel.submit_followup(event, SimpleNamespace(attachments=[]), "new private", 42, draft_token="draft")
    assert "новое обращение" in event.message.answer.await_args.args[0]
    async with db.scope() as session:
        assert (await users.find_by_max_id(session, 42)).dialog_data["summary_chunks"] == ["new private"]


@pytest.mark.asyncio
async def test_personal_view_tracks_admin_navigation_and_blocks_stale_source(erasure_db):
    db = erasure_db
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid="admin-view")))), edit_message=AsyncMock())
    original_send = bot.send_message
    data_erasure.install_outgoing_hook(bot)
    async with data_erasure.personal_view(appeal_id=db.appeal_id, max_user_ids=[42]):
        async with db.scope() as session:
            text = (await session.get(Appeal, db.appeal_id)).summary
        await bot.send_message(chat_id=data_erasure.settings.admin_group_id, text=text)
    async with db.scope() as session:
        copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.message_id == "admin-view"))
        assert copy.appeal_id == db.appeal_id and copy.subjects == [data_erasure.subject_digest(42)]
        await users.erase_pdn_detailed(session, 42)
        assert copy.erase_requested
    before = original_send.await_count
    with pytest.raises(data_erasure.PersonalDataErasedError):
        async with data_erasure.personal_view(appeal_id=db.appeal_id, max_user_ids=[42]):
            await bot.send_message(chat_id=data_erasure.settings.admin_group_id, text="stale private")
    assert original_send.await_count == before


@pytest.mark.asyncio
async def test_personal_view_holds_erasure_barrier_before_read_until_output(erasure_db):
    db = erasure_db
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid="racing-view")))), edit_message=AsyncMock())
    data_erasure.install_outgoing_hook(bot)
    async def erase():
        async with db.scope() as session:
            await users.erase_pdn_detailed(session, 42)
    async with data_erasure.personal_view(appeal_id=db.appeal_id):
        async with db.scope() as session:
            private = (await session.get(Appeal, db.appeal_id)).summary
        erasing = asyncio.create_task(erase())
        await asyncio.sleep(.03)
        assert not erasing.done()
        await bot.send_message(chat_id=data_erasure.settings.admin_group_id, text=private)
    await erasing
    async with db.scope() as session:
        copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.message_id == "racing-view"))
        assert copy.erase_requested and copy.state == "pending"


@pytest.mark.asyncio
async def test_personal_edit_commits_union_before_unknown_external_result(erasure_db):
    db = erasure_db
    expected = {data_erasure.subject_digest(42), data_erasure.subject_digest(43)}
    async def inspect_committed_then_timeout(*args, **kwargs):
        async with db.scope() as session:
            copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.message_id == "navigation"))
            assert set(copy.subjects) == expected
            assert copy.appeal_id is None
        raise TimeoutError("MAX may have applied the edit")
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid="navigation")))), edit_message=inspect_committed_then_timeout)
    data_erasure.install_outgoing_hook(bot)
    async with data_erasure.personal_view(appeal_id=db.appeal_id):
        await bot.send_message(chat_id=data_erasure.settings.admin_group_id, text="resident42")
    with pytest.raises(TimeoutError):
        async with data_erasure.personal_view(appeal_id=db.other_id):
            await bot.edit_message(message_id="navigation", text="resident43")
    async with db.scope() as session:
        await users.erase_pdn_detailed(session, 42)
        copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.message_id == "navigation"))
        assert copy.erase_requested and copy.state == "pending"
        assert (await session.get(Appeal, db.other_id)).summary == "Preserve"


@pytest.mark.asyncio
async def test_personal_edit_inventories_previously_untracked_mid(erasure_db):
    db = erasure_db
    bot = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock())
    data_erasure.install_outgoing_hook(bot)
    async with data_erasure.personal_view() as view:
        await view.include(appeal_id=db.appeal_id)
        await bot.edit_message(message_id="previous-menu", text="private history")
    async with db.scope() as session:
        copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.message_id == "previous-menu"))
        assert copy.appeal_id == db.appeal_id and copy.subjects == [data_erasure.subject_digest(42)]
        await users.erase_pdn_detailed(session, 42)
        assert copy.erase_requested


@pytest.mark.asyncio
async def test_repeat_retention_preserves_clean_reports_and_unrelated_personal_views(erasure_db):
    from aemr_bot.services import appeals
    db = erasure_db
    sequence = iter(["user42-list", "user43-list", "audience", "clean-report"])
    async def send(**kwargs):
        return SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid=next(sequence))))
    bot = SimpleNamespace(send_message=send, edit_message=AsyncMock())
    data_erasure.install_outgoing_hook(bot)
    for uid in [42, 43]:
        async with data_erasure.personal_view(max_user_ids=[uid]):
            await bot.send_message(chat_id=data_erasure.settings.admin_group_id, text="personal list")
    async with data_erasure.personal_view(aggregate=True):
        await bot.send_message(chat_id=data_erasure.settings.admin_group_id, text="all residents")
    old = datetime.now(UTC) - timedelta(days=365 * 7)
    async with db.scope() as session:
        item = await session.get(Appeal, db.appeal_id)
        item.created_at = old
        message = await session.scalar(select(Message).where(Message.appeal_id == item.id))
        message.created_at = old
    async with db.scope() as session:
        assert await appeals.purge_old_appeals_content(session) == (1, 1)
        copies = {copy.message_id: copy for copy in (await session.scalars(select(ResidentDataCopy))).all()}
        assert copies["user42-list"].erase_requested
        assert copies["audience"].erase_requested
        assert not copies["user43-list"].erase_requested
    await data_erasure.tracked_send(bot, kind="bulk_export", text="clean post-retention report")
    async with db.scope() as session:
        assert await appeals.purge_old_appeals_content(session) == (0, 0)
        copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.message_id == "clean-report"))
        assert copy.state == "retained" and not copy.erase_requested
