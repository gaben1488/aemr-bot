from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from maxapi import Dispatcher
from maxapi.types import MessageCallback, MessageCreated

from aemr_bot import keyboards, texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import DialogState
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import card_format
from aemr_bot.services import settings_store
from aemr_bot.services import users as users_service
from aemr_bot.utils.attachments import (
    collect_attachments,
    deserialize_for_relay,
    extract_phone,
)
from aemr_bot.utils.event import (
    ack_callback,
    extract_message_id,
    get_chat_id,
    get_first_name,
    get_message_body,
    get_message_text,
    get_payload,
    get_user_id,
)

log = logging.getLogger(__name__)

# Citizen-name / address must contain at least one alphanumeric — guards
# against "👍", "...", "`````" and similar one-glyph submissions.
_HAS_ALNUM = re.compile(r"[A-Za-zА-Яа-яЁё0-9]")

_collect_timers: dict[int, asyncio.Task] = {}
_user_locks: dict[int, asyncio.Lock] = {}


def _get_user_lock(max_user_id: int) -> asyncio.Lock:
    """Per-user lock so concurrent submit/cancel/timer paths don't double-dispatch.

    Single-instance only — at horizontal scale this would need
    pg_advisory_xact_lock or a Redis lock.
    """
    lock = _user_locks.get(max_user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[max_user_id] = lock
    return lock


def _drop_user_lock(max_user_id: int) -> None:
    """Release the lock object after a funnel has fully terminated. Keeps
    `_user_locks` from growing unbounded as more citizens cycle through
    the bot. Safe to call when no one holds the lock — the dict-pop is
    idempotent."""
    lock = _user_locks.get(max_user_id)
    if lock is not None and not lock.locked():
        _user_locks.pop(max_user_id, None)


async def recover_stuck_funnels(bot) -> int:
    """Finalize funnels left in AWAITING_SUMMARY after a restart. Run once at startup."""
    async with session_scope() as session:
        ids = await users_service.find_stuck_in_summary(
            session, idle_seconds=cfg.appeal_collect_timeout_seconds
        )
    if not ids:
        return 0

    results = await asyncio.gather(
        *(_persist_and_dispatch_appeal(bot, uid) for uid in ids),
        return_exceptions=True,
    )

    # Empty submissions never get a re-prompt at recovery time — drop them to
    # IDLE so they don't reappear in every subsequent recover() pass.
    empty_ids = [uid for uid, r in zip(ids, results) if r is False]
    if empty_ids:
        async with session_scope() as session:
            for uid in empty_ids:
                await users_service.reset_state(session, uid)

    finalized = sum(1 for r in results if r is True)
    failed = sum(1 for r in results if isinstance(r, BaseException))
    if failed:
        log.warning("recover: %d/%d funnels failed", failed, len(ids))
    if finalized:
        log.info("recovered %d stuck funnels", finalized)
    return finalized


async def _send_to_admin_card(bot, text: str) -> str | None:
    """Send formatted card into admin group. Returns admin message_id or None on failure."""
    if not cfg.admin_group_id:
        log.warning("ADMIN_GROUP_ID is not set — admin card not delivered")
        return None
    try:
        sent = await bot.send_message(chat_id=cfg.admin_group_id, text=text)
    except Exception:
        log.exception("failed to deliver admin card to chat_id=%s", cfg.admin_group_id)
        return None
    return extract_message_id(sent)


async def _relay_attachments_to_admin(
    bot,
    *,
    appeal_id: int,
    admin_mid: str | None,
    stored_attachments: list[dict],
) -> None:
    """Forward citizen-supplied photos/geo/files into the admin group as a reply
    to the card. Best-effort: failures are logged and don't break the appeal flow."""
    if not cfg.admin_group_id or not stored_attachments:
        return
    relayable = deserialize_for_relay(stored_attachments)
    if not relayable:
        return
    try:
        from maxapi.enums.message_link_type import MessageLinkType
        from maxapi.types.message import NewMessageLink
    except Exception:
        log.exception("maxapi link types unavailable; relaying without reply link")
        MessageLinkType = None  # type: ignore[assignment]
        NewMessageLink = None  # type: ignore[assignment]

    link = None
    if admin_mid and MessageLinkType is not None and NewMessageLink is not None:
        try:
            link = NewMessageLink(type=MessageLinkType.REPLY, mid=admin_mid)
        except Exception:
            log.exception("failed to build NewMessageLink for admin_mid=%s", admin_mid)
            link = None

    # Chunk attachments per message — MAX server attachment limit isn't
    # documented; staying under attachments_per_relay_message keeps each
    # send_message comfortably within whatever server-side cap exists.
    chunk_size = max(1, cfg.attachments_per_relay_message)
    batches = [relayable[i:i + chunk_size] for i in range(0, len(relayable), chunk_size)]
    total_batches = len(batches)
    for idx, batch in enumerate(batches, start=1):
        header = (
            f"📎 Вложения к обращению #{appeal_id}"
            if total_batches == 1
            else f"📎 Вложения к обращению #{appeal_id} ({idx}/{total_batches})"
        )
        try:
            await bot.send_message(
                chat_id=cfg.admin_group_id,
                text=header,
                attachments=batch,
                link=link,
            )
        except Exception:
            log.exception(
                "failed to relay attachment batch %d/%d for appeal #%s",
                idx, total_batches, appeal_id,
            )


async def _persist_and_dispatch_appeal(bot, max_user_id: int) -> bool:
    """Create an Appeal from accumulated dialog_data, post the admin card,
    confirm to citizen by user_id. Returns True on persist+dispatch, False on
    empty submission. Raises only on storage failure.

    Guarded by per-user asyncio.Lock so a double-click on «Отправить» (or a
    timer firing while the user is also clicking submit) cannot create two
    appeals — the second call sees IDLE state and bails.
    """
    async with _get_user_lock(max_user_id):
        async with session_scope() as session:
            user = await users_service.get_or_create(session, max_user_id=max_user_id)
            # Idempotency: if state is already IDLE, the previous concurrent
            # call already finalized this funnel — don't double-dispatch.
            if user.dialog_state == DialogState.IDLE.value:
                log.info("dispatch skipped for user %s — state already IDLE", max_user_id)
                return False
            data: dict[str, Any] = dict(user.dialog_data or {})
            summary = "\n".join(data.get("summary_chunks") or []).strip()
            attachments = data.get("attachments") or []
            if not summary and not attachments:
                return False
            appeal = await appeals_service.create_appeal(
                session,
                user=user,
                address=data.get("address", ""),
                topic=data.get("topic", ""),
                summary=summary,
                attachments=attachments,
            )
            await users_service.reset_state(session, max_user_id)

    admin_mid = await _send_to_admin_card(bot, card_format.admin_card(appeal, user))
    if admin_mid:
        async with session_scope() as session:
            await appeals_service.set_admin_message_id(session, appeal.id, admin_mid)

    await _relay_attachments_to_admin(
        bot,
        appeal_id=appeal.id,
        admin_mid=admin_mid,
        stored_attachments=attachments,
    )

    try:
        await bot.send_message(
            user_id=max_user_id,
            text=texts.APPEAL_ACCEPTED.format(number=appeal.id, sla_hours=cfg.sla_response_hours),
            attachments=[keyboards.main_menu()],
        )
    except Exception:
        log.exception("ack to user %s failed for appeal #%s", max_user_id, appeal.id)

    # Освобождаем lock-объект — funnel завершён, новая воронка получит свой.
    # Без этого `_user_locks` копит запись на каждого жителя навечно.
    _drop_user_lock(max_user_id)
    return True


async def _start_appeal_flow(event, max_user_id: int):
    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        if not user.consent_pdn_at:
            await users_service.set_state(session, max_user_id, DialogState.AWAITING_CONSENT, data={})
            policy_url = await settings_store.get(session, "policy_url")
            policy_token = await settings_store.get(session, "policy_pdf_token")
        else:
            policy_url = None
            policy_token = None

    if policy_url is not None or policy_token is not None:
        attachments: list = [keyboards.consent_keyboard()]
        if policy_token:
            from aemr_bot.services.policy import build_file_attachment
            attachments.insert(0, build_file_attachment(policy_token))
            text = (
                "Перед оформлением обращения нужно ваше согласие на обработку "
                "персональных данных в соответствии с 152-ФЗ. Полный текст политики — "
                "в прикреплённом PDF.\n\nНажмите «Согласен», чтобы продолжить."
            )
        else:
            text = texts.CONSENT_REQUEST.format(policy_url=policy_url)
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=text,
            attachments=attachments,
        )
        return

    await _ask_contact_or_skip(event, max_user_id)


