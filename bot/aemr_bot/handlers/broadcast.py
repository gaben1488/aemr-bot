"""Broadcast wizard and dispatch loop.

Operator workflow in the admin chat:

  1. /broadcast               → bot prompts for the text
  2. operator types text      → bot shows preview with subscriber count
  3. operator clicks ✅       → bot starts a background send task
  4. background task          → ships the broadcast at 1 msg/sec, edits a
                                 progress message in the admin group every
                                 BROADCAST_PROGRESS_UPDATE_SEC seconds
  5. anyone clicks ⛔ stop    → status flips to cancelled, loop exits

Wizard state (steps 1-3) lives in process memory only — operators are not
in the `users` table, and a half-finished wizard is cheap to redo. State is
auto-evicted after BROADCAST_WIZARD_TTL_SEC.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from maxapi import Dispatcher
from maxapi.types import Command, MessageCreated
from zoneinfo import ZoneInfo

from aemr_bot import keyboards, texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import BroadcastStatus, OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.services import broadcasts as broadcasts_service
from aemr_bot.services import operators as operators_service
from aemr_bot.utils.event import (
    ack_callback,
    extract_message_id,
    get_chat_id,
    get_message_text,
    get_user_id,
)

log = logging.getLogger(__name__)

TZ = ZoneInfo(cfg.timezone)


WizardStep = Literal["awaiting_text", "awaiting_confirm"]


@dataclass
class _WizardState:
    step: WizardStep
    text: str = ""
    expires_at: float = field(
        default_factory=lambda: time.monotonic() + cfg.broadcast_wizard_ttl_sec
    )

    def expired(self) -> bool:
        return time.monotonic() > self.expires_at

    def renew(self) -> None:
        self.expires_at = time.monotonic() + cfg.broadcast_wizard_ttl_sec


# Per-operator wizard state. Single-instance only — multi-replica deployment
# would need Redis or pg_advisory_lock-backed state.
_wizards: dict[int, _WizardState] = {}


def _is_admin_chat(event) -> bool:
    chat_id = get_chat_id(event)
    return cfg.admin_group_id is not None and chat_id == cfg.admin_group_id


async def _get_operator(event):
    if not _is_admin_chat(event):
        return None
    author_id = get_user_id(event)
    if author_id is None:
        return None
    async with session_scope() as session:
        return await operators_service.get(session, author_id)


async def _ensure_role(event, *allowed: OperatorRole) -> bool:
    """Same shape as admin_commands._ensure_role but local — operator returned
    is needed to record audit entries with the actor's row id."""
    op = await _get_operator(event)
    if op is None:
        return False
    if op.role not in {r.value for r in allowed}:
        await event.message.answer(
            f"Команда доступна только ролям: {', '.join(r.value for r in allowed)}"
        )
        return False
    return True


def _drop_expired_wizards() -> None:
    """Sweep stale wizards. Called opportunistically on each new wizard event."""
    stale = [uid for uid, st in _wizards.items() if st.expired()]
    for uid in stale:
        _wizards.pop(uid, None)


async def _start_wizard(event) -> None:
    if not await _ensure_role(event, OperatorRole.IT, OperatorRole.COORDINATOR):
        return
    _drop_expired_wizards()
    actor_id = get_user_id(event)
    if actor_id is None:
        return
    _wizards[actor_id] = _WizardState(step="awaiting_text")
    await event.message.answer(
        texts.OP_BROADCAST_PROMPT.format(limit=cfg.broadcast_max_chars)
    )


