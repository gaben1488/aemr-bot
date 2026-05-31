"""UI шаблонов рассылок — CRUD-операции (delete / rename / clone).

Декомпозиция god-объекта `broadcast_templates.py` (DDD tactical,
2026-06-01). Ответственность модуля — изменяющие пул сценарии, кроме
create/edit-wizard (те в `broadcast_templates_wizard`):

  - `_ask_delete`/`_do_delete` (`op:tmpl:delete:<id>` →
    `op:tmpl:delete_ok:<id>`) — soft-delete (archive) + запись в audit_log;
  - `_start_rename`/`_step_rename` (`op:tmpl:rename:<id>`) — однострочный
    wizard переименования + audit;
  - `_start_clone`/`_step_clone_name` (`op:tmpl:clone:<id>`) — клон
    шаблона (текст+картинки источника, спрашиваем только имя) + audit.

Wizard-state импортируем из `broadcast_templates_state` по ссылке —
состояние единое на все группы. Dispatch и re-export остаются в фасаде
`broadcast_templates.py`.
"""

from __future__ import annotations

import logging

from aemr_bot import keyboards, texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import ensure_role, get_operator
from aemr_bot.services import broadcast_templates as templates_service
from aemr_bot.services import operators as operators_service
from aemr_bot.utils.event import get_user_id, send_or_edit_screen

from aemr_bot.handlers.broadcast_templates_state import (
    _TmplWizardState,
    _drop_expired,
    _validate_tmpl_name,
    _wizards,
)


log = logging.getLogger(__name__)


# ---- rename ----------------------------------------------------------

async def _start_rename(event, template_id: int) -> None:
    if not await ensure_role(event, OperatorRole.IT, OperatorRole.COORDINATOR):
        return
    actor_id = get_user_id(event)
    if actor_id is None:
        return
    async with session_scope() as session:
        tmpl = await templates_service.get_by_id(session, template_id)
    if tmpl is None:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=texts.OP_TMPL_NOT_FOUND,
            attachments=[keyboards.op_back_to_menu_keyboard()],
        )
        return
    _drop_expired()
    _wizards[actor_id] = _TmplWizardState(
        step="rename_awaiting_name", target_id=template_id
    )
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=texts.OP_TMPL_RENAME_PROMPT.format(
            old_name=tmpl.name, limit=templates_service.MAX_NAME_LEN
        ),
        attachments=[keyboards.broadcast_template_cancel_keyboard()],
    )


async def _step_rename(
    event,
    actor_id: int,
    state: _TmplWizardState,
    text: str,
    *,
    op_id: int | None,
) -> bool:
    if (err := _validate_tmpl_name(text)) is not None:
        await event.message.answer(
            err,
            attachments=[keyboards.broadcast_template_cancel_keyboard()],
        )
        return True
    if state.target_id is None:
        _wizards.pop(actor_id, None)
        return False
    try:
        async with session_scope() as session:
            tmpl_before = await templates_service.get_by_id(
                session, state.target_id
            )
            if tmpl_before is None:
                _wizards.pop(actor_id, None)
                await event.message.answer(
                    texts.OP_TMPL_NOT_FOUND,
                    attachments=[keyboards.op_back_to_menu_keyboard()],
                )
                return True
            old_name = tmpl_before.name
            tmpl_after = await templates_service.rename(
                session, state.target_id, text
            )
            if op_id is not None and actor_id is not None:
                await operators_service.write_audit(
                    session,
                    operator_max_user_id=actor_id,
                    action="broadcast_template_rename",
                    target=f"template #{state.target_id}",
                    details={"old_name": old_name, "new_name": text},
                )
            new_name = tmpl_after.name
    except templates_service.TemplateNameAlreadyExists:
        await event.message.answer(
            texts.OP_TMPL_NAME_TAKEN.format(name=text),
            attachments=[keyboards.broadcast_template_cancel_keyboard()],
        )
        return True
    target_id = state.target_id
    _wizards.pop(actor_id, None)
    await event.message.answer(
        texts.OP_TMPL_RENAMED.format(old_name=old_name, new_name=new_name),
        attachments=[keyboards.broadcast_template_card_keyboard(target_id)],
    )
    return True


# ---- clone (PR template-editor-upgrade) ------------------------------


