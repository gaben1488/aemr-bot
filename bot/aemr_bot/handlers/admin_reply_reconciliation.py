"""Operator reconciliation of uncertain reply deliveries."""
from aemr_bot import keyboards as kbds
from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import ensure_operator
from aemr_bot.services import data_erasure
from aemr_bot.utils.event import ack_callback, send_or_edit_screen


async def _op_reply_resolve(event, payload: str) -> None:
    """Confirm a human's explicit reconciliation, never send automatically."""
    from maxapi.types import CallbackButton
    from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

    from aemr_bot.db.models import OperatorReplyAttempt
    from aemr_bot.services import appeal_locks, operator_reply_attempts, privacy_epoch
    from aemr_bot.utils.event import get_user_id

    await ack_callback(event)
    if not await ensure_operator(event):
        return
    bits = payload.split(":")
    if len(bits) not in {4, 5} or not bits[2].isdigit() or bits[3] not in {"delivered", "not_delivered", "discard"}:
        return
    attempt_id, choice = int(bits[2]), bits[3]
    if bits[1] == "reply_resolve":
        if len(bits) != 4:
            return
        try:
            async with data_erasure.personal_view() as view:
                async with session_scope() as session:
                    attempt = await session.get(OperatorReplyAttempt, attempt_id)
                if attempt is None:
                    await send_or_edit_screen(event, chat_id=cfg.admin_group_id, text="Ответ уже недоступен.",
                                              attachments=[kbds.op_back_to_menu_keyboard()])
                    return
                await view.include(appeal_id=attempt.appeal_id)
                parts = list(attempt.delivery_parts or [])
                uncertain = next((part for part in parts if part.get("state") == "sending"), None)
                revision = operator_reply_attempts.reconciliation_revision(attempt)
                keyboard = InlineKeyboardBuilder()
                keyboard.row(CallbackButton(text="✅ Подтвердить", payload=f"op:reply_confirm:{attempt_id}:{choice}:{revision}"))
                keyboard.row(CallbackButton(text="↩️ Назад", payload=f"op:reply_attempt:{attempt_id}:0"))
                subject = f"часть № {uncertain['index'] + 1} ответа" if uncertain else "ответ"
                verb = "доставлена" if uncertain else "доставлен"
                conclusion = (f"{subject} действительно {verb} жителю" if choice == "delivered"
                              else f"{subject} жителю не {verb}")
                if choice == "delivered" and (not parts or not all("attachments" in part for part in parts)):
                    conclusion = (f"весь текст и каждое из {len(attempt.attachments or [])} вложений "
                                  "этого ответа действительно доставлены жителю")
                prompt = ("Удалить сохранённый черновик недоставленного ответа?"
                          if choice == "discard" else
                          f"Вы проверили переписку и установили, что {conclusion}?\n\n"
                          "Подтверждайте только после проверки. Бот не отправит ответ повторно.")
                await send_or_edit_screen(event, chat_id=cfg.admin_group_id,
                    text=prompt,
                    attachments=[keyboard.as_markup()])
        except data_erasure.PersonalDataErasedError:
            await send_or_edit_screen(event, chat_id=cfg.admin_group_id, text="Ответ уже недоступен.",
                                      attachments=[kbds.op_back_to_menu_keyboard()])
        return
    if len(bits) != 5 or len(bits[4]) != 16:
        await send_or_edit_screen(event, chat_id=cfg.admin_group_id,
            text="Кнопка устарела. Откройте ответ и проверьте его состояние заново.",
            attachments=[kbds.op_back_to_menu_keyboard()])
        return
    revision = bits[4]
    actor = get_user_id(event)
    if actor is None:
        return
    async with session_scope() as barrier:
        await privacy_epoch.hold_delivery(barrier)
        async with session_scope() as session:
            attempt = await session.get(OperatorReplyAttempt, attempt_id)
            appeal_id = attempt.appeal_id if attempt else None
        if appeal_id is None:
            changed = False
        else:
            await appeal_locks.hold(barrier, appeal_id)
            async with session_scope() as session:
                if choice == "discard":
                    changed = await operator_reply_attempts.discard_not_delivered(
                        session, attempt_id, actor_max_id=actor, expected_revision=revision,
                    )
                else:
                    try:
                        changed = await operator_reply_attempts.resolve_unknown(
                            session, attempt_id, delivered=choice == "delivered", actor_max_id=actor,
                            expected_revision=revision,
                        )
                    except ValueError as exc:
                        await send_or_edit_screen(event, chat_id=cfg.admin_group_id, text=str(exc),
                                                  attachments=[kbds.op_back_to_menu_keyboard()])
                        return
        text = ("✅ Отметка сохранена. Повторной отправки не было." if changed else
                "Состояние уже изменилось или ответ недоступен. Обновите список.")
        await send_or_edit_screen(event, chat_id=cfg.admin_group_id, text=text,
                                  attachments=[kbds.op_back_to_menu_keyboard()])
