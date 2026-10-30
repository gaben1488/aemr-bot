"""Real PostgreSQL and the production hook cover personal operator views."""
import asyncio
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
from sqlalchemy import select

from aemr_bot.db.models import Appeal, AuditLog, Message, OperatorReplyAttempt, ResidentDataCopy, User
from aemr_bot.db.session import session_scope
from aemr_bot.services import data_erasure, users
from tests._helpers import make_event


@pytest.fixture
async def source(session, monkeypatch):
    user = User(max_user_id=800201, first_name="PRIVATE_NAME", consent_pdn_at=datetime.now(UTC),
        consent_records=[{"accepted_at": datetime.now(UTC).isoformat(), "text": "PRIVATE_CONSENT"}])
    session.add(user)
    await session.flush()
    appeal = Appeal(user_id=user.id, summary="PRIVATE_REPORT", status="new", attachments=[])
    session.add(appeal)
    await session.flush()
    session.add(Message(appeal_id=appeal.id, direction="from_user", text="PRIVATE_FOLLOWUP"))
    attempt = OperatorReplyAttempt(appeal_id=appeal.id, text="PRIVATE_ANSWER", attachments=[],
                                   is_final=False, state="unknown")
    session.add(attempt)
    await session.commit()
    event = make_event(chat_id=123, user_id=77)
    event.callback = None
    raw_send = AsyncMock(return_value=SimpleNamespace(message_id="personal-view"))
    bot = SimpleNamespace(send_message=raw_send,
                          edit_message=AsyncMock(return_value=SimpleNamespace(success=True)))
    event.bot = bot
    data_erasure.install_outgoing_hook(bot)
    from aemr_bot.handlers import admin_callback_dispatch as dispatch
    monkeypatch.setattr(dispatch, "get_operator", AsyncMock(return_value=SimpleNamespace(id=77, role="it")))
    monkeypatch.setattr(dispatch, "ensure_operator", AsyncMock(return_value=True))
    monkeypatch.setattr(dispatch, "ack_callback", AsyncMock())
    return SimpleNamespace(event=event, appeal_id=appeal.id, attempt_id=attempt.id,
                           user_id=user.max_user_id, raw_send=raw_send)


@pytest.mark.parametrize("route", ["card", "history", "consent", "attempt"])
async def test_personal_admin_navigation_is_inventoried_and_erased(source, route):
    from aemr_bot.handlers import admin_callback_dispatch as dispatch
    if route == "card":
        await dispatch._op_open_card(source.event, source.appeal_id)
    elif route == "history":
        await dispatch._op_history(source.event, f"op:history:{source.appeal_id}:1")
    elif route == "consent":
        await dispatch._op_consent(source.event, f"op:consent:{source.appeal_id}:0")
    else:
        await dispatch._op_reply_attempt(source.event, f"op:reply_attempt:{source.attempt_id}:0")
    async with session_scope() as session:
        copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.message_id == "personal-view"))
        assert copy is not None
        assert data_erasure.subject_digest(source.user_id) in copy.subjects
        await users.erase_pdn_detailed(session, source.user_id)
    async with session_scope() as session:
        copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.message_id == "personal-view"))
        assert copy.erase_requested and copy.state == "manual"


async def test_personal_notification_is_inventoried_before_erasure(source):
    from aemr_bot.services import admin_events
    await admin_events.notify_consent_given(source.event.bot, max_user_id=source.user_id)
    async with session_scope() as session:
        copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.message_id == "personal-view"))
        assert copy is not None and data_erasure.subject_digest(source.user_id) in copy.subjects
        await users.erase_pdn_detailed(session, source.user_id)
    async with session_scope() as session:
        copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.message_id == "personal-view"))
        assert copy.erase_requested


@pytest.mark.parametrize("route", ["audience", "search"])
async def test_multi_resident_views_are_inventoried_before_erasure(source, route, monkeypatch):
    from aemr_bot.handlers import admin_audience, admin_resident_search
    if route == "audience":
        await admin_audience._render_audience_page(source.event, "consent", 1)
    else:
        monkeypatch.setattr(admin_resident_search, "ensure_operator", AsyncMock(return_value=True))
        await admin_resident_search.run_find_resident(source.event, str(source.user_id))
    async with session_scope() as session:
        copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.message_id == "personal-view"))
        assert copy is not None and copy.kind == "bulk_view"
        await users.erase_pdn_detailed(session, source.user_id)
    async with session_scope() as session:
        copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.message_id == "personal-view"))
        assert copy.erase_requested and copy.state == "manual"


@pytest.mark.parametrize("existing_mid", [None, "existing-preview"])
async def test_operator_draft_previews_register_initial_sends_and_edits(source, existing_mid):
    from aemr_bot.handlers import operator_draft
    draft = SimpleNamespace(appeal_id=source.appeal_id, operator_id=77, is_final=False,
        state="editing", token="draft-token",
        data={"summary_chunks": ["PRIVATE_DRAFT"], "_screen_mid": existing_mid})
    await operator_draft.show(source.event, draft)
    async with session_scope() as session:
        copy = await session.scalar(select(ResidentDataCopy).where(
            ResidentDataCopy.message_id == (existing_mid or "personal-view")))
        assert copy is not None and data_erasure.subject_digest(source.user_id) in copy.subjects
        await users.erase_pdn_detailed(session, source.user_id)
    async with session_scope() as session:
        copy = await session.scalar(select(ResidentDataCopy).where(
            ResidentDataCopy.message_id == (existing_mid or "personal-view")))
        assert copy.erase_requested