async def _start_clone(event, template_id: int) -> None:
    """`op:tmpl:clone:<id>` — клонировать шаблон.

    Берём текст и картинки источника, открываем wizard на шаге ввода
    нового имени. После имени сразу сохраняем — text/attachments не
    спрашиваем, они уже есть в pending_*.
    """
    if not await ensure_role(event, OperatorRole.IT, OperatorRole.COORDINATOR):
        return
    actor_id = get_user_id(event)
    if actor_id is None:
        return
    async with session_scope() as session:
        tmpl = await templates_service.get_by_id(session, template_id)
    if tmpl is None:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=texts.OP_TMPL_NOT_FOUND,
            attachments=[keyboards.op_back_to_menu_keyboard()],
        )
        return
    _drop_expired()
    _wizards[actor_id] = _TmplWizardState(
        step="clone_awaiting_name",
        pending_text=tmpl.text,
        pending_attachments=list(tmpl.attachments),
        target_id=tmpl.id,  # источник, для аудит-лога
        source_name=tmpl.name,
    )
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=texts.OP_TMPL_CLONE_NAME_PROMPT.format(
            source_name=tmpl.name,
            image_count=len(tmpl.attachments),
            limit=templates_service.MAX_NAME_LEN,
        ),
        attachments=[keyboards.broadcast_template_cancel_keyboard()],
    )


async def _step_clone_name(
    event,
    actor_id: int,
    state: _TmplWizardState,
    text: str,
    *,
    op_id: int | None,
) -> bool:
    """Принять имя для клона; создать шаблон сразу (text+attachments
    уже в pending_*, превью не нужен — это копия)."""
    if (err := _validate_tmpl_name(text)) is not None:
        await event.message.answer(
            err,
            attachments=[keyboards.broadcast_template_cancel_keyboard()],
        )
        return True
    try:
        async with session_scope() as session:
            tmpl = await templates_service.create_template(
                session,
                name=text,
                text=state.pending_text,
                attachments=state.pending_attachments,
                created_by_operator_id=op_id,
            )
            if op_id is not None:
                await operators_service.write_audit(
                    session,
                    operator_max_user_id=actor_id,
                    action="broadcast_template_clone",
                    target=f"template #{tmpl.id}",
                    details={
                        "source_id": state.target_id,
                        "source_name": state.source_name,
                        "new_name": text,
                    },
                )
            new_id = tmpl.id
            new_name = tmpl.name
            source_name = state.source_name
    except templates_service.TemplateNameAlreadyExists:
        await event.message.answer(
            texts.OP_TMPL_NAME_TAKEN.format(name=text),
            attachments=[keyboards.broadcast_template_cancel_keyboard()],
        )
        return True
    _wizards.pop(actor_id, None)
    await event.message.answer(
        texts.OP_TMPL_CLONED.format(
            name=new_name, source_name=source_name, number=new_id
        ),
        attachments=[keyboards.broadcast_template_card_keyboard(new_id)],
    )
    return True


# ---- delete (confirm-then-archive) ----------------------------------

async def _ask_delete(event, template_id: int) -> None:
    if not await ensure_role(event, OperatorRole.IT, OperatorRole.COORDINATOR):
        return
    async with session_scope() as session:
        tmpl = await templates_service.get_by_id(session, template_id)
    if tmpl is None:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=texts.OP_TMPL_NOT_FOUND,
            attachments=[keyboards.op_back_to_menu_keyboard()],
        )
        return
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=texts.OP_TMPL_DELETE_CONFIRM.format(name=tmpl.name),
        attachments=[
            keyboards.broadcast_template_delete_confirm_keyboard(template_id)
        ],
    )


async def _do_delete(event, template_id: int) -> None:
    if not await ensure_role(event, OperatorRole.IT, OperatorRole.COORDINATOR):
        return
    actor_id = get_user_id(event)
    op = await get_operator(event)
    async with session_scope() as session:
        tmpl = await templates_service.get_by_id(session, template_id)
        if tmpl is None:
            await send_or_edit_screen(
                event,
                chat_id=cfg.admin_group_id,
                text=texts.OP_TMPL_NOT_FOUND,
                attachments=[keyboards.op_back_to_menu_keyboard()],
            )
            return
        name = tmpl.name
        await templates_service.archive(session, template_id)
        if actor_id is not None and op is not None:
            await operators_service.write_audit(
                session,
                operator_max_user_id=actor_id,
                action="broadcast_template_delete",
                target=f"template #{template_id}",
                details={"name": name},
            )
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=texts.OP_TMPL_DELETED.format(name=name),
        attachments=[keyboards.op_back_to_menu_keyboard()],
    )
