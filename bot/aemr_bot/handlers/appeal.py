from __future__ import annotations

import asyncio
import logging
from typing import Any

from maxapi import Dispatcher
from maxapi.types import MessageCallback, MessageCreated

log = logging.getLogger(__name__)

from aemr_bot import keyboards, texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import DialogState
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import card_format
from aemr_bot.services import settings_store
from aemr_bot.services import users as users_service
from aemr_bot.utils.attachments import collect_attachments, extract_phone
from aemr_bot.utils.event import (
    ack_callback,
    get_chat_id,
    get_first_name,
    get_message_body,
    get_message_text,
    get_payload,
    get_user_id,
)

_collect_timers: dict[int, asyncio.Task] = {}


async def recover_stuck_funnels(bot) -> int:
    """Finalize funnels that were left in AWAITING_SUMMARY after a restart.

    Called once at startup. Returns the count of funnels finalized.
    """
    async with session_scope() as session:
        users = await users_service.find_stuck_in_summary(
            session, idle_seconds=cfg.appeal_collect_timeout_seconds
        )

    finalized = 0
    for user in users:
        try:
            await _finalize_appeal_for(bot, user.max_user_id)
            finalized += 1
        except Exception:
            log.exception("recover: failed to finalize for user %s", user.max_user_id)
    if finalized:
        log.info("recovered %d stuck funnels", finalized)
    return finalized