async def _ask_contact_or_skip(event, max_user_id: int):
    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        if not user.phone:
            target_state = DialogState.AWAITING_CONTACT
        elif not user.first_name or user.first_name == "Удалено":
            target_state = DialogState.AWAITING_NAME
        else:
            target_state = DialogState.AWAITING_ADDRESS
        await users_service.set_state(session, max_user_id, target_state, data={})

    prompt_for = {
        DialogState.AWAITING_CONTACT: (texts.CONTACT_REQUEST, keyboards.contact_request_keyboard()),
        DialogState.AWAITING_NAME: (texts.CONTACT_RECEIVED, keyboards.cancel_keyboard()),
        DialogState.AWAITING_ADDRESS: (texts.NAME_RECEIVED, keyboards.cancel_keyboard()),
    }
    text, keyboard = prompt_for[target_state]
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=text,
        attachments=[keyboard],
    )


async def _ask_topic(event, max_user_id: int):
    async with session_scope() as session:
        topics = await settings_store.get(session, "topics") or ["Другое"]
        await users_service.set_state(session, max_user_id, DialogState.AWAITING_TOPIC)
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.ADDRESS_RECEIVED,
        attachments=[keyboards.topics_keyboard(topics)],
    )


async def _ask_summary(event, max_user_id: int):
    async with session_scope() as session:
        await users_service.set_state(session, max_user_id, DialogState.AWAITING_SUMMARY)
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.TOPIC_RECEIVED,
        attachments=[keyboards.submit_or_cancel_keyboard()],
    )


