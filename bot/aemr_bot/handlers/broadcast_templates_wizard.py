"""UI шаблонов рассылок — create/edit wizard с превью + навигация.

Декомпозиция god-объекта `broadcast_templates.py` (DDD tactical,
2026-06-01). Ответственность модуля — многошаговые сценарии создания и
редактирования с обязательным preview «как увидит подписчик»:

  - создание (`op:tmpl:new`): `_start_new` → `_step_new_name` →
    `_step_new_text` → `_render_preview_new` → `_save_new`;
  - редактирование текста/картинок (`op:tmpl:edit:<id>`): `_start_edit`
    → `_step_edit` → `_render_preview_edit` → `_save_edit`;
  - навигация назад между шагами (`back_to_name`, `back_to_text_new`,
    `back_to_text_edit`) и общий `_cancel`.

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
from aemr_bot.handlers import broadcast as broadcast_handler
from aemr_bot.handlers._auth import ensure_role, get_operator
from aemr_bot.services import broadcast_templates as templates_service
from aemr_bot.services import operators as operators_service
from aemr_bot.utils import image_attachments as _image_attachments
from aemr_bot.utils.event import get_user_id, send_or_edit_screen

from aemr_bot.handlers.broadcast_templates_state import (
    _TmplWizardState,
    _drop_expired,
    _validate_tmpl_name,
    _wizards,
)


log = logging.getLogger(__name__)


# ---- create wizard ---------------------------------------------------

async def _start_new(event) -> None:
    """`op:tmpl:new` — старт wizard'а «создать шаблон»."""
    if not await ensure_role(event, OperatorRole.IT, OperatorRole.COORDINATOR):
        return
    actor_id = get_user_id(event)
    if actor_id is None:
        return
    _drop_expired()
    _wizards[actor_id] = _TmplWizardState(step="new_awaiting_name")
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=texts.OP_TMPL_NEW_NAME_PROMPT.format(
            limit=templates_service.MAX_NAME_LEN
        ),
        attachments=[keyboards.broadcast_template_cancel_keyboard()],
    )


async def _start_edit(event, template_id: int) -> None:
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
        step="edit_awaiting_text", target_id=template_id
    )
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=texts.OP_TMPL_EDIT_PROMPT.format(
            name=tmpl.name, limit=cfg.broadcast_max_chars
        ),
        attachments=[keyboards.broadcast_template_cancel_keyboard()],
    )


# ---- cancel ----------------------------------------------------------

async def _cancel(event) -> None:
    actor_id = get_user_id(event)
    if actor_id is not None:
        _wizards.pop(actor_id, None)
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=texts.OP_TMPL_CANCELLED,
        attachments=[keyboards.op_back_to_menu_keyboard()],
    )


async def _back_to_name(event) -> None:
    """Шаг 2 → шаг 1: вернуть wizard к вводу имени. Pending text
    сбрасываем — оператор может перезайти и набрать новый. Имя
    оставляем в pending_name, чтобы оператор увидел его в качестве
    подсказки (но это не реализовано тут — prompt стандартный)."""
    actor_id = get_user_id(event)
    if actor_id is None:
        return
    state = _wizards.get(actor_id)
    if state is None or state.step != "new_awaiting_text":
        # Защита от устаревшей кнопки — wizard уже закрыт/в другом шаге.
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=texts.OP_TMPL_CANCELLED,
            attachments=[keyboards.op_back_to_menu_keyboard()],
        )
        return
    state.step = "new_awaiting_name"
    state.renew()
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=texts.OP_TMPL_NEW_NAME_PROMPT.format(
            limit=templates_service.MAX_NAME_LEN
        ),
        attachments=[keyboards.broadcast_template_cancel_keyboard()],
    )


# ---- create wizard steps ---------------------------------------------

