"""Действия оператора над конкретным обращением.

Выделено из handlers/admin_commands.py (рефакторинг 2026-05-10).

- ✉️ Ответить (reply_intent + cancel)
- 🔁 Возобновить (reopen)
- ⛔ Закрыть (close)
- 🚫 Заблокировать жителя / ✅ Разблокировать
- 🗑 Удалить ПДн жителя
"""
from __future__ import annotations

import logging

from aemr_bot import texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import ensure_operator, ensure_role
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import card_format
from aemr_bot.services import operators as operators_service
from aemr_bot.services import users as users_service
from aemr_bot.utils.event import get_user_id, is_admin_chat, send_or_edit_screen

log = logging.getLogger(__name__)


async def _show_appeal_card_or_result(event, appeal_id: int, fallback_text: str) -> None:
    from aemr_bot import keyboards as kbds

    try:
        async with session_scope() as session:
            appeal = await appeals_service.get_by_id(session, appeal_id)
    except Exception:
        log.exception("appeal card refresh failed for appeal_id=%s", appeal_id)
        appeal = None
    if appeal is not None and appeal.user is not None:
        try:
            await send_or_edit_screen(
                event,
                chat_id=cfg.admin_group_id,
                text=card_format.admin_card(appeal, appeal.user),
                attachments=[
                    kbds.appeal_admin_actions(
                        appeal.id,
                        appeal.status,
                        is_it=True,
                        user_blocked=bool(appeal.user.is_blocked),
                        closed_due_to_revoke=bool(appeal.closed_due_to_revoke),
                    )
                ],
            )
            return
        except Exception:
            log.exception("appeal card render/edit failed for appeal_id=%s", appeal_id)
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=fallback_text,
        attachments=[kbds.op_back_to_menu_keyboard()],
    )


async def run_reply_intent(event, appeal_id: int) -> None:
    """Кнопка «✉️ Ответить» под карточкой обращения. Запоминает намерение
    оператора в in-memory словаре. Следующее текстовое сообщение
    оператора в админ-группе доставляется как /reply <appeal_id>
    <текст>.

    Защиты:
    - запрещаем reply-intent на CLOSED-обращение
    - запрещаем для is_blocked жителя
    - сбрасываем активные wizard'ы (broadcast, add-operator) этого
      оператора, чтобы следующий текст не утёк туда
    """
    from aemr_bot import keyboards as kbds
    from aemr_bot.db.models import AppealStatus
    from aemr_bot.handlers import admin_operators
    from aemr_bot.handlers import broadcast as broadcast_handler
    from aemr_bot.handlers import operator_reply as op_reply
    from aemr_bot.utils.event import ack_callback

    if not is_admin_chat(event):
        await ack_callback(event)
        return
    if not await ensure_operator(event):
        await ack_callback(event)
        return
    operator_id = get_user_id(event)
    if operator_id is None:
        await ack_callback(event)
        return

    async with session_scope() as session:
        appeal = await appeals_service.get_by_id(session, appeal_id)
    if appeal is None:
        await ack_callback(event)
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=texts.OP_APPEAL_NOT_FOUND.format(number=appeal_id),
            attachments=[kbds.op_back_to_menu_keyboard()],
        )
        return
    if appeal.status == AppealStatus.CLOSED.value:
        await ack_callback(event)
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=(
                f"Обращение #{appeal_id} закрыто. Сначала верните его в "
                f"работу кнопкой «🔁 Возобновить» под карточкой."
            ),
            attachments=[kbds.op_back_to_menu_keyboard()],
        )
        return
    if appeal.user is None or appeal.user.is_blocked:
        await ack_callback(event)
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=(
                f"Житель по обращению #{appeal_id} заблокирован — ответ не "
                f"будет доставлен через бот. Если ответ всё-таки нужен, "
                f"сначала снимите блокировку."
            ),
            attachments=[kbds.op_back_to_menu_keyboard()],
        )
        return

    # Сбрасываем чужие wizard'ы того же оператора.
    broadcast_handler._wizards.pop(operator_id, None)
    admin_operators._op_wizards.pop(operator_id, None)

    op_reply.remember_reply_intent(operator_id, appeal_id)
    await ack_callback(event, f"Ответ на #{appeal_id}")
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=(
            f"✉️ Введите текст ответа на обращение #{appeal_id}.\n"
            f"Лимит {cfg.answer_max_chars} символов. Просто отправьте "
            f"следующее сообщение в этот чат, либо «Отменить» ниже."
        ),
        attachments=[kbds.cancel_reply_intent_keyboard()],
    )