async def _finalize_appeal(event, max_user_id: int):
    """Submit-button / timeout entry point. Sends a re-prompt on empty input."""
    persisted = await _persist_and_dispatch_appeal(event.bot, max_user_id)
    if not persisted:
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=texts.APPEAL_EMPTY_REJECTED,
            attachments=[keyboards.submit_or_cancel_keyboard()],
        )


def _schedule_collect_timeout(event, max_user_id: int):
    """Cancel any previous timer and start a new one for this user."""
    existing = _collect_timers.get(max_user_id)
    if existing and not existing.done():
        existing.cancel()

    async def _runner():
        try:
            await asyncio.sleep(cfg.appeal_collect_timeout_seconds)
            await _finalize_appeal(event, max_user_id)
        except asyncio.CancelledError:
            return
        finally:
            _collect_timers.pop(max_user_id, None)

    _collect_timers[max_user_id] = asyncio.create_task(_runner())


def register(dp: Dispatcher) -> None:
    @dp.message_callback()
    async def on_callback(event: MessageCallback):
        payload = get_payload(event)
        max_user_id = get_user_id(event)
        if max_user_id is None:
            log.warning("callback without user_id, payload=%r — skipped", payload)
            return

        # Citizen-flow callbacks (menu:*, consent:*, topic:*, appeal:*,
        # info:*, cancel) не должны срабатывать в админ-группе. Иначе
        # любое случайное нажатие на старую цитированную inline-кнопку
        # запустит воронку обращения от имени оператора и засорит таблицу
        # users. В админ-чате пропускаем только admin-flow:
        # broadcast:{confirm,abort,stop:N} и op:*. broadcast:unsubscribe —
        # citizen-side, шлётся из личного broadcast'а, в админ-чате тоже
        # не нужен.
        chat_id = get_chat_id(event)
        if cfg.admin_group_id and chat_id == cfg.admin_group_id:
            is_admin_callback = payload.startswith("op:") or (
                payload.startswith("broadcast:")
                and payload != "broadcast:unsubscribe"
            )
            if not is_admin_callback:
                await ack_callback(event)
                return

        if payload == "menu:new_appeal":
            await ack_callback(event)
            await _start_appeal_flow(event, max_user_id)
            return

        if payload == "consent:yes":
            async with session_scope() as session:
                await users_service.set_consent(session, max_user_id)
            await ack_callback(event, texts.CONSENT_ACCEPTED)
            await _ask_contact_or_skip(event, max_user_id)
            return

        if payload == "consent:no":
            async with session_scope() as session:
                await users_service.reset_state(session, max_user_id)
            await ack_callback(event)
            await event.bot.send_message(
                chat_id=get_chat_id(event),
                text=texts.CONSENT_DECLINED,
                attachments=[keyboards.main_menu()],
            )
            return

        if payload == "cancel":
            async with session_scope() as session:
                await users_service.reset_state(session, max_user_id)
            timer = _collect_timers.pop(max_user_id, None)
            if timer and not timer.done():
                timer.cancel()
            _drop_user_lock(max_user_id)
            await ack_callback(event)
            await event.bot.send_message(
                chat_id=get_chat_id(event),
                text=texts.CANCELLED,
                attachments=[keyboards.main_menu()],
            )
            return

        if payload.startswith("topic:"):
            try:
                idx = int(payload.split(":")[1])
            except (IndexError, ValueError):
                return
            async with session_scope() as session:
                topics = await settings_store.get(session, "topics") or []
                if 0 <= idx < len(topics):
                    chosen = topics[idx]
                    await users_service.update_dialog_data(session, max_user_id, {"topic": chosen})
                else:
                    return
            await ack_callback(event)
            await _ask_summary(event, max_user_id)
            return

        if payload == "appeal:submit":
            timer = _collect_timers.pop(max_user_id, None)
            if timer and not timer.done():
                timer.cancel()
            await ack_callback(event)
            await _finalize_appeal(event, max_user_id)
            return

        # Broadcast wizard callbacks (operator-side) live in their own handler;
        # delegate so we don't register a second @dp.message_callback().
        if payload.startswith("broadcast:") and not payload.startswith(
            "broadcast:unsubscribe"
        ):
            from aemr_bot.handlers import broadcast as broadcast_handler
            if payload == "broadcast:confirm":
                await broadcast_handler._handle_confirm(event)
                return
            if payload == "broadcast:abort":
                await broadcast_handler._handle_abort(event)
                return
            if payload.startswith("broadcast:stop:"):
                try:
                    bid = int(payload.split(":", 2)[2])
                except (IndexError, ValueError):
                    return
                await broadcast_handler._handle_stop(event, bid)
                return

        # /op_help quick-action buttons.
        if payload.startswith("op:"):
            from aemr_bot.handlers import admin_commands, broadcast as broadcast_handler
            if payload == "op:stats_today":
                await ack_callback(event)
                await admin_commands.run_stats_today(event)
                return
            if payload == "op:broadcast":
                await ack_callback(event)
                await broadcast_handler._start_wizard(event)
                return
            if payload == "op:help_full":
                await ack_callback(event)
                await admin_commands.show_full_help(event)
                return

        # Fall through to menu/contacts/appeal-show handlers
        from aemr_bot.handlers import menu as menu_handlers
        await menu_handlers.handle_callback(event, payload, max_user_id)

    @dp.message_created()
    async def on_message(event: MessageCreated):
        from aemr_bot.handlers import operator_reply as op_reply

        chat_id = get_chat_id(event)
        if chat_id is None:
            log.warning("message_created without chat_id — event.get_ids() returned None")
            return

        text_body = get_message_text(event)
        body = get_message_body(event)

        if cfg.admin_group_id and chat_id == cfg.admin_group_id:
            # Broadcast wizard takes priority over slash-filtering so that
            # /cancel works mid-wizard. _handle_wizard_text returns False
            # when no wizard is active for this operator, so it's safe to
            # call on every admin-group message.
            from aemr_bot.handlers import broadcast as broadcast_handler
            consumed = await broadcast_handler._handle_wizard_text(event, text_body)
            if consumed:
                return
            if text_body.startswith("/"):
                # Slash command without an active wizard. Admin-side command
                # handlers (admin_commands.py, broadcast.py) registered before
                # this catch-all already had their chance — drop silently.
                return
            await op_reply.handle_operator_reply(event, body, text_body)
            return

        # Citizen DM: slash-prefixed text has command handlers registered
        # earlier; if we got here, none matched — drop silently.
        if text_body.startswith("/"):
            return

        max_user_id = get_user_id(event)
        if max_user_id is None:
            return

        async with session_scope() as session:
            user = await users_service.get_or_create(
                session,
                max_user_id=max_user_id,
                first_name=get_first_name(event),
            )
            state = DialogState(user.dialog_state)

        handler = _STATE_HANDLERS.get(state)
        if handler is not None:
            await handler(event, body, text_body, max_user_id, op_reply)


