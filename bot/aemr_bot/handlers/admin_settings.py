"""Иерархическое меню «⚙️ Настройки бота» — ФАСАД.

Покрывает все ключи settings_store через структурированную навигацию:
- Тексты для жителей (welcome_text, consent_text, appointment_text)
- Внешние ссылки (4 URL)
- Списки (topics, localities) — CRUD по строкам
- Объекты (emergency_contacts, transport_dispatcher_contacts) — CRUD
- Автор коммитов от бота
- Создание PR с изменениями (через services/repo_sync)

Команда /setting сохраняется как fallback для экспертных случаев
(длинные JSON-структуры, ключи без UI), но через меню больше не
требуется.

Inline-редактирование текстов и URL идёт через TTL-кэш ожидаемого
ввода (_edit_intents). Когда IT-админ присылает следующее текстовое
сообщение в админ-группе — оно перехватывается и применяется как
новое значение указанного ключа.

──────────────────────────────────────────────────────────────────────
ДЕКОМПОЗИЦИЯ (DDD tactical). Прикладной слой разнесён по
ответственности на подмодули рядом; здесь остаётся ФАСАД — точка
входа, диспетчер callback'ов, перехватчик текста и re-export
перенесённых функций (внешние импорты `from .admin_settings import X`
не ломаются):

- admin_settings_shared  — intent-кэш + `_clip_audit_value`/`_render_value`
- admin_settings_text    — карточки/правка текстов и URL (+ commit_author_*)
- admin_settings_author  — карточка автора коммитов
- admin_settings_list    — списки строк (topics, localities)
- admin_settings_obj     — списки объектов (emergency/transport контакты)
- admin_settings_quiet   — wizard тихого режима
- admin_settings_notify  — модульные тумблеры служебных уведомлений
- admin_settings_pr      — создание PR / diff с репозиторием

Диспетчер `_route_set_action` и перехватчик `handle_settings_edit_text`
зовут перенесённые функции через ЭТОТ фасадный namespace (re-export
ниже), поэтому характеризационные тесты, патчащие `mod.<handler>`,
продолжают работать без репойнта. Тесты прикладных функций патчат уже
на соответствующем подмодуле (см. их docstring).
"""
from __future__ import annotations

import json  # noqa: F401  # `_show_expert_key` рендерит значение через json.dumps
import logging

from aemr_bot import keyboards as kbds
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import ensure_role
from aemr_bot.services import settings_store
from aemr_bot.utils.event import ack_callback, get_user_id, send_or_edit_screen

# ── Общие примитивы (intent-кэш + helper'ы) — re-export из shared ──────
# `mod._edit_intents` / `mod._intent_set` / `from ...admin_settings import
# _clip_audit_value` в тестах продолжают резолвиться. Это ссылки на те же
# объекты, что использует shared-модуль (общий мутабельный dict).
# `_clip_audit_value` / `_render_value` re-export'ятся ради
# test_admin_settings_handlers / test_admin_settings_audit, которые
# импортируют их из этого фасада (вместе с `_AUDIT_VALUE_CLIP_LEN`).
from aemr_bot.handlers.admin_settings_shared import (  # noqa: F401
    _AUDIT_VALUE_CLIP_LEN,
    _clip_audit_value,
    _edit_intents,
    _intent_drop,
    _intent_get,
    _intent_set,
    _render_value,
)

# ── Прикладные обработчики — re-export из подмодулей ───────────────────
# Диспетчер и перехватчик ниже зовут их через эти фасадные имена.
from aemr_bot.handlers.admin_settings_author import _show_author_card  # noqa: F401
from aemr_bot.handlers.admin_settings_list import (  # noqa: F401
    _apply_list_add,
    _list_delete,
    _show_list_card,
)
from aemr_bot.handlers.admin_settings_obj import (  # noqa: F401
    _apply_obj_add,
    _obj_delete,
    _show_obj_card,
    _show_obj_item,
    _start_obj_add,
)
from aemr_bot.handlers.admin_settings_pr import (  # noqa: F401
    _create_pr,
    _show_pr_confirm,
    _show_pr_diff,
)
from aemr_bot.handlers.admin_settings_notify import (  # noqa: F401
    _show_notify_card,
    _toggle_notify,
)
from aemr_bot.handlers.admin_settings_quiet import (  # noqa: F401
    _apply_quiet_hour_edit,
    _show_quiet_card,
    _start_quiet_hour_intent,
    _toggle_quiet,
)
from aemr_bot.handlers.admin_settings_text import (  # noqa: F401
    _apply_single_edit,
    _show_text_card,
    _start_edit_intent,
)

