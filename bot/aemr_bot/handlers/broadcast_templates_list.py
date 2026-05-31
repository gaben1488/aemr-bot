"""UI шаблонов рассылок — чтение и применение (list / open / apply / search).

Декомпозиция god-объекта `broadcast_templates.py` (DDD tactical,
2026-06-01). Ответственность модуля — read-side операторских сценариев:

  - `_list` (`op:tmpl:list`) — карточка-список активных шаблонов;
  - `_open` (`op:tmpl:open:<id>`) — карточка одного шаблона с preview;
  - `_apply` (`op:tmpl:apply:<id>`) — пред-зарядить /broadcast wizard
    данными шаблона (double-tap dedupe + citation-footer);
  - `_start_search`/`_step_search` (`op:tmpl:search`) — поиск по пулу.

Wizard-state и dedupe импортируем из `broadcast_templates_state` по
ссылке — состояние единое на все группы. Точки входа (dispatch) и
re-export остаются в фасаде `broadcast_templates.py`.
"""

from __future__ import annotations

import logging

from aemr_bot import keyboards, texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.handlers import broadcast as broadcast_handler
from aemr_bot.handlers._auth import ensure_role
from aemr_bot.services import broadcast_templates as templates_service
from aemr_bot.services import broadcasts as broadcasts_service
from aemr_bot.utils import image_attachments as _image_attachments
from aemr_bot.utils.event import (
    get_user_id,
    send_or_edit_screen,
)

from aemr_bot.handlers.broadcast_templates_state import (
    _TmplWizardState,
    _drop_expired,
    _format_dt,
    _is_recent_apply,
    _mark_apply,
    _wizards,
)


log = logging.getLogger(__name__)


# ---- entry points: list / open / apply -------------------------------

async def _list(event) -> None:
    """`op:tmpl:list` — показать список активных шаблонов."""
    if not await ensure_role(event, OperatorRole.IT, OperatorRole.COORDINATOR):
        return
    async with session_scope() as session:
        items = await templates_service.list_active(session)
    if not items:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=texts.OP_TMPL_LIST_EMPTY,
            attachments=[
                keyboards.broadcast_templates_list_keyboard([], can_create=True)
            ],
        )
        return
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=texts.OP_TMPL_LIST_HEADER.format(count=len(items)),
        attachments=[
            keyboards.broadcast_templates_list_keyboard(items, can_create=True)
        ],
    )


async def _open(event, template_id: int) -> None:
    """`op:tmpl:open:<id>` — карточка шаблона."""
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
    if tmpl.use_count and tmpl.last_used_at:
        last_used_line = texts.OP_TMPL_CARD_LAST_USED.format(
            when=_format_dt(tmpl.last_used_at)
        )
    elif not tmpl.use_count:
        last_used_line = texts.OP_TMPL_CARD_NEVER_USED
    else:
        last_used_line = ""
    body = texts.OP_TMPL_CARD.format(
        number=tmpl.id,
        name=tmpl.name,
        created_at=_format_dt(tmpl.created_at),
        image_count=len(tmpl.attachments),
        char_count=len(tmpl.text),
        use_count=tmpl.use_count or 0,
        last_used_line=last_used_line,
        text=tmpl.text,
    )
    # Карточка показывает сохранённые картинки рядом с кнопками — оператор
    # видит, ЧТО именно уйдёт подписчикам, до нажатия «Отправить».
    preview_images = _image_attachments.build_outbound_image_attachments(
        tmpl.attachments
    )
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=body,
        attachments=[
            *preview_images,
            keyboards.broadcast_template_card_keyboard(tmpl.id),
        ],
    )