async def _step_new_name(
    event, actor_id: int, state: _TmplWizardState, text: str
) -> bool:
    if (err := _validate_tmpl_name(text)) is not None:
        await event.message.answer(
            err,
            attachments=[keyboards.broadcast_template_cancel_keyboard()],
        )
        return True
    state.step = "new_awaiting_text"
    state.pending_name = text
    state.renew()
    await event.message.answer(
        texts.OP_TMPL_NEW_TEXT_PROMPT.format(
            name=text, limit=cfg.broadcast_max_chars
        ),
        attachments=[keyboards.broadcast_template_step2_keyboard()],
    )
    return True


async def _step_new_text(
    event,
    actor_id: int,
    state: _TmplWizardState,
    text: str,
    *,
    op_id: int | None,
) -> bool:
    """Шаг 2: принимаем текст+картинки, показываем превью, ждём подтверждения.

    PR template-editor-upgrade: сохранение перенесено из этого шага в
    callback `op:tmpl:save_new` — оператор сначала видит, как ровно
    это будет выглядеть у подписчика, и только потом подтверждает.
    """
    if not text:
        await event.message.answer(
            texts.OP_TMPL_NEW_TEXT_PROMPT.format(
                name=state.pending_name, limit=cfg.broadcast_max_chars
            ),
            attachments=[keyboards.broadcast_template_step2_keyboard()],
        )
        return True
    if len(text) > cfg.broadcast_max_chars:
        await event.message.answer(
            texts.OP_TMPL_TEXT_TOO_LONG.format(
                actual=len(text), limit=cfg.broadcast_max_chars
            ),
            attachments=[keyboards.broadcast_template_step2_keyboard()],
        )
        return True
    # Картинки — те же helper'ы, что у /broadcast. Лимит наследуем от
    # broadcast_max_images (актуальный из settings_store).
    async with session_scope() as session:
        max_images = await broadcast_handler._resolve_broadcast_max_images(
            session
        )
    attachments = _image_attachments.image_attachments_from_event(
        event, limit=max_images
    )
    # Накопили; переходим в preview-шаг.
    state.step = "new_preview"
    state.pending_text = text
    state.pending_attachments = list(attachments)
    state.renew()
    await _render_preview_new(event, state)
    return True


async def _render_preview_new(event, state: _TmplWizardState) -> None:
    """Превью «как увидит подписчик» для нового шаблона."""
    images = _image_attachments.build_outbound_image_attachments(
        state.pending_attachments
    )
    # Заголовок-объяснение + сам текст шаблона ровно как уйдёт подписчику.
    await event.message.answer(
        texts.OP_TMPL_PREVIEW_HEADER_NEW.format(
            name=state.pending_name,
            image_count=len(state.pending_attachments),
        ),
    )
    await event.message.answer(
        state.pending_text,
        attachments=[
            *images,
            keyboards.broadcast_template_preview_keyboard(None),
        ],
    )


async def _save_new(event) -> None:
    """`op:tmpl:save_new` — окончательное сохранение нового шаблона."""
    actor_id = get_user_id(event)
    if actor_id is None:
        return
    state = _wizards.get(actor_id)
    if state is None or state.step != "new_preview":
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=texts.OP_TMPL_CANCELLED,
            attachments=[keyboards.op_back_to_menu_keyboard()],
        )
        return
    op = await get_operator(event)
    op_id = op.id if op is not None else None
    try:
        async with session_scope() as session:
            tmpl = await templates_service.create_template(
                session,
                name=state.pending_name,
                text=state.pending_text,
                attachments=state.pending_attachments,
                created_by_operator_id=op_id,
            )
            if op_id is not None:
                await operators_service.write_audit(
                    session,
                    operator_max_user_id=actor_id,
                    action="broadcast_template_create",
                    target=f"template #{tmpl.id}",
                    details={
                        "name": tmpl.name,
                        "chars": len(state.pending_text),
                        "image_count": len(state.pending_attachments),
                    },
                )
            new_id = tmpl.id
            new_name = tmpl.name
    except templates_service.TemplateNameAlreadyExists:
        # Имя могло «занять» параллельным оператором между шагом 1 и save.
        state.step = "new_awaiting_name"
        state.renew()
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=texts.OP_TMPL_NAME_TAKEN.format(name=state.pending_name),
            attachments=[keyboards.broadcast_template_cancel_keyboard()],
        )
        return
    except ValueError as exc:
        log.warning("broadcast_templates: create failed: %s", exc)
        _wizards.pop(actor_id, None)
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=f"Ошибка создания: {exc}",
            attachments=[keyboards.op_back_to_menu_keyboard()],
        )
        return
    _wizards.pop(actor_id, None)
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=texts.OP_TMPL_CREATED.format(name=new_name, number=new_id),
        attachments=[keyboards.broadcast_template_card_keyboard(new_id)],
    )