async def test_erased_source_cannot_republish_a_detached_operator_draft(source):
    from aemr_bot.handlers import operator_draft
    draft = SimpleNamespace(appeal_id=source.appeal_id, operator_id=77, is_final=False,
        state="editing", token="draft-token", data={"summary_chunks": ["PRIVATE_DRAFT"]})
    async with session_scope() as session:
        await users.erase_pdn_detailed(session, source.user_id)
    await operator_draft.show(source.event, draft)
    assert source.raw_send.await_count == 1
    assert "PRIVATE_DRAFT" not in source.raw_send.await_args.kwargs["text"]
    assert "очищено" in source.raw_send.await_args.kwargs["text"]


async def test_begin_does_not_create_draft_or_change_erased_appeal(source, monkeypatch):
    from sqlalchemy import func
    from aemr_bot.db.models import OperatorReplyDraft
    from aemr_bot.handlers import operator_draft
    monkeypatch.setattr(operator_draft, "get_operator", AsyncMock(
        return_value=SimpleNamespace(id=77, role="it")))
    monkeypatch.setattr(operator_draft, "ack_callback", AsyncMock())
    async with session_scope() as session:
        appeal = await session.get(Appeal, source.appeal_id)
        appeal.content_erased_at = datetime.now(UTC)
    await operator_draft.begin(source.event, source.appeal_id, is_final=False)
    async with session_scope() as session:
        assert (await session.get(Appeal, source.appeal_id)).status == "new"
        assert await session.scalar(select(func.count()).select_from(OperatorReplyDraft)) == 0


@pytest.mark.parametrize("query_kind", ["max_user_id", "phone"])
async def test_real_resident_search_audit_erases_subject_but_keeps_staff_trace(source, monkeypatch, query_kind):
    from aemr_bot.handlers import admin_resident_search
    monkeypatch.setattr(admin_resident_search, "ensure_operator", AsyncMock(return_value=True))
    async with session_scope() as session:
        resident = await users.find_by_max_id(session, source.user_id)
        resident.phone = "+79991234567"
        resident.phone_normalized = users._normalize_phone(resident.phone)
        session.add(AuditLog(operator_max_user_id=77, action="resident_search_found", target="800999",
            details={"kind": "max_user_id", "found_max_user_id": 800999}))
        session.add(AuditLog(operator_max_user_id=source.user_id, action="operator_update",
            target="operator 77", details={"actor_role": "it"}))
    query = str(source.user_id) if query_kind == "max_user_id" else "+79991234567"
    await admin_resident_search.run_find_resident(source.event, query)
    async with session_scope() as session:
        row = await session.scalar(select(AuditLog).where(AuditLog.action == "resident_search_found")
                                   .order_by(AuditLog.id.desc()))
        row_id, date = row.id, row.created_at
        # Structured ownership permits removal of resident details, including
        # older captured query fields, without removing unrelated staff facts.
        row.details = {**row.details, "query": query, "phone": "+79991234567",
                       "name": "PRIVATE_NAME", "operator_max_user_id": 77}
    async with session_scope() as session:
        await users.erase_pdn_detailed(session, source.user_id)
    async with session_scope() as session:
        row = await session.get(AuditLog, row_id)
        assert row.action == "resident_search_found" and row.operator_max_user_id == 77
        assert row.created_at == date
        assert row.target is None
        assert row.details == {"kind": query_kind, "operator_max_user_id": 77}
        other = await session.scalar(select(AuditLog).where(AuditLog.target == "800999"))
        assert other.details["found_max_user_id"] == 800999
        operator = await session.scalar(select(AuditLog).where(AuditLog.action == "operator_update"))
        assert operator.operator_max_user_id == source.user_id


async def test_word_upload_finishes_before_retention_can_erase_source(source, monkeypatch):
    from aemr_bot.handlers import admin_callback_dispatch as dispatch
    from aemr_bot.services import appeals, uploads
    async with session_scope() as session:
        appeal = await session.get(Appeal, source.appeal_id)
        appeal.created_at = datetime.now(UTC) - timedelta(days=2200)
        appeal.closed_at = appeal.created_at
        appeal.status = "closed"
        for message in (await session.scalars(select(Message).where(Message.appeal_id == appeal.id))):
            message.created_at = appeal.created_at
    cleanup = []
    async def purge():
        async with session_scope() as session:
            await appeals.purge_old_appeals_content(session)
    async def build_and_start_cleanup(function, *args):
        document = function(*args)
        task = asyncio.create_task(purge())
        cleanup.append(task)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.15)
        except TimeoutError:
            pass
        return document
    async def upload(bot, document, **kwargs):
        async with session_scope() as session:
            appeal = await session.get(Appeal, source.appeal_id)
            assert appeal.content_erased_at is None, "upload must precede source erasure"
        with ZipFile(BytesIO(document)) as archive:
            assert b"PRIVATE_REPORT" in archive.read("word/document.xml")
        return "synthetic-token"
    monkeypatch.setattr(asyncio, "to_thread", build_and_start_cleanup)
    monkeypatch.setattr(uploads, "upload_bytes", upload)
    try:
        await dispatch._op_word(source.event, source.appeal_id)
    finally:
        await asyncio.gather(*cleanup)
    async with session_scope() as session:
        assert (await session.get(Appeal, source.appeal_id)).content_erased_at is not None
