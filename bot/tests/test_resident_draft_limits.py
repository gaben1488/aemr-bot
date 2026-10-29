import pytest

from aemr_bot.services.resident_drafts import DraftLimitError, append_part


def test_draft_accumulates_text_and_files_without_clipping():
    draft = {"summary_chunks": ["а" * 10000], "attachments": [{"type": "image"}]}
    result = append_part(draft, "б" * 9999, [{"type": "file"}], max_chars=20000, max_files=20)
    assert "\n".join(result["summary_chunks"]) == "а" * 10000 + "\n" + "б" * 9999
    assert len(result["attachments"]) == 2
    assert draft["summary_chunks"] == ["а" * 10000]


def test_draft_rejects_excess_without_mutating_previous_parts():
    draft = {"summary_chunks": ["а" * 20000], "attachments": []}
    with pytest.raises(DraftLimitError):
        append_part(draft, "б", [], max_chars=20000, max_files=20)
    assert draft["summary_chunks"] == ["а" * 20000]


def test_draft_rejects_total_file_count():
    draft = {"summary_chunks": [], "attachments": [{"type": "image"}] * 20}
    with pytest.raises(DraftLimitError):
        append_part(draft, "", [{"type": "file"}], max_chars=20000, max_files=20)


@pytest.mark.asyncio
async def test_pause_preserves_completed_parts_and_does_not_replace_a_draft(session):
    from aemr_bot.db.models import DialogState, User
    from aemr_bot.services.resident_drafts import pause

    user = User(max_user_id=15002001, first_name="Иван",
                dialog_state=DialogState.AWAITING_SUMMARY.value,
                dialog_data={"draft_token": "first", "summary_chunks": ["Исходный текст"]})
    session.add(user)
    await session.flush()
    assert await pause(session, user)
    await session.refresh(user)
    assert user.dialog_state == DialogState.IDLE.value
    assert user.appeal_draft["data"]["summary_chunks"] == ["Исходный текст"]
    user.dialog_state = DialogState.AWAITING_SUMMARY.value
    user.dialog_data = {"draft_token": "second", "summary_chunks": ["Второй текст"]}
    await session.flush()
    assert await pause(session, user)
    await session.refresh(user)
    assert user.appeal_draft["data"]["draft_token"] == "first"
    assert user.dialog_data["draft_token"] == "second"
