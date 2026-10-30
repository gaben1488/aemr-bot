"""Explicit composition of operator replies, backed by PostgreSQL."""
from __future__ import annotations

import logging

from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from aemr_bot.config import settings
from aemr_bot.db.models import OperatorReplyAttempt
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import get_operator
from aemr_bot.handlers._common import op_screen
from aemr_bot.services import appeals, idempotency, operator_drafts
from aemr_bot.utils.attachments import collect_attachments, has_unsupported_appeal_attachments
from aemr_bot.utils.event import ack_callback, extract_message_id, is_admin_chat
from aemr_bot.utils.text_pages import split_text_units

log = logging.getLogger(__name__)


async def show(event, draft, *, notice: str = "", page: int = 0, new: bool = False):
    data = draft.data or {}
    text = "\n".join(data.get("summary_chunks") or [])
    pages = split_text_units(text, 2200)
    page = max(0, min(page, len(pages) - 1))
    kind = "Итоговый ответ" if draft.is_final else "Промежуточный ответ"
    summary = (f"✍️ {kind} · обращение № {draft.appeal_id}\n"
               f"Текст: {len(text)}/{settings.answer_max_chars} · "
               f"вложения: {len(data.get('attachments') or [])}/20\n")
    kb = InlineKeyboardBuilder()
    def button(label, action):
        return CallbackButton(text=label, payload=f"op:replydraft:{action}:{draft.token}")
    if draft.state == "editing":
        summary += "Добавляйте сообщения и файлы. Житель получит ответ после нажатия «Отправить».\n"
        kb.row(button("📤 Отправить", "send"), button("⏸ Сохранить и выйти", "pause"))
        kb.row(button("✏️ Заменить текст", "edit"), button("🗑 Убрать вложения", "files"))
        kb.row(button("❌ Отменить ответ", "cancel"))
    elif draft.state == "paused":
        summary += "Черновик сохранён. Чтобы добавить части или отправить его, нажмите «Продолжить».\n"
        kb.row(button("✍️ Продолжить", "resume"))
        kb.row(button("❌ Отменить ответ", "cancel"))
    else:
        summary += "Отправка уже началась. Проверьте историю и диагностику доставки; автоматического повтора нет.\n"
        kb.row(CallbackButton(text="📬 Диагностика доставки", payload="op:reply_issues:0"))
    if len(pages) > 1:
        nav = []
        if page:
            nav.append(button("◀️", f"page{page - 1}"))
        if page + 1 < len(pages):
            nav.append(button("▶️", f"page{page + 1}"))
        kb.row(*nav)
    kb.row(CallbackButton(text="🏠 В меню", payload="op:menu"))
    rendered = (summary + (f"\n{notice}\n" if notice else "") +
                f"\nТекст · {page + 1}/{len(pages)}\n{pages[page] or 'Пока нет текста.'}")
    screen_mid = data.get("_screen_mid")
    if screen_mid and not new:
        try:
            await event.bot.edit_message(message_id=screen_mid, text=rendered,
                                         attachments=[kb.as_markup()], notify=False)
        except Exception as exc:
            # The durable content is safe. Do not create a new screen on every
            # network error; reopening through the menu can show the draft.
            log.warning("operator draft screen update failed (%s)", type(exc).__name__)
        return
    sent = await op_screen(event, rendered, kb.as_markup(), new=True)
    mid = extract_message_id(sent)
    if mid:
        async with session_scope() as session:
            current = await operator_drafts.get(session, draft.operator_id, locked=True)
            if current is not None and current.token == draft.token:
                current.data = {**current.data, "_screen_mid": mid}



async def begin(event, appeal_id: int, *, is_final: bool):
    operator = await get_operator(event)
    if operator is None or not is_admin_chat(event):
        await ack_callback(event)
        return
    notice = ""
    async with session_scope() as session:
        appeal = await appeals.get_by_id(session, appeal_id, for_update=True)
        if (appeal is None or appeal.status not in {"new", "in_progress"}
                or appeal.user is None or appeal.user.is_blocked or appeal.user.consent_pdn_at is None):
            await ack_callback(event, "Ответ сейчас недоступен. Проверьте состояние обращения и согласие.")
            return
        draft = await operator_drafts.get(session, operator.id, locked=True)
        if draft is None:
            try:
                draft = await operator_drafts.start(session, operator.id, appeal_id, is_final=is_final)
            except operator_drafts.DraftConflict:
                draft = await operator_drafts.get(session, operator.id, locked=True)
                notice = "Другой запрос уже создал черновик. Его данные сохранены."
            else:
                await appeals.mark_in_progress(session, appeal_id)
        else:
            notice = "Сначала завершите или отмените сохранённый ответ. Его обращение и тип не изменены."
    from aemr_bot.handlers import admin_operators, admin_settings, broadcast
    from aemr_bot.services import wizard_registry
    broadcast._wizards.pop(operator.max_user_id, None)
    admin_operators._op_wizard_drop(operator.max_user_id)
    admin_settings._intent_drop(operator.max_user_id)
    wizard_registry.drop_reply_intent(operator.max_user_id)
    await ack_callback(event)
    await show(event, draft, notice=notice, new=True)