async def _handle_wizard_text(event, text_body: str) -> bool:
    """Called from the global on_message router when the author has an active
    awaiting_text wizard. Returns True if the message was consumed."""
    actor_id = get_user_id(event)
    if actor_id is None:
        return False
    state = _wizards.get(actor_id)
    if state is None or state.step != "awaiting_text":
        return False

    if state.expired():
        _wizards.pop(actor_id, None)
        await event.message.answer(texts.OP_BROADCAST_WIZARD_EXPIRED)
        return True

    if text_body.strip() == "/cancel":
        _wizards.pop(actor_id, None)
        await event.message.answer(texts.OP_BROADCAST_CANCELLED_BY_USER)
        return True

    text = text_body.strip()
    if len(text) > cfg.broadcast_max_chars:
        await event.message.answer(
            texts.OP_BROADCAST_TOO_LONG.format(
                limit=cfg.broadcast_max_chars, actual=len(text)
            )
        )
        return True
    if not text:
        # Empty — re-prompt without changing state.
        await event.message.answer(
            texts.OP_BROADCAST_PROMPT.format(limit=cfg.broadcast_max_chars)
        )
        return True

    async with session_scope() as session:
        count = await broadcasts_service.count_subscribers(session)
    if count == 0:
        _wizards.pop(actor_id, None)
        await event.message.answer(texts.OP_BROADCAST_NO_SUBSCRIBERS)
        return True

    state.text = text
    state.step = "awaiting_confirm"
    state.renew()
    await event.message.answer(
        texts.OP_BROADCAST_PREVIEW.format(text=text, count=count),
        attachments=[keyboards.broadcast_confirm_keyboard()],
    )
    return True


async def _handle_confirm(event) -> None:
    actor_id = get_user_id(event)
    if actor_id is None:
        return
    state = _wizards.pop(actor_id, None)
    if state is None or state.step != "awaiting_confirm" or state.expired():
        await ack_callback(event, "Мастер закрыт.")
        return
    await ack_callback(event)
    op = await _get_operator(event)
    if op is None:
        return

    async with session_scope() as session:
        count = await broadcasts_service.count_subscribers(session)
        if count == 0:
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text=texts.OP_BROADCAST_NO_SUBSCRIBERS,
            )
            return
        broadcast = await broadcasts_service.create_broadcast(
            session,
            text=state.text,
            operator_id=op.id,
            subscriber_count=count,
        )
        await operators_service.write_audit(
            session,
            operator_max_user_id=actor_id,
            action="broadcast_send",
            target=f"broadcast #{broadcast.id}",
            details={"text": state.text, "subscriber_count": count},
        )
        broadcast_id = broadcast.id

    asyncio.create_task(_run_broadcast(event.bot, broadcast_id, state.text, count))


async def _handle_abort(event) -> None:
    actor_id = get_user_id(event)
    if actor_id is not None:
        _wizards.pop(actor_id, None)
    await ack_callback(event, "Отменено.")
    await event.bot.send_message(
        chat_id=cfg.admin_group_id,
        text=texts.OP_BROADCAST_CANCELLED_BY_USER,
    )


async def _handle_stop(event, broadcast_id: int) -> None:
    """Anyone in the admin group can stop a running broadcast."""
    if not _is_admin_chat(event):
        await ack_callback(event)
        return
    async with session_scope() as session:
        flipped = await broadcasts_service.request_cancel(session, broadcast_id)
    await ack_callback(
        event, "Остановлено." if flipped else "Уже завершено."
    )


def _format_progress(
    *, broadcast_id: int, total: int, delivered: int, failed: int
) -> str:
    failed_suffix = (
        texts.OP_BROADCAST_FAILED_SUFFIX.format(failed=failed) if failed else ""
    )
    return texts.OP_BROADCAST_PROGRESS.format(
        number=broadcast_id,
        total=total,
        delivered=delivered,
        failed_suffix=failed_suffix,
    )


async def _send_one(bot, user, broadcast_id: int, body_text: str) -> str | None:
    """Returns None on success, error string on failure."""
    try:
        await bot.send_message(
            user_id=user.max_user_id,
            text=body_text,
            attachments=[keyboards.broadcast_unsubscribe_keyboard()],
        )
    except Exception as e:
        return repr(e)[:500]
    return None


