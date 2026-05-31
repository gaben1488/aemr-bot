"""UI шаблонов рассылок (PR H) — ФАСАД после декомпозиции god-объекта.

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

ДЕКОМПОЗИЦИЯ (DDD tactical, 2026-06-01). ~1232-строчный god-объект
разнесён по ответственности на соседние подмодули; этот файл — фасад:

  - `broadcast_templates_state`   — разделяемое ядро: wizard-state
    (`_wizards`, `_TmplWizardState`), apply-dedupe, мелкие helper'ы;
  - `broadcast_templates_list`    — чтение/применение: `_list`, `_open`,
    `_apply`, поиск (`_start_search`, `_step_search`);
  - `broadcast_templates_crud`    — delete/rename/clone;
  - `broadcast_templates_wizard`  — create/edit wizard с превью + back-nav
    + `_cancel`.

Точки входа `handle_callback` / `handle_wizard_text` (то, что зовёт
admin_callback_dispatch / menu.on_message) и публичные имена остаются
здесь. Все перенесённые функции re-export'ятся ниже, поэтому внешние
импорты `aemr_bot.handlers.broadcast_templates.X` и patch-точки тестов на
ДИСПЕТЧЕРНОМ уровне (`{module}._list`, `{module}._step_search`, ...)
продолжают работать без правок. Patch-точки на ВНУТРЕННИХ зависимостях
перенесённых функций (`templates_service`, `session_scope`, ...) теперь
живут в подмодуле, где функция определена.
"""

from __future__ import annotations

import logging

from aemr_bot.handlers import broadcast as broadcast_handler  # noqa: F401
from aemr_bot.utils.event import (
    ack_callback,
    get_user_id,
    is_admin_chat,
    send_or_edit_screen,  # noqa: F401
)
from aemr_bot.handlers._auth import get_operator

# ---- разделяемое ядро (state + dedupe + helpers) ---------------------
# Re-export'ятся только символы, которые читаются через фасад
# (`bt._wizards` / `bt._TmplWizardState` / dedupe-helpers в
# характеризационных тестах). `_drop_expired`/`_format_dt`/`WizardStep`/
# TTL-/window-константы подмодули берут напрямую из state — здесь не
# реэкспортируются (0 читателей `broadcast_templates.<symbol>`).
from aemr_bot.handlers.broadcast_templates_state import (  # noqa: F401
    _TmplWizardState,
    _apply_dedupe,
    _is_recent_apply,
    _mark_apply,
    _wizards,
)

# ---- чтение/применение (list / open / apply / search) ----------------
from aemr_bot.handlers.broadcast_templates_list import (  # noqa: F401
    _apply,
    _list,
    _open,
    _start_search,
    _step_search,
)

# ---- CRUD (delete / rename / clone) ----------------------------------
from aemr_bot.handlers.broadcast_templates_crud import (  # noqa: F401
    _ask_delete,
    _do_delete,
    _start_clone,
    _start_rename,
    _step_clone_name,
    _step_rename,
)

# ---- create/edit wizard + навигация + cancel -------------------------
from aemr_bot.handlers.broadcast_templates_wizard import (  # noqa: F401
    _back_to_name,
    _back_to_text_edit,
    _back_to_text_new,
    _cancel,
    _render_preview_edit,
    _render_preview_new,
    _save_edit,
    _save_new,
    _start_edit,
    _start_new,
    _step_edit,
    _step_new_name,
    _step_new_text,
)


log = logging.getLogger(__name__)


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
