"""One durable, editable operator queue; never retry an uncertain creation.

The initial claim commits before MAX. A crash leaves `attempting`, which is
uncertain and requires reconciliation. Edits target the confirmed message
under a database row lock and may safely converge again after a timeout.
Only IDs, counts and internal status labels appear in the shared panel.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from aemr_bot.config import settings
from aemr_bot.db.models import Setting
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import sla, work_schedule
from aemr_bot.ui.operator_keyboards import queue_panel_keyboard
from aemr_bot.utils.event import check_edit_result, extract_message_id

log = logging.getLogger(__name__)



async def _snapshot() -> tuple[str, bool]:
    async with session_scope() as session:
        appeals = await appeals_service.list_unanswered(session)
        total = await appeals_service.count_open(session)
        ids = [a.id for a in appeals]
        first = await appeals_service.first_operator_reply_times(session, ids)
        intermediate = await appeals_service.latest_intermediate_reply_times(session, ids)
        followup = await appeals_service.current_followup_hours(session)
    now = datetime.now(ZoneInfo(settings.timezone))
    groups: dict[str, list[int]] = {
        "⚠️ Просрочен первый ответ": [], "🆕 Ожидают первого ответа": [],
        "⏱ Требуют дальнейшей работы": [], "💬 В работе после первого ответа": [],
    }
    for appeal in appeals:
        if appeal.id not in first:
            overdue = sla.is_overdue(
                getattr(appeal, "sla_anchor_at", None) or appeal.created_at, now,
                appeals_service.response_hours_for(appeal, settings.sla_response_hours),
                schedule=work_schedule.for_appeal(appeal),
            )
            label = "⚠️ Просрочен первый ответ" if overdue else "🆕 Ожидают первого ответа"
        elif (followup > 0 and appeal.id in intermediate
              and sla.is_overdue(intermediate[appeal.id], now, followup,
                                 schedule=work_schedule.for_appeal(appeal))):
            label = "⏱ Требуют дальнейшей работы"
        else:
            label = "💬 В работе после первого ответа"
        groups[label].append(appeal.id)
    if not total:
        return "🎉 Сейчас нет открытых обращений.", False
    lines = [f"📋 Открытые обращения: {total}"]
    for label, group in groups.items():
        if group:
            preview = ", ".join(f"#{i}" for i in sorted(group)[:12])
            if len(group) > 12:
                preview += f" · ещё {len(group) - 12}"
            lines.append(f"{label}: {len(group)}\n{preview}")
    if total > len(appeals):
        lines.append(f"Сводка по первым {len(appeals)}; полный список — кнопкой ниже.")
    lines.append("Сроки — внутренние рабочие сроки. Подробности доступны операторам по кнопке.")
    return "\n\n".join(lines), True


async def refresh(bot: Any) -> None:
    """Refresh the canonical message, without creating routine notifications.

    No new message is sent for an empty queue, an uncertain initial send,
    or an edit failure (including a deleted/uneditable MAX message).
    """
    if not settings.admin_group_id:
        return
    key = f"__queue_panel:{settings.admin_group_id}"
    async with session_scope() as session:
        await session.execute(insert(Setting).values(key=key, value={"state": "idle"})
                              .on_conflict_do_nothing(index_elements=[Setting.key]))
    async with session_scope() as session:
        row = await session.scalar(select(Setting).where(Setting.key == key).with_for_update())
        assert row is not None
        state = row.value if isinstance(row.value, dict) else {}
        if state.get("state") in {"attempting", "unknown"}:
            return
        # Compute after locking: simultaneous ticks cannot overwrite a newer
        # snapshot with one computed before they waited for the lock.
        text, nonempty = await _snapshot()
        signature = hashlib.sha256(text.encode()).hexdigest()
        mid = state.get("message_id")
        if mid:
            if state.get("signature") == signature:
                return
            try:
                result = await bot.edit_message(message_id=mid, text=text,
                                                attachments=[queue_panel_keyboard()], notify=False)
                check_edit_result(result)
            except Exception:
                log.exception("queue panel edit failed for %s; preserving identity", mid)
                return
            row.value = {"state": "confirmed", "message_id": mid, "signature": signature}
            return
        if not nonempty:
            return
        token = uuid4().hex
        row.value = {"state": "attempting", "token": token}
    # Initial claim is durable before the external mutation. No other worker
    # may create another message while this request is in flight.
    mid = None
    try:
        sent = await bot.send_message(chat_id=settings.admin_group_id, text=text,
                                      attachments=[queue_panel_keyboard()], notify=False)
        mid = extract_message_id(sent)
    except Exception:
        log.exception("queue panel initial send has unknown outcome; not retrying")
    async with session_scope() as session:
        row = await session.scalar(select(Setting).where(Setting.key == key).with_for_update())
        assert row is not None
        if not isinstance(row.value, dict) or row.value.get("token") != token:
            log.error("queue panel send completed after recovery; reconcile old attempt %s", token)
            return
        row.value = ({"state": "confirmed", "message_id": mid, "signature": signature}
                     if mid else {"state": "unknown", "token": token})
    if not mid:
        log.error("queue panel creation unknown; reconcile %s before any replacement", key)


async def recovery_status() -> dict:
    """Return operator-visible uncertainty and a versioned manual recovery token."""
    async with session_scope() as session:
        row = await session.scalar(select(Setting).where(
            Setting.key == f"__queue_panel:{settings.admin_group_id}"
        ).with_for_update())
        if row is None or not isinstance(row.value, dict):
            return {}
        state = dict(row.value)
        if state.get("state") not in {"attempting", "unknown"}:
            return state
        # A live request must not be reset. After a crash the committed claim
        # becomes recoverable, but only following an explicit MAX check.
        recoverable = (state.get("state") == "unknown"
                       or row.updated_at < datetime.now(UTC) - timedelta(minutes=5))
        if recoverable and not state.get("token"):
            state["token"] = uuid4().hex
            state["state"] = "unknown"
            row.value = state
        return {**state, "recoverable": recoverable}


async def allow_recreate(token: str) -> bool:
    """Consume the IT operator's confirmation once, under the row lock."""
    async with session_scope() as session:
        row = await session.scalar(select(Setting).where(
            Setting.key == f"__queue_panel:{settings.admin_group_id}"
        ).with_for_update())
        if row is None or not isinstance(row.value, dict):
            return False
        state = row.value
        if not token or state.get("token") != token:
            return False
        if state.get("state") != "unknown" and not (
            state.get("state") == "attempting"
            and row.updated_at < datetime.now(UTC) - timedelta(minutes=5)
        ):
            return False
        row.value = {"state": "idle"}
        return True
