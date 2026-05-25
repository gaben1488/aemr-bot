"""UI шаблонов рассылок (PR H).

Сценарии оператора в админ-чате через `/op_help → 📋 Шаблоны рассылок`:

  - **Список** (`op:tmpl:list`) — карточка с активными шаблонами и кнопкой
    «➕ Создать шаблон»;
  - **Карточка** (`op:tmpl:open:<id>`) — preview текста и кнопки «📨
    Отправить как рассылку», «✏️ Переименовать», «📝 Изменить текст»,
    «🗑 Удалить шаблон»;
  - **Применить** (`op:tmpl:apply:<id>`) — пред-заряжает мастер рассылок
    (handlers/broadcast.py:prefill_wizard_from_template) и показывает
    обычный confirm-preview;
  - **Создать** (`op:tmpl:new`) — двухшаговый wizard: имя → текст
    (с опциональными картинками);
  - **Переименовать** (`op:tmpl:rename:<id>`) — однострочный wizard;
  - **Изменить текст** (`op:tmpl:edit:<id>`) — однострочный wizard
    (новый текст + опциональные картинки полностью заменяют);
  - **Удалить** (`op:tmpl:delete:<id>` → `op:tmpl:delete_ok:<id>`) —
    soft-delete (archive).

Wizard state — in-memory dict как у `/broadcast`. Стартует только в
служебной группе и только под IT/COORDINATOR (как сама рассылка).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from aemr_bot import keyboards, texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.handlers import broadcast as broadcast_handler
from aemr_bot.handlers._auth import ensure_role, get_operator
from aemr_bot.services import broadcast_templates as templates_service
from aemr_bot.services import broadcasts as broadcasts_service
from aemr_bot.services import operators as operators_service
from aemr_bot.utils import image_attachments as _image_attachments
from aemr_bot.utils.event import (
    ack_callback,
    get_user_id,
    is_admin_chat,
    send_or_edit_screen,
)


log = logging.getLogger(__name__)


_WIZARD_TTL_SEC = 600  # 10 минут — те же лимиты, что у broadcast wizard


# ---- wizard state ----------------------------------------------------

WizardStep = Literal[
    "new_awaiting_name",
    "new_awaiting_text",
    "new_preview",
    "rename_awaiting_name",
    "edit_awaiting_text",
    "edit_preview",
    "clone_awaiting_name",
    "search_awaiting_query",
]


@dataclass
class _TmplWizardState:
    step: WizardStep
    # Имя нового шаблона (new flow) или нового имени клона (clone flow).
    pending_name: str = ""
    # Текст и вложения, накопленные на шаге 2 — используются для
    # превью и итогового сохранения.
    pending_text: str = ""
    pending_attachments: list = field(default_factory=list)
    # Для rename/edit/clone — id источника / редактируемого шаблона.
    target_id: int | None = None
    # Имя шаблона-источника при клонировании (для prompt'а).
    source_name: str = ""
    expires_at: float = field(
        default_factory=lambda: time.monotonic() + _WIZARD_TTL_SEC
    )

    def expired(self) -> bool:
        return time.monotonic() > self.expires_at

    def renew(self) -> None:
        self.expires_at = time.monotonic() + _WIZARD_TTL_SEC


# Один wizard на оператора (за один сценарий — один шаг ввода).
_wizards: dict[int, _TmplWizardState] = {}


def _drop_expired() -> None:
    stale = [uid for uid, st in _wizards.items() if st.expired()]
    for uid in stale:
        _wizards.pop(uid, None)


def _format_dt(dt) -> str:
    """ISO дату в локальный «дд.мм.гггг ЧЧ:ММ» Камчатского времени."""
    from zoneinfo import ZoneInfo

    if dt is None:
        return "—"
    return dt.astimezone(ZoneInfo(cfg.timezone)).strftime("%d.%m.%Y %H:%M")


# ---- entry points: list / open / new / apply / rename / edit / delete

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


# P3 #25 — double-tap dedupe для apply. MAX иногда задваивает callback
# (пользовательский tap и retry клиента). Без guard'а `record_usage`
# инкрементируется дважды, статистика «частоты обращений к шаблону»
# распухает. In-memory dict (actor_id, template_id) → monotonic-ts.
# Окно 3 сек: достаточно для защиты от двойного тапа, мало для блока
# легитимного «применил → отменил → применил снова».
_apply_dedupe: dict[tuple[int, int], float] = {}
_APPLY_DEDUPE_WINDOW_SEC = 3.0


def _is_recent_apply(actor_id: int, template_id: int) -> bool:
    import time as _time

    key = (actor_id, template_id)
    prev = _apply_dedupe.get(key)
    if prev is None:
        return False
    return _time.monotonic() - prev < _APPLY_DEDUPE_WINDOW_SEC


def _mark_apply(actor_id: int, template_id: int) -> None:
    import time as _time

    _apply_dedupe[(actor_id, template_id)] = _time.monotonic()
    # GC: чистим записи старше 5 окон, чтобы dict не разрастался.
    if len(_apply_dedupe) > 256:
        cutoff = _time.monotonic() - _APPLY_DEDUPE_WINDOW_SEC * 5
        for k in list(_apply_dedupe.keys()):
            if _apply_dedupe[k] < cutoff:
                _apply_dedupe.pop(k, None)


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
        f"────────────────\n"
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


# ---- callback dispatch (то, что вызывает admin_callback_dispatch) ----

async def handle_callback(event, payload: str) -> bool:
    """Точка входа для `op:tmpl:*`-callback'ов.

    Возвращает True, если payload распознан и обработан, False иначе —
    тогда caller продолжает обычный fallthrough.
    """
    if not is_admin_chat(event):
        return False
    # Strip prefix
    if not payload.startswith("op:tmpl:"):
        return False
    rest = payload[len("op:tmpl:"):]

    # Сначала exact-варианты без id
    if rest == "list":
        await ack_callback(event)
        await _list(event)
        return True
    if rest == "new":
        await ack_callback(event)
        await _start_new(event)
        return True
    if rest == "cancel":
        await ack_callback(event)
        await _cancel(event)
        return True
    if rest == "back_to_name":
        # Вернуть wizard на шаг 1 (имя). pending_name не сбрасываем —
        # покажем как старое в подсказке-примере, оператор может его
        # подправить или ввести заново.
        await ack_callback(event)
        await _back_to_name(event)
        return True
    # PR template-editor-upgrade: новые exact-варианты
    if rest == "search":
        await ack_callback(event)
        await _start_search(event)
        return True
    if rest == "save_new":
        await ack_callback(event)
        await _save_new(event)
        return True
    if rest == "back_to_text_new":
        await ack_callback(event)
        await _back_to_text_new(event)
        return True

    # verb:id
    if ":" in rest:
        verb, raw_id = rest.split(":", 1)
        try:
            tid = int(raw_id)
        except ValueError:
            return False
        if verb == "open":
            await ack_callback(event)
            await _open(event, tid)
            return True
        if verb == "apply":
            await ack_callback(event)
            await _apply(event, tid)
            return True
        if verb == "rename":
            await ack_callback(event)
            await _start_rename(event, tid)
            return True
        if verb == "edit":
            await ack_callback(event)
            await _start_edit(event, tid)
            return True
        if verb == "delete":
            await ack_callback(event)
            await _ask_delete(event, tid)
            return True
        if verb == "delete_ok":
            await ack_callback(event)
            await _do_delete(event, tid)
            return True
        # PR template-editor-upgrade: новые verb:id
        if verb == "clone":
            await ack_callback(event)
            await _start_clone(event, tid)
            return True
        if verb == "save_edit":
            await ack_callback(event)
            await _save_edit(event, tid)
            return True
        if verb == "back_to_text_edit":
            await ack_callback(event)
            await _back_to_text_edit(event, tid)
            return True
    return False


# ---- message handler (для wizard'а ввода name/text) ------------------

async def handle_wizard_text(event, text_body: str) -> bool:
    """Перехватывает ввод оператора, если активен wizard шаблонов.

    Возвращает True, если сообщение поглощено (обработано wizard'ом).
    Caller (handlers/menu.py:on_message) тогда не пропускает событие
    дальше.
    """
    if not is_admin_chat(event):
        return False
    actor_id = get_user_id(event)
    if actor_id is None:
        return False
    state = _wizards.get(actor_id)
    if state is None:
        return False
    if state.expired():
        _wizards.pop(actor_id, None)
        return False
    text = text_body.strip()

    if text.lower() == "/cancel":
        await _cancel(event)
        return True

    op = await get_operator(event)
    op_id = op.id if op is not None else None

    if state.step == "new_awaiting_name":
        return await _step_new_name(event, actor_id, state, text)
    if state.step == "new_awaiting_text":
        return await _step_new_text(event, actor_id, state, text, op_id=op_id)
    if state.step == "rename_awaiting_name":
        return await _step_rename(event, actor_id, state, text, op_id=op_id)
    if state.step == "edit_awaiting_text":
        return await _step_edit(event, actor_id, state, text, op_id=op_id)
    if state.step == "clone_awaiting_name":
        return await _step_clone_name(event, actor_id, state, text, op_id=op_id)
    if state.step == "search_awaiting_query":
        return await _step_search(event, actor_id, state, text)
    return False


async def _step_new_name(
    event, actor_id: int, state: _TmplWizardState, text: str
) -> bool:
    if not text:
        await event.message.answer(
            texts.OP_TMPL_NAME_EMPTY,
            attachments=[keyboards.broadcast_template_cancel_keyboard()],
        )
        return True
    if len(text) > templates_service.MAX_NAME_LEN:
        await event.message.answer(
            texts.OP_TMPL_NAME_TOO_LONG.format(
                actual=len(text), limit=templates_service.MAX_NAME_LEN
            ),
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


async def _step_rename(
    event,
    actor_id: int,
    state: _TmplWizardState,
    text: str,
    *,
    op_id: int | None,
) -> bool:
    if not text:
        await event.message.answer(
            texts.OP_TMPL_NAME_EMPTY,
            attachments=[keyboards.broadcast_template_cancel_keyboard()],
        )
        return True
    if len(text) > templates_service.MAX_NAME_LEN:
        await event.message.answer(
            texts.OP_TMPL_NAME_TOO_LONG.format(
                actual=len(text), limit=templates_service.MAX_NAME_LEN
            ),
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
        # пустой ввод — повторим prompt
        tmpl = None
        if state.target_id is not None:
            async with session_scope() as session:
                tmpl = await templates_service.get_by_id(session, state.target_id)
        name = tmpl.name if tmpl else "?"
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
    if not text:
        await event.message.answer(
            texts.OP_TMPL_NAME_EMPTY,
            attachments=[keyboards.broadcast_template_cancel_keyboard()],
        )
        return True
    if len(text) > templates_service.MAX_NAME_LEN:
        await event.message.answer(
            texts.OP_TMPL_NAME_TOO_LONG.format(
                actual=len(text), limit=templates_service.MAX_NAME_LEN
            ),
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