async def _run_broadcast(bot, broadcast_id: int, text: str, total: int) -> None:
    """Background task: ship a prepared broadcast to all eligible subscribers,
    edit a progress message in the admin group, honor the cancel flag."""

    body = f"{texts.BROADCAST_HEADER}\n\n{text}"
    delivered = 0
    failed = 0

    # Start: post header in admin group, capture admin_message_id for edits.
    sent = None
    try:
        sent = await bot.send_message(
            chat_id=cfg.admin_group_id,
            text=texts.OP_BROADCAST_STARTED.format(number=broadcast_id, total=total),
            attachments=[keyboards.broadcast_stop_keyboard(broadcast_id)],
        )
    except Exception:
        log.exception("failed to post broadcast start in admin group")
    admin_mid = extract_message_id(sent) if sent is not None else None

    async with session_scope() as session:
        await broadcasts_service.mark_started(session, broadcast_id, admin_mid)

    rate_delay = (
        1.0 / cfg.broadcast_rate_limit_per_sec
        if cfg.broadcast_rate_limit_per_sec > 0
        else 1.0
    )
    last_progress_at = time.monotonic()
    cancelled = False

    async with session_scope() as session:
        async for user in broadcasts_service.iter_subscribers(session):
            # Re-check cancel flag in a fresh session — admin click flips it.
            async with session_scope() as flag_session:
                status = await broadcasts_service.get_status(
                    flag_session, broadcast_id
                )
            if status == BroadcastStatus.CANCELLED.value:
                cancelled = True
                break

            error = await _send_one(bot, user, broadcast_id, body)
            async with session_scope() as delivery_session:
                await broadcasts_service.record_delivery(
                    delivery_session,
                    broadcast_id=broadcast_id,
                    user_id=user.id,
                    error=error,
                )
            if error is None:
                delivered += 1
            else:
                failed += 1

            now = time.monotonic()
            if (
                admin_mid is not None
                and now - last_progress_at >= cfg.broadcast_progress_update_sec
            ):
                last_progress_at = now
                async with session_scope() as upd_session:
                    await broadcasts_service.update_progress(
                        upd_session,
                        broadcast_id,
                        delivered=delivered,
                        failed=failed,
                    )
                try:
                    await bot.edit_message(
                        message_id=admin_mid,
                        text=_format_progress(
                            broadcast_id=broadcast_id,
                            total=total,
                            delivered=delivered,
                            failed=failed,
                        ),
                        attachments=[keyboards.broadcast_stop_keyboard(broadcast_id)],
                    )
                except Exception:
                    log.exception(
                        "failed to edit progress message for broadcast #%s",
                        broadcast_id,
                    )

            await asyncio.sleep(rate_delay)

    final_status = (
        BroadcastStatus.CANCELLED if cancelled else BroadcastStatus.DONE
    )
    async with session_scope() as session:
        await broadcasts_service.mark_finished(
            session,
            broadcast_id,
            status=final_status,
            delivered=delivered,
            failed=failed,
        )

    if admin_mid is not None:
        if cancelled:
            final_text = texts.OP_BROADCAST_CANCELLED.format(
                number=broadcast_id, delivered=delivered, total=total
            )
        else:
            failed_line = (
                texts.OP_BROADCAST_FAILED_LINE.format(failed=failed) if failed else ""
            )
            final_text = texts.OP_BROADCAST_DONE.format(
                number=broadcast_id,
                delivered=delivered,
                total=total,
                failed_line=failed_line,
            )
        try:
            await bot.edit_message(message_id=admin_mid, text=final_text)
        except Exception:
            log.exception(
                "failed to edit final progress message for broadcast #%s",
                broadcast_id,
            )


def _format_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


async def _list_broadcasts(event) -> None:
    if not await _ensure_role(event, OperatorRole.IT, OperatorRole.COORDINATOR):
        return
    async with session_scope() as session:
        items = await broadcasts_service.list_recent(session, limit=10)
    if not items:
        await event.message.answer(texts.OP_BROADCAST_LIST_EMPTY)
        return
    lines = [texts.OP_BROADCAST_LIST_HEADER.rstrip()]
    for bc in items:
        lines.append(
            texts.OP_BROADCAST_LIST_ITEM.format(
                number=bc.id,
                created_at=_format_dt(bc.created_at),
                status=bc.status,
                delivered=bc.delivered_count,
                total=bc.subscriber_count_at_start,
            )
        )
    await event.message.answer("\n".join(lines))


def register(dp: Dispatcher) -> None:
    """Register only `/broadcast` here. Wizard callbacks (confirm/abort/stop)
    are routed from `handlers.appeal.on_callback` via delegation, and the
    citizen-side `broadcast:unsubscribe` is handled by `handlers.menu`. We
    deliberately don't add a second `@dp.message_callback()` to avoid
    double-dispatch: maxapi runs every registered handler for each event,
    and a second one would duplicate every ack."""

    @dp.message_created(Command("broadcast"))
    async def cmd_broadcast(event: MessageCreated):
        if not _is_admin_chat(event):
            return
        text = get_message_text(event)
        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if arg.lower() == "list":
            await _list_broadcasts(event)
            return
        await _start_wizard(event)
