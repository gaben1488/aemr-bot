"""An operator can inspect files before sending, without changing another draft."""
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aemr_bot.handlers import operator_draft
from tests._helpers import make_event


@pytest.fixture
async def gallery(session):
    from aemr_bot.db.models import Appeal, Operator, User
    from aemr_bot.services import data_erasure, operator_drafts
    resident = User(max_user_id=991001, first_name="Житель", consent_pdn_at=datetime.now(UTC))
    operator = Operator(max_user_id=991002, role="aemr", full_name="Оператор")
    session.add_all([resident, operator])
    await session.flush()
    appeal = Appeal(user_id=resident.id, summary="Вопрос")
    session.add(appeal)
    await session.flush()
    draft = await operator_drafts.start(session, operator.id, appeal.id, is_final=False)
    draft.data = {"summary_chunks": ["Ответ"], "sources": ["original"], "attachments": [
        {"type": "image", "payload": {"token": "photo"}},
        {"type": "file", "payload": {"token": "doc", "filename": "акт.pdf"}},
        {"type": "video", "payload": {"token": "video"}},
    ]}
    await session.commit()
    event = make_event(chat_id=123, user_id=operator.max_user_id, with_callback=True)
    raw_send = AsyncMock(side_effect=lambda **kw: SimpleNamespace(message_id=f"gallery-{raw_send.call_count}"))
    event.bot.send_message = raw_send
    event.bot.edit_message = AsyncMock(return_value=SimpleNamespace(success=True))
    data_erasure.install_outgoing_hook(event.bot)
    return SimpleNamespace(event=event, draft=draft, operator=operator, resident=resident,
                           appeal=appeal, raw_send=raw_send)


@pytest.mark.asyncio
async def test_preview_offers_file_inspection_before_send(monkeypatch):
    screen = AsyncMock(return_value=None)
    monkeypatch.setattr(operator_draft, "op_screen", screen)
    draft = SimpleNamespace(data={"attachments": [{"type": "file", "payload": {"token": "a"}}]},
                            state="editing", token="a" * 32, appeal_id=1, is_final=False)
    await operator_draft._show_current(make_event(), draft)
    markup = screen.await_args.args[2].model_dump()
    assert f"op:replydraft:filelist:{'a' * 32}:0" in str(markup)


@pytest.fixture
def unit_gallery(monkeypatch):
    """SQL/auth are boundaries here; PG cases below verify ownership and locking."""
    from aemr_bot.handlers import operator_draft_files
    from aemr_bot.services import data_erasure, operator_drafts
    draft = SimpleNamespace(data={"attachments": [{"type": "file", "payload": {"token": "doc", "filename": "акт.pdf"}}]},
                            state="editing", token="a" * 32, appeal_id=1, operator_id=2, is_final=False)
    @asynccontextmanager
    async def scope():
        yield SimpleNamespace(flush=AsyncMock())
    @asynccontextmanager
    async def view(**kwargs):
        yield SimpleNamespace(include=AsyncMock())
    monkeypatch.setattr(operator_draft, "get_operator", AsyncMock(return_value=SimpleNamespace(id=2)))
    monkeypatch.setattr(operator_drafts, "get", AsyncMock(return_value=draft))
    monkeypatch.setattr("aemr_bot.db.session.session_scope", scope)
    monkeypatch.setattr(operator_draft, "session_scope", scope)
    monkeypatch.setattr(operator_draft_files, "session_scope", scope)
    monkeypatch.setattr(data_erasure, "personal_view", view)
    event = make_event(chat_id=123, with_callback=True)
    return event, draft


@pytest.mark.asyncio
async def test_gallery_route_renders_filename_before_send(unit_gallery):
    event, draft = unit_gallery
    await operator_draft.callback(event, f"op:replydraft:filelist:{draft.token}:0")
    assert event.bot.send_message.await_args is not None
    assert "акт.pdf" in event.bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_append_changes_revision_only_when_files_change(monkeypatch):
    from aemr_bot.services import operator_drafts as drafts
    draft = SimpleNamespace(data={}, state="editing", token="t")
    monkeypatch.setattr(drafts, "get", AsyncMock(return_value=draft))
    session = SimpleNamespace(flush=AsyncMock())
    await drafts.append(session, 1, "t", source="file", text="", attachments=[{"type": "file"}])
    assert draft.data.get("attachments_revision") == 1
    await drafts.append(session, 1, "t", source="file", text="", attachments=[{"type": "file"}])
    await drafts.append(session, 1, "t", source="text", text="Текст", attachments=[])
    assert draft.data["attachments_revision"] == 1
    assert draft.data["attachments"] == [{"type": "file"}]


