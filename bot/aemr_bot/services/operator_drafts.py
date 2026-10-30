"""Database-owned operator drafts. Callers commit before acknowledging input.

A claimed draft stays frozen across a restart: only the delivery journal or
explicit reconciliation may decide its outcome. No background sender exists.
"""
from __future__ import annotations

from secrets import token_hex

from sqlalchemy import exists, select, update
from sqlalchemy.dialects.postgresql import insert

from aemr_bot.config import settings
from aemr_bot.db.models import OperatorReplyAttempt, OperatorReplyDraft
from aemr_bot.services.resident_drafts import append_part


class DraftConflict(ValueError):
    """A stale button, another composition, or a send already in progress."""


async def get(session, operator_id: int, *, locked: bool = False):
    statement = select(OperatorReplyDraft).where(OperatorReplyDraft.operator_id == operator_id)
    if locked:
        statement = statement.with_for_update()
    return await session.scalar(statement.execution_options(populate_existing=True))


async def start(session, operator_id: int, appeal_id: int, *, is_final: bool):
    token = token_hex(16)
    statement = insert(OperatorReplyDraft).values(
        operator_id=operator_id, appeal_id=appeal_id, token=token,
        is_final=is_final, state="editing", data={},
    ).on_conflict_do_nothing(index_elements=[OperatorReplyDraft.operator_id])
    result = await session.execute(statement.returning(OperatorReplyDraft.token))
    inserted = result.scalar_one_or_none()
    draft = await get(session, operator_id, locked=True)
    if inserted is None:
        # Starting again must not change the kind or silently retarget content.
        raise DraftConflict("Сначала продолжите или отмените сохранённый ответ.")
    return draft


async def _editable(session, operator_id: int, token: str):
    draft = await get(session, operator_id, locked=True)
    if draft is None or draft.token != token or draft.state != "editing":
        raise DraftConflict("Кнопка устарела или отправка уже началась.")
    return draft


async def append(session, operator_id: int, token: str, *, source: str,
                 text: str, attachments: list):
    draft = await _editable(session, operator_id, token)
    if not source:
        raise ValueError("Не удалось определить событие MAX; часть не сохранена.")
    data = dict(draft.data or {})
    sources = list(data.get("sources") or [])
    if source in sources:
        return draft
    if not text.strip() and not attachments:
        raise ValueError("Добавьте текст или вложение.")
    # Bound both content and event metadata, including attachment-only parts.
    if len(sources) >= 100:
        raise ValueError("В черновике уже 100 частей. Отправьте его или начните заново.")
    if data.get("replace_text") and text.strip():
        data["summary_chunks"] = []
        data["replace_text"] = False
    composed = append_part(data, text, attachments,
                           max_chars=settings.answer_max_chars, max_files=20)
    draft.data = {**data, **composed, "sources": [*sources, source]}
    await session.flush()
    return draft


async def claim(session, operator_id: int, token: str):
    draft = await _editable(session, operator_id, token)
    data = draft.data or {}
    if not data.get("summary_chunks") and not data.get("attachments"):
        raise ValueError("Черновик пуст.")
    draft.state = "sending"
    await session.flush()
    return draft


async def cancel(session, operator_id: int, token: str) -> bool:
    draft = await get(session, operator_id, locked=True)
    if draft is None or draft.token != token or draft.state != "editing":
        return False
    await session.delete(draft)
    await session.flush()
    return True


async def recover_unreserved(session) -> int:
    """Startup only, before handlers: no reservation means no MAX send began.

    The single-instance startup barrier must be held by the caller. Never
    run this concurrently with live callbacks between claim and reserve.
    """
    reserved = exists(select(OperatorReplyAttempt.id).where(
        OperatorReplyAttempt.source_key == "reply_draft:" + OperatorReplyDraft.token))
    rows = await session.execute(update(OperatorReplyDraft).where(
        OperatorReplyDraft.state == "sending", ~reserved,
    ).values(state="editing").returning(OperatorReplyDraft.operator_id))
    return len(rows.all())