async def _back_to_text_new(event) -> None:
    """`op:tmpl:back_to_text_new` — превью → шаг ввода текста (правка)."""
    actor_id = get_user_id(event)
    if actor_id is None:
        return
    state = _wizards.get(actor_id)
    if state is None or state.step != "new_preview":
        return
    state.step = "new_awaiting_text"
    state.renew()
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=texts.OP_TMPL_NEW_TEXT_PROMPT.format(
            name=state.pending_name, limit=cfg.broadcast_max_chars
        ),
        attachments=[keyboards.broadcast_template_step2_keyboard()],
    )


# ---- edit wizard steps -----------------------------------------------

async def _step_edit(
    event,
    actor_id: int,
    state: _TmplWizardState,
    text: str,
    *,
    op_id: int | None,
) -> bool:
    """Шаг edit: принимаем текст+картинки, показываем превью.

    Сохранение перенесено в callback `op:tmpl:save_edit:<id>` после
    подтверждения превью — оператор видит «как увидит подписчик» до
    apply.
    """
    if not text:
        # Пустой текст. Но если оператор прислал ТОЛЬКО картинки без
        # подписи — это валидная замена вложений шаблона: текст
        # оставляем прежним. audit 2026-05-28: раньше image-only правка
        # была невозможна — `if not text` отбивал ввод до обработки
        # картинок ниже, и заменить вложения, не перенабрав текст,
        # оператор физически не мог.
        tmpl_before = None
        new_attachments: list = []
        if state.target_id is not None:
            async with session_scope() as session:
                max_images = await broadcast_handler._resolve_broadcast_max_images(
                    session
                )
                tmpl_before = await templates_service.get_by_id(
                    session, state.target_id
                )
            if tmpl_before is not None:
                new_attachments = list(
                    _image_attachments.image_attachments_from_event(
                        event, limit=max_images
                    )
                )
        if tmpl_before is not None and new_attachments:
            state.step = "edit_preview"
            state.pending_text = tmpl_before.text
            state.pending_attachments = new_attachments
            state.pending_name = tmpl_before.name
            state._edit_image_replaced = True  # type: ignore[attr-defined]
            state.renew()
            await _render_preview_edit(event, state)
            return True
        # Иначе — обычный пустой ввод: повторим prompt.
        name = tmpl_before.name if tmpl_before else "?"
        await event.message.answer(
            texts.OP_TMPL_EDIT_PROMPT.format(
                name=name, limit=cfg.broadcast_max_chars
            ),
            attachments=[keyboards.broadcast_template_cancel_keyboard()],
        )
        return True
    if len(text) > cfg.broadcast_max_chars:
        await event.message.answer(
            texts.OP_TMPL_TEXT_TOO_LONG.format(
                actual=len(text), limit=cfg.broadcast_max_chars
            ),
            attachments=[keyboards.broadcast_template_cancel_keyboard()],
        )
        return True
    if state.target_id is None:
        _wizards.pop(actor_id, None)
        return False

    async with session_scope() as session:
        max_images = await broadcast_handler._resolve_broadcast_max_images(
            session
        )
        tmpl_before = await templates_service.get_by_id(session, state.target_id)
    if tmpl_before is None:
        _wizards.pop(actor_id, None)
        await event.message.answer(
            texts.OP_TMPL_NOT_FOUND,
            attachments=[keyboards.op_back_to_menu_keyboard()],
        )
        return True
    new_attachments = _image_attachments.image_attachments_from_event(
        event, limit=max_images
    )
    # «приложили картинки → они полностью заменяют сохранённые»;
    # «не приложили → старые остаются». В превью показываем итоговое
    # содержимое — что реально уйдёт жителю.
    effective_atts = (
        list(new_attachments) if new_attachments
        else list(tmpl_before.attachments or [])
    )
    state.step = "edit_preview"
    state.pending_text = text
    state.pending_attachments = effective_atts
    state.pending_name = tmpl_before.name
    # Помним, заменены ли картинки — нужно для audit-details.
    state._edit_image_replaced = bool(new_attachments)  # type: ignore[attr-defined]
    state.renew()
    await _render_preview_edit(event, state)
    return True