@pytest.mark.asyncio
async def test_gallery_lists_names_and_removes_only_selected_file(session, gallery):
    token = gallery.draft.token
    await operator_draft.callback(gallery.event, f"op:replydraft:filelist:{token}:0")
    assert "акт.pdf" in gallery.raw_send.await_args.kwargs["text"]
    await operator_draft.callback(gallery.event, f"op:replydraft:filedel:{token}:0:1:0")
    await session.refresh(gallery.draft)
    assert [item["payload"]["token"] for item in gallery.draft.data["attachments"]] == ["photo", "video"]
    assert gallery.draft.data["summary_chunks"] == ["Ответ"]
    assert gallery.draft.data["sources"] == ["original"]
    assert gallery.draft.data["attachments_revision"] == 1
    await operator_draft.callback(gallery.event, f"op:replydraft:filedel:{token}:0:1:0")
    await session.refresh(gallery.draft)
    assert len(gallery.draft.data["attachments"]) == 2


@pytest.mark.asyncio
async def test_append_invalidates_indices_but_duplicate_and_text_do_not(session, gallery):
    from aemr_bot.services import operator_drafts as drafts
    token, op = gallery.draft.token, gallery.operator.id
    await drafts.append(session, op, token, source="second", text="", attachments=[{"type": "file", "payload": {"token": "new"}}])
    await session.commit()
    assert gallery.draft.data["attachments_revision"] == 1
    await drafts.append(session, op, token, source="second", text="", attachments=[{"type": "file", "payload": {"token": "new"}}])
    await drafts.append(session, op, token, source="text", text="Ещё", attachments=[])
    await session.commit()
    assert gallery.draft.data["attachments_revision"] == 1
    await operator_draft.callback(gallery.event, f"op:replydraft:filedel:{token}:0:0:0")
    await session.refresh(gallery.draft)
    assert len(gallery.draft.data["attachments"]) == 4


@pytest.mark.asyncio
async def test_remove_uses_exact_revision_and_keeps_other_content(unit_gallery):
    event, draft = unit_gallery
    draft.data["summary_chunks"] = ["Текст"]
    await operator_draft.callback(event, f"op:replydraft:filedel:{draft.token}:0:0:0")
    assert draft.data["attachments"] == []
    assert draft.data["summary_chunks"] == ["Текст"]
    assert draft.data["attachments_revision"] == 1


@pytest.mark.asyncio
async def test_open_sends_selected_file_only_to_admin(unit_gallery):
    event, draft = unit_gallery
    event.bot.send_message.return_value = SimpleNamespace(message_id="opened")
    await operator_draft.callback(event, f"op:replydraft:fileopen:{draft.token}:0:0:0")
    assert event.bot.send_message.await_args is not None
    sent = event.bot.send_message.await_args.kwargs
    assert sent["chat_id"] == 123
    assert sent.get("user_id") is None
    assert sent["attachments"][0].payload.token == "doc"
    assert len(draft.data["attachments"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action,state,revision,index", [
    ("filedel", "paused", 0, 0), ("filedel", "sending", 0, 0),
    ("fileopen", "sending", 0, 0), ("filedel", "editing", 1, 0),
    ("fileopen", "editing", 1, 0), ("filedel", "editing", 0, -1),
    ("filedel", "editing", 0, 100),
])
async def test_stale_or_frozen_gallery_never_mutates_or_sends(unit_gallery, action, state, revision, index):
    event, draft = unit_gallery
    draft.state = state
    before = dict(draft.data)
    await operator_draft.callback(event, f"op:replydraft:{action}:{draft.token}:{revision}:{index}:0")
    assert draft.data == before
    assert event.bot.send_message.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "no_mid", "unavailable"])