async def _on_awaiting_contact(event, body, text_body, max_user_id, op_reply):
    phone = extract_phone(body)
    if phone is None:
        await event.message.answer(
            texts.CONTACT_RETRY,
            attachments=[keyboards.contact_request_keyboard()],
        )
        return

    async with session_scope() as session:
        await users_service.set_phone(session, max_user_id, phone)
        user = await users_service.get_or_create(session, max_user_id=max_user_id)

    if not user.first_name or user.first_name == "Удалено":
        async with session_scope() as session:
            await users_service.set_state(session, max_user_id, DialogState.AWAITING_NAME)
        await event.message.answer(texts.CONTACT_RECEIVED, attachments=[keyboards.cancel_keyboard()])
    else:
        await _ask_contact_or_skip(event, max_user_id)


async def _on_awaiting_name(event, body, text_body, max_user_id, op_reply):
    name = text_body.strip()[: cfg.name_max_chars]
    if not name or not _HAS_ALNUM.search(name):
        # Empty / whitespace-only / emoji-only / punctuation-only — reject.
        await event.message.answer(texts.NAME_EMPTY)
        return
    async with session_scope() as session:
        await users_service.set_first_name(session, max_user_id, name)
        await users_service.set_state(session, max_user_id, DialogState.AWAITING_ADDRESS)
    await event.message.answer(texts.NAME_RECEIVED, attachments=[keyboards.cancel_keyboard()])