log = logging.getLogger(__name__)


# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────
# Точка входа
# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────


async def run_settings_menu(event) -> None:
    """Главное меню «⚙️ Настройки бота» для роли it."""

    if not await ensure_role(event, OperatorRole.IT):
        return
    async with session_scope() as session:
        dirty = await settings_store.get_dirty_keys(session)
    dirty_count = len(dirty)
    extra = ""
    if dirty_count > 0:
        keys_preview = ", ".join(dirty[:5])
        if len(dirty) > 5:
            keys_preview += f" и ещё {len(dirty) - 5}"
        extra = (
            f"\n\n📌 Не выгружено в репо: {dirty_count}\n"
            f"({keys_preview})"
        )
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=(
            "⚙️ Настройки бота\n"
            "· · · · · · · ·\n"
            "Выберите категорию для редактирования.\n"
            "Каждое изменение применяется к боту сразу.\n"
            "Чтобы зафиксировать изменения в репозитории,\n"
            "создайте PR в нижней части меню."
            + extra
        ),
        attachments=[kbds.op_settings_menu_keyboard(dirty_count)],
    )


# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────
# Главный диспетчер callback'ов
# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────


async def run_settings_action(event, payload: str) -> None:

    if not await ensure_role(event, OperatorRole.IT):
        return
    operator_id = get_user_id(event)
    if operator_id is None:
        await ack_callback(event)
        return

    # Новые callback'и иерархического меню (префикс op:set:)
    if payload.startswith("op:set:"):
        await ack_callback(event)
        rest = payload.removeprefix("op:set:")
        await _route_set_action(event, operator_id, rest)
        return

    # Старый формат: op:setkey:<key> — экспертная карточка ключа
    if payload.startswith("op:setkey:"):
        await _show_expert_key(event, payload)
        return


async def _route_set_action(event, operator_id: int, rest: str) -> None:

    if rest == "expert":
        async with session_scope() as session:
            keys = await settings_store.list_keys(session)
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=(
                "⌨️ Все ключи (экспертный режим)\n"
                "· · · · · · · ·\n"
                "Здесь видны все ключи /setting, включая\n"
                "те, что обычно не редактируются через UI."
            ),
            attachments=[kbds.op_settings_expert_keyboard(keys)],
        )
        return

    if rest.startswith("cat:"):
        cat = rest.removeprefix("cat:")
        if cat == "texts":
            await send_or_edit_screen(
                event, chat_id=cfg.admin_group_id,
                text=(
                    "📢 Тексты для жителей\n"
                    "· · · · · · · ·\n"
                    "Что отредактировать?"
                ),
                attachments=[kbds.op_settings_texts_keyboard()],
            )
            return
        if cat == "urls":
            await send_or_edit_screen(
                event, chat_id=cfg.admin_group_id,
                text=(
                    "🔗 Внешние ссылки\n"
                    "· · · · · · · ·\n"
                    "Выберите ссылку для редактирования."
                ),
                attachments=[kbds.op_settings_urls_keyboard()],
            )
            return

    if rest.startswith("text:"):
        key = rest.removeprefix("text:")
        await _show_text_card(event, key)
        return
    if rest.startswith("url:"):
        key = rest.removeprefix("url:")
        await _show_text_card(event, key)
        return
    if rest.startswith("edit:"):
        key = rest.removeprefix("edit:")
        await _start_edit_intent(event, operator_id, key)
        return
    if rest.startswith("cancel:"):
        key = rest.removeprefix("cancel:")
        _intent_drop(operator_id)
        if key in {"commit_author_name", "commit_author_email"}:
            await _show_author_card(event)
            return
        await _show_text_card(event, key)
        return

    if rest.startswith("list:"):
        key = rest.removeprefix("list:")
        await _show_list_card(event, key)
        return
    if rest.startswith("list_add:"):
        key = rest.removeprefix("list_add:")
        _intent_set(operator_id, key=key, kind="list_add")
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=(
                f"➕ Добавление в «{key}»\n"
                f"· · · · · · · ·\n"
                f"Пришлите название одним сообщением."
            ),
            attachments=[kbds.op_settings_text_cancel_keyboard(key)],
        )
        return
    if rest.startswith("list_del:"):
        rest2 = rest.removeprefix("list_del:")
        await _list_delete(event, operator_id, rest2)
        return

    if rest.startswith("obj:"):
        key = rest.removeprefix("obj:")
        await _show_obj_card(event, key)
        return
    if rest.startswith("obj_view:"):
        rest2 = rest.removeprefix("obj_view:")
        await _show_obj_item(event, rest2)
        return
    if rest.startswith("obj_add:"):
        key = rest.removeprefix("obj_add:")
        await _start_obj_add(event, operator_id, key)
        return
    if rest.startswith("obj_del:"):
        rest2 = rest.removeprefix("obj_del:")
        await _obj_delete(event, operator_id, rest2)
        return

    if rest == "author":
        await _show_author_card(event)
        return

    if rest == "quiet":
        await _show_quiet_card(event)
        return
    if rest == "quiet:toggle":
        await _toggle_quiet(event)
        return
    if rest == "quiet:edit:start":
        await _start_quiet_hour_intent(event, operator_id, which="start")
        return
    if rest == "quiet:edit:end":
        await _start_quiet_hour_intent(event, operator_id, which="end")
        return

    if rest == "notify":
        await _show_notify_card(event)
        return
    if rest.startswith("notify:toggle:"):
        key = rest.removeprefix("notify:toggle:")
        await _toggle_notify(event, key)
        return

    if rest == "pr:start":
        await _show_pr_confirm(event)
        return
    if rest == "pr:confirm":
        await _create_pr(event, operator_id)
        return
    if rest == "pr:diff":
        await _show_pr_diff(event)
        return


# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────
# Старая экспертная карточка ключа (op:setkey:<key>)
# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────


async def _show_expert_key(event, payload: str) -> None:

    key = payload.removeprefix("op:setkey:")
    if not key:
        await ack_callback(event)
        return
    async with session_scope() as session:
        value = await settings_store.get(session, key)
    rendered = (
        json.dumps(value, ensure_ascii=False, indent=2) if value is not None else "—"
    )
    if len(rendered) > 1500:
        rendered = rendered[:1500] + "\n…(значение обрезано)"
    rule = settings_store.SCHEMA.get(key, {})
    expected = rule.get("type", "?")
    expected_name = expected.__name__ if hasattr(expected, "__name__") else str(expected)
    await ack_callback(event)
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=(
            f"⚙️ Настройка «{key}» (тип {expected_name})\n"
            f"· · · · · · · ·\n"
            f"Текущее значение:\n{rendered}\n"
            f"· · · · · · · ·\n"
            f"Изменить: /setting {key} <новое значение>\n"
            f"Для списков и объектов передавайте JSON."
        ),
        attachments=[kbds.op_back_to_settings_keyboard()],
    )


# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────
# Перехватчик текстовых сообщений для редактирования
# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────


async def handle_settings_edit_text(event, text: str) -> bool:
    """Перехватчик текстовых сообщений в админ-группе для редактирования
    настроек. Возвращает True, если сообщение поглощено."""

    operator_id = get_user_id(event)
    if operator_id is None:
        return False
    intent = _intent_get(operator_id)
    if intent is None:
        return False

    if not await ensure_role(event, OperatorRole.IT):
        _intent_drop(operator_id)
        return False

    key = intent["key"]
    kind = intent["kind"]
    new_text = text.strip()

    # Intent снимаем ТОЛЬКО когда `_apply_*` вернул True (значение
    # применено). На False ввод отклонён валидатором — apply уже показал
    # ошибку + cancel-клавиатуру; intent сохраняется, чтобы следующее
    # сообщение оператора было перехвачено как повторная попытка (иначе
    # «🌙 Час начала» → `99` → ошибка → `18` уходил бы в пустоту). В обоих
    # случаях сообщение поглощено (return True) — оно адресовано wizard'у.
    if kind == "single":
        if await _apply_single_edit(event, operator_id, key, new_text):
            _intent_drop(operator_id)
        return True
    if kind == "list_add":
        if await _apply_list_add(event, operator_id, key, new_text):
            _intent_drop(operator_id)
        return True
    if kind == "obj_add":
        if await _apply_obj_add(event, operator_id, key, new_text):
            _intent_drop(operator_id)
        return True
    if kind == "quiet_hour":
        which = intent.get("which", "start")
        if await _apply_quiet_hour_edit(event, operator_id, which, new_text):
            _intent_drop(operator_id)
        return True
    return False