async def _render_preview_edit(event, state: _TmplWizardState) -> None:
    images = _image_attachments.build_outbound_image_attachments(
        state.pending_attachments
    )
    await event.message.answer(
        texts.OP_TMPL_PREVIEW_HEADER_EDIT.format(
            name=state.pending_name,
            image_count=len(state.pending_attachments),
        ),
    )
    await event.message.answer(
        state.pending_text,
        attachments=[
            *images,
            keyboards.broadcast_template_preview_keyboard(state.target_id),
        ],
    )


async def _save_edit(event, template_id: int) -> None:
    """`op:tmpl:save_edit:<id>` — сохранить изменения после превью."""
    actor_id = get_user_id(event)
    if actor_id is None:
        return
    state = _wizards.get(actor_id)
    if (
        state is None
        or state.step != "edit_preview"
        or state.target_id != template_id
    ):
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=texts.OP_TMPL_CANCELLED,
            attachments=[keyboards.op_back_to_menu_keyboard()],
        )
        return
    op = await get_operator(event)
    op_id = op.id if op is not None else None
    replaced = getattr(state, "_edit_image_replaced", False)
    pass_atts: list | None = (
        state.pending_attachments if replaced else None
    )
    async with session_scope() as session:
        try:
            await templates_service.update_text(
                session,
                template_id,
                state.pending_text,
                attachments=pass_atts,
            )
        except templates_service.TemplateNotFound:
            _wizards.pop(actor_id, None)
            await send_or_edit_screen(
                event,
                chat_id=cfg.admin_group_id,
                text=texts.OP_TMPL_NOT_FOUND,
                attachments=[keyboards.op_back_to_menu_keyboard()],
            )
            return
        if op_id is not None:
            await operators_service.write_audit(
                session,
                operator_max_user_id=actor_id,
                action="broadcast_template_update",
                target=f"template #{template_id}",
                details={
                    "chars": len(state.pending_text),
                    "image_replaced": replaced,
                    "image_count": len(state.pending_attachments),
                },
            )
    name = state.pending_name
    image_count = len(state.pending_attachments)
    _wizards.pop(actor_id, None)
    if replaced:
        msg = texts.OP_TMPL_EDITED_WITH_IMAGES.format(
            name=name, image_count=image_count
        )
    else:
        msg = texts.OP_TMPL_EDITED_TEXT_ONLY.format(name=name)
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=msg,
        attachments=[keyboards.broadcast_template_card_keyboard(template_id)],
    )


async def _back_to_text_edit(event, template_id: int) -> None:
    """`op:tmpl:back_to_text_edit:<id>` — превью → шаг ввода текста."""
    actor_id = get_user_id(event)
    if actor_id is None:
        return
    state = _wizards.get(actor_id)
    if state is None or state.step != "edit_preview":
        return
    state.step = "edit_awaiting_text"
    state.renew()
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=texts.OP_TMPL_EDIT_PROMPT.format(
            name=state.pending_name, limit=cfg.broadcast_max_chars
        ),
        attachments=[keyboards.broadcast_template_cancel_keyboard()],
    )