async def _apply(event, template_id: int) -> None:
    """`op:tmpl:apply:<id>` — пред-зарядить /broadcast wizard данными шаблона.

    P3 #25:
    - **double-tap dedupe**: повторный тап в 3-сек окне → ack без побочных
      эффектов (без record_usage, без перерисовки preview).
    - **citation clip footer**: в preview явно указываем имя шаблона —
      «Источник: шаблон «N»», чтобы оператор не отправил тот же текст
      из памяти, думая что это правка.
    """
    if not await ensure_role(event, OperatorRole.IT, OperatorRole.COORDINATOR):
        return
    actor_id = get_user_id(event)
    if actor_id is None:
        return
    if _is_recent_apply(actor_id, template_id):
        log.info(
            "broadcast_templates: dedup apply #%s by operator=%s "
            "(rapid double-tap window)", template_id, actor_id,
        )
        # Тихий ack, без побочных эффектов — оператор всё равно видит
        # ранее отправленный preview, дублировать нет смысла.
        from aemr_bot.utils.event import ack_callback as _ack

        await _ack(event)
        return
    _mark_apply(actor_id, template_id)
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
        subscribers = await broadcasts_service.count_subscribers(session)
        # PR template-editor-upgrade: фиксируем «применил для подготовки
        # рассылки». Инкрементируется на момент open в /broadcast wizard,
        # даже если оператор потом нажмёт ❌. Это адекватнее, чем
        # инкрементить «на отправке» — счётчик отражает «частоту
        # обращений к шаблону», а не отправок (для оценки гигиены
        # списка важна именно частота обращений).
        await templates_service.record_usage(session, template_id)
    if subscribers == 0:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=texts.OP_BROADCAST_NO_SUBSCRIBERS,
            attachments=[keyboards.op_back_to_menu_keyboard()],
        )
        return
    # Заряжаем broadcast wizard — выглядит, как если бы оператор набрал
    # этот текст и приложил картинки сейчас. Confirm/edit/abort работают
    # через тот же broadcast:* dispatcher, что и обычный flow.
    broadcast_handler.prefill_wizard_from_template(
        actor_id,
        text=tmpl.text,
        attachments=list(tmpl.attachments),
    )
    preview_images = _image_attachments.build_outbound_image_attachments(
        tmpl.attachments
    )
    citation_footer = (
        f"📋 Источник: шаблон «{tmpl.name}»\n"
        f"· · · · · · · ·\n"
    )
    body = citation_footer + texts.OP_BROADCAST_PREVIEW.format(
        text=tmpl.text,
        count=subscribers,
        image_count=len(tmpl.attachments),
        image_warning="",
    )
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=body,
        attachments=[
            *preview_images,
            keyboards.broadcast_confirm_keyboard(),
        ],
    )
    log.info(
        "broadcast_templates: applied #%s by operator=%s subscribers=%d",
        tmpl.id, actor_id, subscribers,
    )


# ---- search (PR template-editor-upgrade) -----------------------------


async def _start_search(event) -> None:
    """`op:tmpl:search` — ввод поискового запроса."""
    if not await ensure_role(event, OperatorRole.IT, OperatorRole.COORDINATOR):
        return
    actor_id = get_user_id(event)
    if actor_id is None:
        return
    _drop_expired()
    _wizards[actor_id] = _TmplWizardState(step="search_awaiting_query")
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=texts.OP_TMPL_SEARCH_PROMPT,
        attachments=[keyboards.broadcast_template_cancel_keyboard()],
    )


async def _step_search(
    event,
    actor_id: int,
    state: _TmplWizardState,
    text: str,
) -> bool:
    """Принять поисковый запрос, показать результаты."""
    if not text:
        await event.message.answer(
            texts.OP_TMPL_SEARCH_PROMPT,
            attachments=[keyboards.broadcast_template_cancel_keyboard()],
        )
        return True
    async with session_scope() as session:
        results = await templates_service.search(session, text)
    _wizards.pop(actor_id, None)
    if not results:
        await event.message.answer(
            texts.OP_TMPL_SEARCH_NOTHING_FOUND.format(query=text),
            attachments=[
                keyboards.broadcast_templates_search_results_keyboard([], text)
            ],
        )
        return True
    await event.message.answer(
        texts.OP_TMPL_SEARCH_RESULTS_HEADER.format(
            query=text, count=len(results)
        ),
        attachments=[
            keyboards.broadcast_templates_search_results_keyboard(
                results, text
            )
        ],
    )
    return True