async def collect(event, body, text: str) -> bool:
    if not is_admin_chat(event):
        return False
    operator = await get_operator(event)
    if operator is None:
        return False
    notice = "Часть сохранена."
    async with session_scope() as session:
        draft = await operator_drafts.get(session, operator.id, locked=True)
        if draft is None or draft.state != "editing":
            return False
        try:
            if has_unsupported_appeal_attachments(body):
                raise ValueError("Можно добавить фото, видео или документы. Эта часть не сохранена.")
            source = idempotency.build_idempotency_key(event) or ""
            draft = await operator_drafts.append(session, operator.id, draft.token,
                source=source, text=text, attachments=collect_attachments(body))
        except ValueError as exc:
            notice = {"text": "Часть не сохранена: превышен лимит текста.",
                      "attachments": "Часть не сохранена: превышен лимит вложений."}.get(str(exc), str(exc))
    await show(event, draft, notice=notice)
    return True


async def callback(event, payload: str):
    operator = await get_operator(event)
    if operator is None or not is_admin_chat(event):
        await ack_callback(event)
        return
    parts = payload.split(":")
    if len(parts) != 4:
        await ack_callback(event, "Кнопка устарела.")
        return
    action, token = parts[2:]
    async with session_scope() as session:
        draft = await operator_drafts.get(session, operator.id, locked=True)
        if draft is None or draft.token != token:
            await ack_callback(event, "Кнопка относится к другому черновику.")
            return
        if action == "cancel" and draft.state in {"editing", "paused"}:
            await session.delete(draft)
            await ack_callback(event, "Черновик отменён.")
            return
        if action == "open":
            page = 0
        elif action.startswith("page") and action[4:].isdigit():
            page = int(action[4:])
        elif action == "resume" and draft.state == "paused":
            draft.state = "editing"
            page = 0
        elif draft.state != "editing":
            await ack_callback(event, "Отправка уже началась или черновик приостановлен.")
            return
        elif action == "send":
            try:
                await operator_drafts.claim(session, operator.id, token)
            except ValueError as exc:
                await ack_callback(event, str(exc))
                return
            page = 0
        elif action == "pause":
            draft.state = "paused"
            page = 0
        elif action in {"edit", "files"}:
            # Editing asks for replacement; old text remains until valid input.
            draft.data = {**draft.data, **({"replace_text": True} if action == "edit" else {"attachments": []})}
            page = 0
        else:
            await ack_callback(event, "Кнопка устарела.")
            return
        await session.flush()
        composition = {**draft.data, "token": draft.token}
        appeal_id, is_final = draft.appeal_id, draft.is_final
    await ack_callback(event)
    if action != "send":
        await show(event, draft, page=page, new=action == "open",
                   notice="Следующая текстовая часть заменит текст. Вложения сохранятся." if action == "edit" else "")
        return
    from aemr_bot.handlers.operator_reply import _deliver_operator_reply
    async with session_scope() as session:
        appeal = await appeals.get_by_id(session, appeal_id)
    if appeal is not None:
        await _deliver_operator_reply(event, appeal=appeal, operator=operator,
            text="\n".join(composition.get("summary_chunks") or []),
            audit_action="reply_via_draft", is_final=is_final, composition=composition)
    async with session_scope() as session:
        draft = await operator_drafts.get(session, operator.id, locked=True)
        attempt = await session.scalar(select(OperatorReplyAttempt).where(
            OperatorReplyAttempt.source_key == "reply_draft:" + token))
        if draft is None or draft.token != token:
            return
        if attempt is not None and attempt.state == "recorded":
            await session.delete(draft)
            return
        if attempt is None:
            # Validation rejected before any MAX send reservation.
            draft.state = "editing"
    await show(event, draft, notice="Проверьте результат отправки выше.")