async def _finalize_appeal_for(bot, max_user_id: int) -> None:
    """Variant of _finalize_appeal that doesn't need an inbound event."""
    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        data: dict[str, Any] = dict(user.dialog_data or {})
        summary_chunks = data.get("summary_chunks", []) or []
        summary = "\n".join(summary_chunks).strip()
        attachments = data.get("attachments", []) or []
        if not summary and not attachments:
            await users_service.reset_state(session, max_user_id)
            return
        appeal = await appeals_service.create_appeal(
            session,
            user=user,
            address=data.get("address", ""),
            topic=data.get("topic", ""),
            summary=summary,
            attachments=attachments,
        )
        await users_service.reset_state(session, max_user_id)

    card_text = card_format.admin_card(appeal, user)
    if cfg.admin_group_id:
        try:
            sent = await bot.send_message(chat_id=cfg.admin_group_id, text=card_text)
            mid = getattr(sent, "message_id", None) or getattr(getattr(sent, "body", None), "mid", None)
            if mid:
                async with session_scope() as session:
                    await appeals_service.set_admin_message_id(session, appeal.id, str(mid))
        except Exception:
            log.exception("recover: admin card delivery failed for appeal #%s", appeal.id)

    try:
        await bot.send_message(
            user_id=max_user_id,
            text=texts.APPEAL_ACCEPTED.format(number=appeal.id, sla_hours=cfg.sla_response_hours),
            attachments=[keyboards.main_menu()],
        )
    except Exception:
        log.exception("recover: ack to user %s failed for appeal #%s", max_user_id, appeal.id)


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
    mid = getattr(sent, "message_id", None) or getattr(getattr(sent, "body", None), "mid", None)
    return str(mid) if mid is not None else None


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

    state = target_state
    if state == DialogState.AWAITING_CONTACT:
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=texts.CONTACT_REQUEST,
            attachments=[keyboards.contact_request_keyboard()],
        )
    elif state == DialogState.AWAITING_NAME:
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=texts.CONTACT_RECEIVED,
            attachments=[keyboards.cancel_keyboard()],
        )
    elif state == DialogState.AWAITING_ADDRESS:
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=texts.NAME_RECEIVED,
            attachments=[keyboards.cancel_keyboard()],
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
    """Persist accumulated data into Appeal, post card to admin group, reset FSM."""
    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        data: dict[str, Any] = dict(user.dialog_data or {})
        address = data.get("address", "")
        topic = data.get("topic", "")
        summary_chunks = data.get("summary_chunks", []) or []
        summary = "\n".join(summary_chunks).strip()
        attachments = data.get("attachments", []) or []

        # Reject empty submissions: at least one of text or attachment is required.
        if not summary and not attachments:
            await event.bot.send_message(
                chat_id=get_chat_id(event),
                text=(
                    "Опишите суть обращения текстом или прикрепите фото. "
                    "Без описания обращение не принимается."
                ),
                attachments=[keyboards.submit_or_cancel_keyboard()],
            )
            return

        appeal = await appeals_service.create_appeal(
            session,
            user=user,
            address=address,
            topic=topic,
            summary=summary,
            attachments=attachments,
        )

        await users_service.reset_state(session, max_user_id)

    card_text = card_format.admin_card(appeal, user)
    admin_mid = await _send_to_admin_card(event.bot, card_text)

    async with session_scope() as session:
        if admin_mid:
            await appeals_service.set_admin_message_id(session, appeal.id, admin_mid)

    try:
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=texts.APPEAL_ACCEPTED.format(number=appeal.id, sla_hours=cfg.sla_response_hours),
            attachments=[keyboards.main_menu()],
        )
    except Exception:
        log.exception("failed to confirm appeal #%s to user", appeal.id)


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
        if text_body.startswith("/"):
            return
        body = get_message_body(event)

        # Branch A: admin group → maybe an operator reply via reply-to
        if cfg.admin_group_id and chat_id == cfg.admin_group_id:
            await op_reply.handle_operator_reply(event, body, text_body)
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

        if state == DialogState.AWAITING_CONTACT:
            phone = extract_phone(body)
            if phone:
                async with session_scope() as session:
                    await users_service.set_phone(session, max_user_id, phone)
                    user = await users_service.get_or_create(session, max_user_id=max_user_id)
                if not user.first_name or user.first_name == "Удалено":
                    await event.message.answer(texts.CONTACT_RECEIVED, attachments=[keyboards.cancel_keyboard()])
                    async with session_scope() as session:
                        await users_service.set_state(session, max_user_id, DialogState.AWAITING_NAME)
                else:
                    await _ask_contact_or_skip(event, max_user_id)
            else:
                await event.message.answer(
                    "Нажмите кнопку «Поделиться контактом», чтобы передать номер.",
                    attachments=[keyboards.contact_request_keyboard()],
                )
            return

        if state == DialogState.AWAITING_NAME:
            name = text_body.strip()[:120]
            if not name:
                await event.message.answer("Имя не должно быть пустым. Введите ещё раз.")
                return
            async with session_scope() as session:
                await users_service.set_first_name(session, max_user_id, name)
                await users_service.set_state(session, max_user_id, DialogState.AWAITING_ADDRESS)
            await event.message.answer(texts.NAME_RECEIVED, attachments=[keyboards.cancel_keyboard()])
            return

        if state == DialogState.AWAITING_ADDRESS:
            address = text_body.strip()[:500]
            if not address:
                await event.message.answer("Адрес не должен быть пустым. Введите ещё раз.")
                return
            async with session_scope() as session:
                await users_service.update_dialog_data(session, max_user_id, {"address": address})
            await _ask_topic(event, max_user_id)
            return

        if state == DialogState.AWAITING_SUMMARY:
            chunk = text_body.strip()
            atts = collect_attachments(body)
            async with session_scope() as session:
                user = await users_service.get_or_create(session, max_user_id=max_user_id)
                data = dict(user.dialog_data or {})
                if chunk:
                    data.setdefault("summary_chunks", []).append(chunk)
                if atts:
                    data.setdefault("attachments", []).extend(atts)
                user.dialog_data = data
                await session.flush()
            _schedule_collect_timeout(event, max_user_id)
            return

        if state == DialogState.IDLE:
            # Idle: try to reopen an answered appeal as a follow-up; fall back to menu hint.
            handled = await op_reply.handle_user_followup(event, text_body)
            if not handled:
                await event.message.answer(texts.UNKNOWN_INPUT, attachments=[keyboards.main_menu()])
            return
