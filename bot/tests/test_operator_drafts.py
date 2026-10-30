"""Durable composition: changes are explicit and scoped to operator and token."""
import pytest

from aemr_bot.db.models import Appeal, Operator, User


async def seed(session):
    user = User(max_user_id=700201, first_name="Анна")
    operator = Operator(max_user_id=700202, role="aemr", full_name="Оператор")
    session.add_all([user, operator])
    await session.flush()
    appeal = Appeal(user_id=user.id, summary="Вопрос")
    session.add(appeal)
    await session.flush()
    return appeal, operator


@pytest.mark.asyncio
async def test_draft_survives_reload_and_duplicate_part(session):
    from aemr_bot.services import operator_drafts as drafts
    appeal, operator = await seed(session)
    draft = await drafts.start(session, operator.id, appeal.id, is_final=False)
    token = draft.token
    await drafts.append(session, operator.id, token, source="event-1", text="Первая часть", attachments=[])
    await drafts.append(session, operator.id, token, source="event-1", text="Первая часть", attachments=[])
    operator_id = operator.id
    session.expire_all()
    restored = await drafts.get(session, operator_id)
    assert restored.token == token
    assert restored.data["summary_chunks"] == ["Первая часть"]
    assert restored.is_final is False


@pytest.mark.asyncio
async def test_old_token_and_other_operator_cannot_change_draft(session):
    from aemr_bot.services import operator_drafts as drafts
    appeal, operator = await seed(session)
    draft = await drafts.start(session, operator.id, appeal.id, is_final=True)
    with pytest.raises(drafts.DraftConflict):
        await drafts.append(session, operator.id, "old-token", source="1", text="Чужой", attachments=[])
    assert await drafts.cancel(session, operator.id + 1, draft.token) is False
    assert (await drafts.get(session, operator.id)).data == {}


@pytest.mark.asyncio
async def test_limit_failure_keeps_existing_parts_and_claim_freezes_draft(session):
    from aemr_bot.services import operator_drafts as drafts
    appeal, operator = await seed(session)
    draft = await drafts.start(session, operator.id, appeal.id, is_final=True)
    await drafts.append(session, operator.id, draft.token, source="1", text="А" * 2999, attachments=[])
    with pytest.raises(ValueError):
        await drafts.append(session, operator.id, draft.token, source="2", text="Б", attachments=[])
    assert (await drafts.get(session, operator.id)).data["summary_chunks"] == ["А" * 2999]
    claimed = await drafts.claim(session, operator.id, draft.token)
    assert claimed.state == "sending"
    with pytest.raises(drafts.DraftConflict):
        await drafts.claim(session, operator.id, draft.token)
    with pytest.raises(drafts.DraftConflict):
        await drafts.append(session, operator.id, draft.token, source="3", text="", attachments=[])


@pytest.mark.asyncio
async def test_start_does_not_silently_replace_or_retarget_existing_draft(session):
    from aemr_bot.services import operator_drafts as drafts
    appeal, operator = await seed(session)
    old = await drafts.start(session, operator.id, appeal.id, is_final=False)
    with pytest.raises(drafts.DraftConflict):
        await drafts.start(session, operator.id, appeal.id, is_final=True)
    assert (await drafts.get(session, operator.id)).token == old.token


@pytest.mark.asyncio
async def test_replacement_is_atomic_and_keeps_attachments(session):
    from aemr_bot.services import operator_drafts as drafts
    appeal, operator = await seed(session)
    draft = await drafts.start(session, operator.id, appeal.id, is_final=True)
    files = [{"type": "file", "payload": {"token": "one"}}]
    await drafts.append(session, operator.id, draft.token, source="1", text="Старый текст", attachments=files)
    draft.data = {**draft.data, "replace_text": True}
    await session.flush()
    with pytest.raises(ValueError):
        await drafts.append(session, operator.id, draft.token, source="2", text="Б" * 3001, attachments=[])
    restored = await drafts.get(session, operator.id)
    assert restored.data["summary_chunks"] == ["Старый текст"]
    assert restored.data["replace_text"] is True
    await drafts.append(session, operator.id, draft.token, source="3", text="Новый текст", attachments=[])
    assert draft.data["summary_chunks"] == ["Новый текст"]
    assert draft.data["attachments"] == files


@pytest.mark.asyncio
async def test_concurrent_send_claims_only_once(session):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from aemr_bot.services import operator_drafts as drafts

    appeal, operator = await seed(session)
    draft = await drafts.start(session, operator.id, appeal.id, is_final=True)
    operator_id, token = operator.id, draft.token
    await drafts.append(session, operator_id, token, source="1", text="Ответ", attachments=[])
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    async def claim():
        async with factory() as worker:
            try:
                await drafts.claim(worker, operator_id, token)
                await worker.commit()
                return True
            except drafts.DraftConflict:
                return False
    assert sorted(await asyncio.gather(claim(), claim())) == [False, True]


@pytest.mark.asyncio
async def test_erasure_removes_operator_composition(session):
    from aemr_bot.services import operator_drafts as drafts
    from aemr_bot.services import users
    appeal, operator = await seed(session)
    draft = await drafts.start(session, operator.id, appeal.id, is_final=True)
    await drafts.append(session, operator.id, draft.token, source="1", text="ПДн в ответе", attachments=[])
    await users._redact_appeal_payloads_for_user(session, appeal.user_id)
    assert await drafts.get(session, operator.id) is None


@pytest.mark.asyncio
async def test_restart_thaws_only_claims_without_a_delivery_reservation(session):
    from aemr_bot.db.models import OperatorReplyAttempt
    from aemr_bot.services import operator_drafts as drafts
    appeal, operator = await seed(session)
    draft = await drafts.start(session, operator.id, appeal.id, is_final=True)
    await drafts.append(session, operator.id, draft.token, source="1", text="Ответ", attachments=[])
    await drafts.claim(session, operator.id, draft.token)
    assert await drafts.recover_unreserved(session) == 1
    assert (await drafts.get(session, operator.id)).state == "editing"
    await drafts.claim(session, operator.id, draft.token)
    session.add(OperatorReplyAttempt(appeal_id=appeal.id, operator_id=operator.id,
        source_key="reply_draft:" + draft.token, text="Ответ", attachments=[],
        is_final=True, state="sending"))
    await session.flush()
    assert await drafts.recover_unreserved(session) == 0
    assert (await drafts.get(session, operator.id)).state == "sending"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["confirmed", "recorded"])
async def test_history_recovery_releases_matching_frozen_composition(session, state):
    from aemr_bot.db.models import OperatorReplyAttempt
    from aemr_bot.services import operator_drafts as drafts
    from aemr_bot.services import operator_reply_attempts
    appeal, operator = await seed(session)
    draft = await drafts.start(session, operator.id, appeal.id, is_final=True)
    await drafts.append(session, operator.id, draft.token, source="1", text="Ответ", attachments=[])
    await drafts.claim(session, operator.id, draft.token)
    attempt = OperatorReplyAttempt(appeal_id=appeal.id, operator_id=operator.id,
        source_key="reply_draft:" + draft.token, text="Ответ", attachments=[],
        is_final=True, state=state, max_message_id="confirmed-mid")
    session.add(attempt)
    await session.flush()
    assert await operator_reply_attempts.record_confirmed(session, attempt.id)
    assert await drafts.get(session, operator.id) is None
