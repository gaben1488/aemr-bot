"""Composition rules shared by resident appeal drafts."""
from __future__ import annotations


class DraftLimitError(ValueError):
    """The new part does not fit; previously stored parts remain intact."""


def append_part(data: dict, text: str, attachments: list,
                *, max_chars: int, max_files: int) -> dict:
    chunks = list(data.get("summary_chunks") or [])
    files = list(data.get("attachments") or [])
    if text.strip():
        chunks.append(text)
    files.extend(attachments)
    if len("\n".join(chunks)) > max_chars:
        raise DraftLimitError("text")
    if len(files) > max_files:
        raise DraftLimitError("attachments")
    return {"summary_chunks": chunks, "attachments": files}


async def pause(session, user) -> bool:
    """Save the active workflow before entering the menu, in the same transaction."""
    from aemr_bot.db.models import DialogState

    states = {
        DialogState.AWAITING_LOCALITY.value, DialogState.AWAITING_ADDRESS.value,
        DialogState.AWAITING_TOPIC.value, DialogState.AWAITING_SUMMARY.value,
        DialogState.AWAITING_GEO_CONFIRM.value, DialogState.AWAITING_FOLLOWUP_TEXT.value,
    }
    if user.dialog_state not in states:
        return False
    if user.appeal_draft:
        # A second workflow must not overwrite a previously paused draft.
        return True
    user.appeal_draft = {"state": user.dialog_state, "data": dict(user.dialog_data or {})}
    user.dialog_state = DialogState.IDLE.value
    user.dialog_data = {}
    await session.flush()
    return True


async def offer_saved(event, max_user_id: int) -> bool:
    from aemr_bot import keyboards
    from aemr_bot.db.session import session_scope
    from aemr_bot.services import users
    from aemr_bot.utils.event import send_or_edit_screen

    async with session_scope() as session:
        user = await users.find_by_max_id(session, max_user_id)
        from aemr_bot.db.models import DialogState
        active_states = {
            DialogState.AWAITING_LOCALITY.value, DialogState.AWAITING_ADDRESS.value,
            DialogState.AWAITING_TOPIC.value, DialogState.AWAITING_SUMMARY.value,
            DialogState.AWAITING_GEO_CONFIRM.value, DialogState.AWAITING_FOLLOWUP_TEXT.value,
        }
        exists = bool(user and (user.appeal_draft or user.dialog_state in active_states))
    if exists:
        await send_or_edit_screen(
            event, text="У вас уже есть черновик. Продолжите или отмените его, прежде чем начинать новый.",
            attachments=[keyboards.draft_resume_keyboard()],
        )
    return exists