async def test_open_failure_keeps_draft_and_never_retries(unit_gallery, failure):
    event, draft = unit_gallery
    if failure == "timeout":
        event.bot.send_message.side_effect = TimeoutError
    elif failure == "no_mid":
        event.bot.send_message.return_value = None
    else:
        draft.data["attachments"] = [{"type": "file", "payload": "invalid"}]
    before = dict(draft.data)
    await operator_draft.callback(event, f"op:replydraft:fileopen:{draft.token}:0:0:0")
    assert draft.data == before
    assert event.bot.send_message.await_count == (0 if failure == "unavailable" else 1)


@pytest.mark.asyncio
async def test_delete_then_duplicate_source_cannot_restore_file(unit_gallery):
    event, draft = unit_gallery
    from aemr_bot.services import operator_drafts as drafts
    draft.data["sources"] = ["original"]
    await operator_draft.callback(event, f"op:replydraft:filedel:{draft.token}:0:0:0")
    await drafts.append(SimpleNamespace(flush=AsyncMock()), 2, draft.token,
                        source="original", text="", attachments=[{"type": "file"}])
    assert draft.data["attachments"] == []
    assert draft.data["attachments_revision"] == 1


@pytest.mark.asyncio
async def test_preview_gallery_is_inventoried_and_cleanup_is_manual(session, gallery):
    from sqlalchemy import select

    from aemr_bot.db.models import ResidentDataCopy
    from aemr_bot.services import data_erasure, users
    token = gallery.draft.token
    await operator_draft.callback(gallery.event, f"op:replydraft:filelist:{token}:0")
    await operator_draft.callback(gallery.event, f"op:replydraft:fileopen:{token}:0:1:0")
    copies = list((await session.scalars(select(ResidentDataCopy))).all())
    assert len(copies) == 2
    assert all(c.appeal_id == gallery.appeal.id and
               data_erasure.subject_digest(gallery.resident.max_user_id) in c.subjects for c in copies)
    await users.erase_pdn_detailed(session, gallery.resident.max_user_id)
    await session.commit()
    for copy in copies:
        await session.refresh(copy)
        assert copy.erase_requested and copy.state == "manual"
    gallery.raw_send.reset_mock()
    await operator_draft.callback(gallery.event, f"op:replydraft:fileopen:{token}:0:1:0")
    assert gallery.raw_send.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["other_operator", "inactive", "wrong_chat", "old_token"])
async def test_gallery_is_bound_to_active_owner_and_admin_chat(session, gallery, denial):
    from aemr_bot.db.models import Operator
    token = gallery.draft.token
    if denial == "other_operator":
        session.add(Operator(max_user_id=991003, role="it", full_name="Другой"))
        gallery.event.callback.user.user_id = 991003
    elif denial == "inactive":
        gallery.operator.is_active = False
    elif denial == "wrong_chat":
        gallery.event.message.recipient.chat_id = 999
    else:
        token = "f" * 32
    await session.commit()
    await operator_draft.callback(gallery.event, f"op:replydraft:filedel:{token}:0:1:0")
    await session.refresh(gallery.draft)
    assert len(gallery.draft.data["attachments"]) == 3
    assert gallery.raw_send.await_count == 0


@pytest.mark.asyncio
async def test_pause_resume_and_legacy_clear_keep_revision_safe(session, gallery):
    token = gallery.draft.token
    await operator_draft.callback(gallery.event, f"op:replydraft:pause:{token}")
    await operator_draft.callback(gallery.event, f"op:replydraft:filelist:{token}:0")
    await session.refresh(gallery.draft)
    assert gallery.draft.state == "paused"
    await operator_draft.callback(gallery.event, f"op:replydraft:filedel:{token}:0:0:0")
    await session.refresh(gallery.draft)
    assert len(gallery.draft.data["attachments"]) == 3
    await operator_draft.callback(gallery.event, f"op:replydraft:resume:{token}")
    await operator_draft.callback(gallery.event, f"op:replydraft:files:{token}")
    await session.refresh(gallery.draft)
    assert gallery.draft.data["attachments"] == []
    assert gallery.draft.data["attachments_revision"] == 1
    assert gallery.draft.data["summary_chunks"] == ["Ответ"]