async def run_reply_cancel(event) -> None:
    """Кнопка «❌ Отменить ответ» под подсказкой ввода."""
    from aemr_bot.handlers import operator_reply as op_reply
    from aemr_bot.utils.event import ack_callback
    from aemr_bot import keyboards as kbds

    operator_id = get_user_id(event)
    if operator_id is None:
        await ack_callback(event)
        return
    cancelled_appeal = op_reply.drop_reply_intent(operator_id)
    await ack_callback(event)
    if cancelled_appeal is not None:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=f"Ответ на обращение #{cancelled_appeal} отменён.",
            attachments=[kbds.op_back_to_menu_keyboard()],
        )
    else:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text="Мастер ответа уже закрыт.",
            attachments=[kbds.op_back_to_menu_keyboard()],
        )


async def run_reopen(event, appeal_id: int) -> None:
    """Кнопочный аналог /reopen N — возобновить обращение."""
    from aemr_bot.utils.event import ack_callback

    if not await ensure_operator(event):
        return
    async with session_scope() as session:
        ok = await appeals_service.reopen(session, appeal_id)
        if ok:
            await operators_service.write_audit(
                session,
                operator_max_user_id=get_user_id(event),
                action="reopen",
                target=f"appeal #{appeal_id}",
            )
    await ack_callback(event)
    await _show_appeal_card_or_result(
        event,
        appeal_id,
        (
            texts.OP_APPEAL_REOPENED.format(number=appeal_id)
            if ok
            else texts.OP_APPEAL_NOT_FOUND.format(number=appeal_id)
        ),
    )


async def run_close(event, appeal_id: int) -> None:
    """Кнопочный аналог /close N — закрыть обращение без ответа."""
    from aemr_bot.utils.event import ack_callback

    if not await ensure_operator(event):
        return
    async with session_scope() as session:
        ok = await appeals_service.close(session, appeal_id)
        if ok:
            await operators_service.write_audit(
                session,
                operator_max_user_id=get_user_id(event),
                action="close",
                target=f"appeal #{appeal_id}",
            )
    await ack_callback(event)
    await _show_appeal_card_or_result(
        event,
        appeal_id,
        (
            texts.OP_APPEAL_CLOSED.format(number=appeal_id)
            if ok
            else texts.OP_APPEAL_NOT_FOUND.format(number=appeal_id)
        ),
    )


async def run_block_for_appeal(
    event, appeal_id: int, *, blocked: bool
) -> None:
    """Кнопки «🚫 Заблокировать жителя» / «✅ Разблокировать»."""
    from aemr_bot import keyboards as kbds
    from aemr_bot.utils.event import ack_callback

    if not await ensure_role(event, OperatorRole.IT):
        return
    async with session_scope() as session:
        appeal = await appeals_service.get_by_id(session, appeal_id)
        if appeal is None or appeal.user is None:
            await ack_callback(event)
            await send_or_edit_screen(
                event,
                chat_id=cfg.admin_group_id,
                text=texts.OP_APPEAL_NOT_FOUND.format(number=appeal_id),
                attachments=[kbds.op_back_to_menu_keyboard()],
            )
            return
        target_id = appeal.user.max_user_id
        ok = await users_service.set_blocked(session, target_id, blocked=blocked)
        if ok:
            await operators_service.write_audit(
                session,
                operator_max_user_id=get_user_id(event),
                action="block" if blocked else "unblock",
                target=f"user max_id={target_id}",
            )
    await ack_callback(event)
    if ok:
        msg = (
            texts.OP_USER_BLOCKED if blocked else texts.OP_USER_UNBLOCKED
        ).format(max_user_id=target_id)
    else:
        msg = "Не удалось обновить статус. См. логи."
    await _show_appeal_card_or_result(event, appeal_id, msg)


async def run_erase_for_appeal(event, appeal_id: int) -> None:
    """Кнопка «🗑 Удалить ПДн жителя» в карточке обращения (только для it)."""
    from aemr_bot import keyboards as kbds
    from aemr_bot.utils.event import ack_callback

    if not await ensure_role(event, OperatorRole.IT):
        return
    async with session_scope() as session:
        appeal = await appeals_service.get_by_id(session, appeal_id)
        if appeal is None or appeal.user is None:
            await ack_callback(event)
            await send_or_edit_screen(
                event,
                chat_id=cfg.admin_group_id,
                text=texts.OP_APPEAL_NOT_FOUND.format(number=appeal_id),
                attachments=[kbds.op_back_to_menu_keyboard()],
            )
            return
        target_id = appeal.user.max_user_id
        ok = await users_service.erase_pdn(session, target_id)
        if ok:
            await operators_service.write_audit(
                session,
                operator_max_user_id=get_user_id(event),
                action="erase",
                target=f"user max_id={target_id}",
            )
    await ack_callback(event)
    if ok:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=texts.OP_USER_ERASED.format(max_user_id=target_id),
            attachments=[kbds.op_back_to_menu_keyboard()],
        )
    else:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text="Пользователь не найден.",
            attachments=[kbds.op_back_to_menu_keyboard()],
        )
