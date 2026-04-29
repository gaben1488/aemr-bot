from __future__ import annotations

import asyncio
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
from aemr_bot.utils.attachments import collect_attachments, extract_phone

_collect_timers: dict[int, asyncio.Task] = {}


async def _send_to_admin_card(bot, text: str) -> str | None:
    """Send formatted card into admin group, return its message_id."""
    if not cfg.admin_group_id:
        return None
    sent = await bot.send_message(chat_id=cfg.admin_group_id, text=text)
    mid = getattr(sent, "message_id", None) or getattr(getattr(sent, "body", None), "mid", None)
    return str(mid) if mid is not None else None


async def _start_appeal_flow(event, max_user_id: int):
    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        if not user.consent_pdn_at:
            await users_service.set_state(session, max_user_id, DialogState.AWAITING_CONSENT, data={})
            policy_url = await settings_store.get(session, "policy_url")
        else:
            policy_url = None

    if policy_url is not None:
        await event.bot.send_message(
            chat_id=event.chat_id,
            text=texts.CONSENT_REQUEST.format(policy_url=policy_url),
            attachments=[keyboards.consent_keyboard()],
        )
        return

    await _ask_contact_or_skip(event, max_user_id)


async def _ask_contact_or_skip(event, max_user_id: int):
    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        if not user.phone:
            await users_service.set_state(session, max_user_id, DialogState.AWAITING_CONTACT, data={})
        elif not user.first_name or user.first_name == "Удалено":
            await users_service.set_state(session, max_user_id, DialogState.AWAITING_NAME, data={})
        else:
            await users_service.set_state(session, max_user_id, DialogState.AWAITING_ADDRESS, data={})

    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        state = DialogState(user.dialog_state)

    if state == DialogState.AWAITING_CONTACT:
        await event.bot.send_message(
            chat_id=event.chat_id,
            text=texts.CONTACT_REQUEST,
            attachments=[keyboards.contact_request_keyboard()],
        )
    elif state == DialogState.AWAITING_NAME:
        await event.bot.send_message(
            chat_id=event.chat_id,
            text=texts.CONTACT_RECEIVED,
            attachments=[keyboards.cancel_keyboard()],
        )
    elif state == DialogState.AWAITING_ADDRESS:
        await event.bot.send_message(
            chat_id=event.chat_id,
            text=texts.NAME_RECEIVED,
            attachments=[keyboards.cancel_keyboard()],
        )


async def _ask_topic(event, max_user_id: int):
    async with session_scope() as session:
        topics = await settings_store.get(session, "topics") or ["Другое"]
        await users_service.set_state(session, max_user_id, DialogState.AWAITING_TOPIC)
    await event.bot.send_message(
        chat_id=event.chat_id,
        text=texts.ADDRESS_RECEIVED,
        attachments=[keyboards.topics_keyboard(topics)],
    )


async def _ask_summary(event, max_user_id: int):
    async with session_scope() as session:
        await users_service.set_state(session, max_user_id, DialogState.AWAITING_SUMMARY)
    await event.bot.send_message(
        chat_id=event.chat_id,
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
        summary = "\n".join(data.get("summary_chunks", []))
        attachments = data.get("attachments", [])

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

    await event.bot.send_message(
        chat_id=event.chat_id,
        text=texts.APPEAL_ACCEPTED.format(number=appeal.id, sla_hours=cfg.sla_response_hours),
        attachments=[keyboards.main_menu()],
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

    _collect_timers[max_user_id] = asyncio.create_task(_runner())


def register(dp: Dispatcher) -> None:
    @dp.message_callback()
    async def on_callback(event: MessageCallback):
        payload = (event.callback.payload or "") if hasattr(event, "callback") else ""
        max_user_id = getattr(event.user, "user_id", None) if getattr(event, "user", None) else None
        if max_user_id is None:
            return

        if payload == "menu:new_appeal":
            await event.answer_on_callback(notification="")
            await _start_appeal_flow(event, max_user_id)
            return

        if payload == "consent:yes":
            async with session_scope() as session:
                await users_service.set_consent(session, max_user_id)
            await event.answer_on_callback(notification=texts.CONSENT_ACCEPTED)
            await _ask_contact_or_skip(event, max_user_id)
            return

        if payload == "consent:no":
            async with session_scope() as session:
                await users_service.reset_state(session, max_user_id)
            await event.answer_on_callback(notification="")
            await event.bot.send_message(
                chat_id=event.chat_id,
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
            await event.answer_on_callback(notification="")
            await event.bot.send_message(
                chat_id=event.chat_id,
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
            await event.answer_on_callback(notification="")
            await _ask_summary(event, max_user_id)
            return

        if payload == "appeal:submit":
            timer = _collect_timers.pop(max_user_id, None)
            if timer and not timer.done():
                timer.cancel()
            await event.answer_on_callback(notification="")
            await _finalize_appeal(event, max_user_id)
            return

    @dp.message_created()
    async def on_message(event: MessageCreated):
        from aemr_bot.handlers import operator_reply as op_reply

        chat_id = getattr(event, "chat_id", None)
        if chat_id is None:
            return

        body = getattr(event.message, "body", None) or event.message
        text_body = getattr(body, "text", None) or ""
        if text_body.startswith("/"):
            return

        # Branch A: admin group → maybe an operator reply via reply-to
        if cfg.admin_group_id and chat_id == cfg.admin_group_id:
            await op_reply.handle_operator_reply(event, body, text_body)
            return

        max_user_id = getattr(event.user, "user_id", None) if getattr(event, "user", None) else None
        if max_user_id is None:
            return

        async with session_scope() as session:
            user = await users_service.get_or_create(
                session,
                max_user_id=max_user_id,
                first_name=getattr(event.user, "first_name", None),
            )
            state = DialogState(user.dialog_state)

        if state == DialogState.AWAITING_CONTACT:
            phone = extract_phone(getattr(event.message, "body", None) or event.message)
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
            atts = collect_attachments(getattr(event.message, "body", None) or event.message)
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