async def _on_awaiting_address(event, body, text_body, max_user_id, op_reply):
    address = text_body.strip()[: cfg.address_max_chars]
    if not address or not _HAS_ALNUM.search(address):
        await event.message.answer(texts.ADDRESS_EMPTY)
        return
    async with session_scope() as session:
        await users_service.update_dialog_data(session, max_user_id, {"address": address})
    await _ask_topic(event, max_user_id)


async def _on_awaiting_summary(event, body, text_body, max_user_id, op_reply):
    chunk = text_body.strip()
    atts = collect_attachments(body)
    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        data = dict(user.dialog_data or {})

        if chunk:
            existing_chunks: list[str] = data.setdefault("summary_chunks", [])
            current_total = sum(len(c) for c in existing_chunks)
            remaining = cfg.summary_max_chars - current_total
            if remaining <= 0:
                # Cap reached — drop silently. Logged for ops, not surfaced
                # to citizen (would be noise; bot already has enough text).
                log.info(
                    "summary cap %d reached for user %s, dropping chunk of %d chars",
                    cfg.summary_max_chars,
                    max_user_id,
                    len(chunk),
                )
            else:
                existing_chunks.append(chunk[:remaining])

        if atts:
            existing_atts: list = data.setdefault("attachments", [])
            cap = cfg.attachments_max_per_appeal - len(existing_atts)
            if cap <= 0:
                log.info(
                    "attachment cap %d reached for user %s, dropping %d attachments",
                    cfg.attachments_max_per_appeal,
                    max_user_id,
                    len(atts),
                )
            else:
                existing_atts.extend(atts[:cap])

        user.dialog_data = data
        await session.flush()
    _schedule_collect_timeout(event, max_user_id)


async def _on_idle(event, body, text_body, max_user_id, op_reply):
    handled = await op_reply.handle_user_followup(event, text_body)
    if not handled:
        await event.message.answer(texts.UNKNOWN_INPUT, attachments=[keyboards.main_menu()])


_STATE_HANDLERS = {
    DialogState.AWAITING_CONTACT: _on_awaiting_contact,
    DialogState.AWAITING_NAME: _on_awaiting_name,
    DialogState.AWAITING_ADDRESS: _on_awaiting_address,
    DialogState.AWAITING_SUMMARY: _on_awaiting_summary,
    DialogState.IDLE: _on_idle,
}