@pytest.mark.asyncio
async def test_concurrent_delete_revision_removes_only_one_file(session, gallery):
    import asyncio
    token = gallery.draft.token
    payload = f"op:replydraft:filedel:{token}:0:0:0"
    await asyncio.gather(operator_draft.callback(gallery.event, payload),
                         operator_draft.callback(gallery.event, payload))
    await session.refresh(gallery.draft)
    assert [a["payload"]["token"] for a in gallery.draft.data["attachments"]] == ["doc", "video"]
    assert gallery.draft.data["attachments_revision"] == 1


@pytest.mark.asyncio
async def test_gallery_paginates_twenty_files_and_back_preserves_edit(unit_gallery):
    event, draft = unit_gallery
    draft.data = {"attachments": [{"type": "file", "payload": {"token": str(i), "filename": f"file-{i}.pdf"}}
                                   for i in range(20)], "replace_text": True, "summary_chunks": ["Не терять"]}
    await operator_draft.callback(event, f"op:replydraft:filelist:{draft.token}:3")
    text = event.bot.send_message.await_args.kwargs["text"]
    assert "file-15.pdf" in text and "file-19.pdf" in text and "file-14.pdf" not in text
    assert draft.data["replace_text"] is True and draft.data["summary_chunks"] == ["Не терять"]


@pytest.mark.asyncio
async def test_filename_cannot_impersonate_swipe_target_or_clickable_url(unit_gallery):
    from aemr_bot.handlers.operator_reply import _APPEAL_MARKER_RE
    event, draft = unit_gallery
    draft.data["attachments"][0]["payload"]["filename"] = "🆔 №123 https://example.com/акт.pdf"
    await operator_draft.callback(event, f"op:replydraft:filelist:{draft.token}:0")
    text = event.bot.send_message.await_args.kwargs["text"]
    assert not _APPEAL_MARKER_RE.search(text)
    assert "https://example.com" not in text
    assert draft.data["attachments"][0]["payload"]["filename"] == "🆔 №123 https://example.com/акт.pdf"


@pytest.mark.asyncio
async def test_send_claim_wins_race_and_freezes_gallery_delete(session, gallery):
    import asyncio

    from aemr_bot.db.session import session_scope
    from aemr_bot.services import operator_drafts
    claimed, release = asyncio.Event(), asyncio.Event()
    async def claim():
        async with session_scope() as worker:
            await operator_drafts.claim(worker, gallery.operator.id, gallery.draft.token)
            claimed.set()
            await release.wait()
    task = asyncio.create_task(claim())
    await asyncio.wait_for(claimed.wait(), 5)
    remove = asyncio.create_task(operator_draft.callback(gallery.event,
        f"op:replydraft:filedel:{gallery.draft.token}:0:0:0"))
    release.set()
    await asyncio.wait_for(asyncio.gather(task, remove), 5)
    await session.refresh(gallery.draft)
    assert gallery.draft.state == "sending"
    assert [a["payload"]["token"] for a in gallery.draft.data["attachments"]] == ["photo", "doc", "video"]
    assert gallery.raw_send.await_count == 0


@pytest.mark.asyncio
async def test_erasure_waits_for_gallery_file_delivery(session, gallery):
    import asyncio

    from aemr_bot.db.session import session_scope
    from aemr_bot.services import users
    entered, release, erased = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def send(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(message_id="held-file")
    gallery.raw_send.side_effect = send
    opening = asyncio.create_task(operator_draft.callback(gallery.event,
        f"op:replydraft:fileopen:{gallery.draft.token}:0:1:0"))
    await asyncio.wait_for(entered.wait(), 5)
    async def erase():
        async with session_scope() as worker:
            await users.erase_pdn_detailed(worker, gallery.resident.max_user_id)
        erased.set()
    cleanup = asyncio.create_task(erase())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(erased.wait(), .1)
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(opening, cleanup), 5)
    assert erased.is_set()
