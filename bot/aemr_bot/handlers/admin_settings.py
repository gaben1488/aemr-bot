"""Иерархическое меню «⚙️ Настройки бота».

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
"""
from __future__ import annotations

import json
import logging
import os
import time as _time
from typing import Any

from aemr_bot import keyboards as kbds
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import ensure_role
from aemr_bot.services import operators as ops_svc
from aemr_bot.services import settings_store
from aemr_bot.utils.event import ack_callback, get_user_id, send_or_edit_screen

log = logging.getLogger(__name__)

# Intent: «следующее текстовое сообщение этого оператора — новое
# значение для ключа». TTL 5 минут (см. _EDIT_INTENT_TTL_SEC).
# operator_max_user_id -> {"key": str, "kind": str, "expires_at": float, "extra": dict}
_edit_intents: dict[int, dict] = {}
_EDIT_INTENT_TTL_SEC = 300.0

# Audit-trail: длинные значения настроек (welcome_text, goodbye_message,
# consent_text) могут быть до нескольких тысяч символов. Полный
# `before`/`after` в каждой записи audit_log раздул бы таблицу. Лимит
# 200 симв — достаточно, чтобы видеть «что поменялось» при расследовании
# инцидента, не теряя сути правки.
_AUDIT_VALUE_CLIP_LEN = 200


def _clip_audit_value(value: object) -> str:
    """Подготовить значение настройки к записи в audit_log.details.

    Списки/dict сериализуем через repr (компактнее json для коротких
    структур и не требует encoding-кода). Усечение через многоточие,
    чтобы было видно, что значение было длиннее.
    """
    if value is None:
        text = "—"
    elif isinstance(value, str):
        text = value
    else:
        text = repr(value)
    if len(text) > _AUDIT_VALUE_CLIP_LEN:
        return text[: _AUDIT_VALUE_CLIP_LEN - 1] + "…"
    return text


def _intent_set(operator_id: int, **kwargs) -> None:
    state = dict(kwargs)
    state["expires_at"] = _time.monotonic() + _EDIT_INTENT_TTL_SEC
    _edit_intents[operator_id] = state
    # Reliability-pass: opportunistic GC. _intent_get чистит только
    # тот ключ, который пришёл в get. Если оператор настроил intent
    # и не дёрнул его (закрыл клиент), запись висит вечно (то же на
    # каждом ребуте session-mid'ов). Раз в set'е (~10/день per
    # admin) — лёгкий проход с удалением истёкших. O(N) по числу
    # операторов — единицы записей; не hot path.
    if len(_edit_intents) > 16:
        now = _time.monotonic()
        for k in [k for k, v in _edit_intents.items() if v.get("expires_at", 0) < now]:
            _edit_intents.pop(k, None)


def _intent_get(operator_id: int) -> dict | None:
    state = _edit_intents.get(operator_id)
    if state is None:
        return None
    if _time.monotonic() > state.get("expires_at", 0):
        _edit_intents.pop(operator_id, None)
        return None
    return state


def _intent_drop(operator_id: int) -> None:
    _edit_intents.pop(operator_id, None)


def _render_value(value: Any, *, limit: int = 1500) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "\n…(обрезано)"
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    return rendered if len(rendered) <= limit else rendered[:limit] + "\n…(обрезано)"


# ──────────────────────────────────────────────────────────────────────
# Точка входа
# ──────────────────────────────────────────────────────────────────────


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
            "──────────\n"
            "Выберите категорию для редактирования.\n"
            "Каждое изменение применяется к боту сразу.\n"
            "Чтобы зафиксировать изменения в репозитории,\n"
            "создайте PR в нижней части меню."
            + extra
        ),
        attachments=[kbds.op_settings_menu_keyboard(dirty_count)],
    )


# ──────────────────────────────────────────────────────────────────────
# Главный диспетчер callback'ов
# ──────────────────────────────────────────────────────────────────────


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
                "──────────\n"
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
                    "──────────\n"
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
                    "──────────\n"
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
                f"──────────\n"
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

    if rest == "pr:start":
        await _show_pr_confirm(event)
        return
    if rest == "pr:confirm":
        await _create_pr(event, operator_id)
        return
    if rest == "pr:diff":
        await _show_pr_diff(event)
        return


# ──────────────────────────────────────────────────────────────────────
# Тексты и URL: одинаковый паттерн card + edit
# ──────────────────────────────────────────────────────────────────────


async def _show_text_card(event, key: str) -> None:

    async with session_scope() as session:
        value = await settings_store.get(session, key)
    rule = settings_store.SCHEMA.get(key, {})
    expected = rule.get("type", str)
    type_label = expected.__name__ if hasattr(expected, "__name__") else str(expected)
    is_url = bool(rule.get("url"))
    title_map = {
        "welcome_text": "👋 Приветствие",
        "consent_text": "🔐 Текст согласия на ПДн",
        "appointment_text": "🏛 Расписание приёма граждан",
        "electronic_reception_url": "🌐 Электронная приёмная",
        "policy_url": "📄 Политика ПДн (ссылка)",
        "udth_schedule_url": "🚌 Пригородные автобусы (УДТХ)",
        "udth_schedule_intermunicipal_url": "🚍 Межмуниципальные маршруты",
    }
    title = title_map.get(key, key)
    constraints = ""
    if expected is str:
        if "max_len" in rule:
            constraints = f"\nЛимит: до {rule['max_len']} символов."
        if is_url:
            constraints += "\nДолжно начинаться с http:// или https://"
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            f"{title} ({type_label})\n"
            f"──────────\n"
            f"Текущее значение:\n{_render_value(value)}"
            f"{constraints}"
        ),
        attachments=[kbds.op_settings_text_actions_keyboard(key)],
    )


async def _start_edit_intent(event, operator_id: int, key: str) -> None:

    if key not in settings_store.SCHEMA:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=f"Ключ «{key}» нельзя править из меню.",
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return
    _intent_set(operator_id, key=key, kind="single")
    rule = settings_store.SCHEMA[key]
    hint = ""
    if rule.get("url"):
        hint = "\n\nПришлите новый URL (http:// или https://)."
    elif rule.get("type") is str:
        max_len = rule.get("max_len", "?")
        hint = f"\n\nПришлите новый текст одним сообщением (до {max_len} симв)."
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            f"✏️ Редактирование «{key}»\n"
            f"──────────"
            f"{hint}"
        ),
        attachments=[kbds.op_settings_text_cancel_keyboard(key)],
    )


# ──────────────────────────────────────────────────────────────────────
# Списки строк (topics, localities)
# ──────────────────────────────────────────────────────────────────────


async def _show_list_card(event, key: str) -> None:

    async with session_scope() as session:
        items = await settings_store.get(session, key) or []
    if not isinstance(items, list):
        items = []
    title_map = {
        "topics": "🏷 Тематики обращений",
        "localities": "📍 Населённые пункты",
    }
    title = title_map.get(key, key)
    if items:
        body = "\n".join(f"{i+1}. {x}" for i, x in enumerate(items))
    else:
        body = "(список пуст)"
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            f"{title} ({len(items)})\n"
            f"──────────\n"
            f"{body}\n"
            f"──────────\n"
            f"Тап «🗑 N» — удалить запись.\n"
            f"Тап «➕ Добавить» — добавить новую."
        ),
        attachments=[kbds.op_settings_list_keyboard(key, items)],
    )


async def _list_delete(event, operator_id: int, suffix: str) -> None:

    parts = suffix.split(":", 1)
    if len(parts) != 2:
        return
    key, idx_str = parts[0], parts[1]
    try:
        idx = int(idx_str)
    except ValueError:
        return
    async with session_scope() as session:
        items = await settings_store.get(session, key) or []
        if not isinstance(items, list) or idx < 0 or idx >= len(items):
            await send_or_edit_screen(
                event, chat_id=cfg.admin_group_id,
                text="Элемент не найден.",
                attachments=[kbds.op_back_to_settings_keyboard()],
            )
            return
        removed = items.pop(idx)
        ok, msg = settings_store.validate(key, items)
        if not ok:
            await send_or_edit_screen(
                event, chat_id=cfg.admin_group_id,
                text=f"Удаление отменено: {msg}",
                attachments=[kbds.op_back_to_settings_keyboard()],
            )
            return
        await settings_store.set_value(session, key, items)
        await ops_svc.write_audit(
            session,
            operator_max_user_id=operator_id,
            action="setting_list_del",
            target=key,
            details={"removed": removed, "index": idx},
        )
    await _show_list_card(event, key)


# ──────────────────────────────────────────────────────────────────────
# Списки объектов (emergency_contacts, transport_dispatcher_contacts)
# ──────────────────────────────────────────────────────────────────────


async def _show_obj_card(event, key: str) -> None:

    async with session_scope() as session:
        items = await settings_store.get(session, key) or []
    if not isinstance(items, list):
        items = []
    title_map = {
        "emergency_contacts": "🆘 Экстренные службы",
        "transport_dispatcher_contacts": "🚌 Диспетчерские транспорта",
    }
    title = title_map.get(key, key)
    # Pure-функция в services/settings_store — там же и юнит-тесты
    # без зависимости от maxapi.
    body = settings_store.format_obj_list(items)
    hint = ""
    if key == "emergency_contacts":
        hint = (
            "\n\nФормат добавления: пришлите две или три строки —\n"
            "название, телефон и (опционально) раздел.\n"
            "Пример с разделом:\n"
            "Пожарная служба\n"
            "01\n"
            "Экстренные службы"
        )
    elif key == "transport_dispatcher_contacts":
        hint = (
            "\n\nФормат добавления: пришлите две строки —\n"
            "маршруты и телефон.\n"
            "Пример:\n"
            "Автобусы 101, 102, 103\n"
            "+7 (415-31) 7-25-29"
        )
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            f"{title} ({len(items)})\n"
            f"──────────\n"
            f"{body}"
            f"{hint}"
        ),
        attachments=[kbds.op_settings_obj_keyboard(key, items)],
    )


async def _show_obj_item(event, suffix: str) -> None:

    parts = suffix.split(":", 1)
    if len(parts) != 2:
        return
    key, idx_str = parts[0], parts[1]
    try:
        idx = int(idx_str)
    except ValueError:
        return
    async with session_scope() as session:
        items = await settings_store.get(session, key) or []
    if idx < 0 or idx >= len(items):
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="Запись не найдена.",
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return
    item = items[idx]
    lines = [f"{k}: {v}" for k, v in item.items()]
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text="\n".join(lines),
        attachments=[kbds.op_settings_obj_item_keyboard(key, idx)],
    )


async def _start_obj_add(event, operator_id: int, key: str) -> None:

    _intent_set(operator_id, key=key, kind="obj_add")
    if key == "emergency_contacts":
        hint = (
            "Пришлите две или три строки:\n"
            "1) название\n"
            "2) телефон\n"
            "3) раздел (необязательно — Экстренные службы / Электроэнергия / ...)"
        )
    elif key == "transport_dispatcher_contacts":
        hint = "Пришлите две строки: маршруты и телефон."
    else:
        hint = "Пришлите данные двумя строками."
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            f"➕ Добавление в «{key}»\n"
            f"──────────\n"
            f"{hint}"
        ),
        attachments=[kbds.op_settings_text_cancel_keyboard(key)],
    )


async def _obj_delete(event, operator_id: int, suffix: str) -> None:

    parts = suffix.split(":", 1)
    if len(parts) != 2:
        return
    key, idx_str = parts[0], parts[1]
    try:
        idx = int(idx_str)
    except ValueError:
        return
    async with session_scope() as session:
        items = await settings_store.get(session, key) or []
        if not isinstance(items, list) or idx < 0 or idx >= len(items):
            await send_or_edit_screen(
                event, chat_id=cfg.admin_group_id,
                text="Запись не найдена.",
                attachments=[kbds.op_back_to_settings_keyboard()],
            )
            return
        removed = items.pop(idx)
        ok, msg = settings_store.validate(key, items)
        if not ok:
            await send_or_edit_screen(
                event, chat_id=cfg.admin_group_id,
                text=f"Удаление отменено: {msg}",
                attachments=[kbds.op_back_to_settings_keyboard()],
            )
            return
        await settings_store.set_value(session, key, items)
        await ops_svc.write_audit(
            session,
            operator_max_user_id=operator_id,
            action="setting_obj_del",
            target=key,
            details={"removed": removed, "index": idx},
        )
    await _show_obj_card(event, key)


# ──────────────────────────────────────────────────────────────────────
# Автор коммитов
# ──────────────────────────────────────────────────────────────────────


async def _show_author_card(event) -> None:

    async with session_scope() as session:
        name = await settings_store.get(session, "commit_author_name")
        email = await settings_store.get(session, "commit_author_email")
    name_line = name or "(не задано)"
    email_line = email or "(не задано)"
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            "👤 Автор коммитов от бота\n"
            "──────────\n"
            f"ФИО:   {name_line}\n"
            f"Email: {email_line}\n\n"
            "Это значения подставляются в коммиты,\n"
            "которые бот создаёт при синхронизации\n"
            "настроек с репозиторием."
        ),
        attachments=[kbds.op_settings_author_keyboard()],
    )


# ──────────────────────────────────────────────────────────────────────
# Pull Request
# ──────────────────────────────────────────────────────────────────────


async def _show_pr_confirm(event) -> None:

    async with session_scope() as session:
        dirty = await settings_store.get_dirty_keys(session)
        name = await settings_store.get(session, "commit_author_name")
        email = await settings_store.get(session, "commit_author_email")

    pat_present = bool(os.environ.get("GITHUB_PAT", "").strip())
    if not dirty:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=(
                "📥 Нет несинхронизированных изменений.\n"
                "──────────\n"
                "Все настройки совпадают с последним PR в репо."
            ),
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return
    blockers: list[str] = []
    if not pat_present:
        blockers.append("• GITHUB_PAT не задан в .env (см. infra/.env.example)")
    if not name:
        blockers.append("• Не задан автор коммитов — раздел «👤 Автор»")
    if not email:
        blockers.append("• Не задан email автора — раздел «👤 Автор»")

    keys_preview = "\n".join(f"• {k}" for k in dirty[:10])
    if len(dirty) > 10:
        keys_preview += f"\n…и ещё {len(dirty) - 10}"

    if blockers:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=(
                "💾 Создать PR с изменениями\n"
                "──────────\n"
                f"Будет включено {len(dirty)} ключей:\n{keys_preview}\n\n"
                "❌ Нельзя создать PR:\n" + "\n".join(blockers) +
                "\n\nИзменения уже применены в боте — это\n"
                "только про их фиксацию в репозитории."
            ),
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return

    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            "💾 Создать PR с изменениями\n"
            "──────────\n"
            f"Будет включено {len(dirty)} ключей:\n{keys_preview}\n\n"
            f"Автор: {name} <{email}>\n\n"
            "После создания PR откройте его в браузере,\n"
            "проверьте diff и нажмите Merge. Auto-deploy\n"
            "подхватит изменения в течение 10 минут."
        ),
        attachments=[kbds.op_settings_pr_confirm_keyboard()],
    )


async def _create_pr(event, operator_id: int) -> None:
    from aemr_bot.services import repo_sync

    async with session_scope() as session:
        dirty = await settings_store.get_dirty_keys(session)
        runtime_config = await settings_store.export_synced(session)
        name = await settings_store.get(session, "commit_author_name")
        email = await settings_store.get(session, "commit_author_email")
        op_record = await ops_svc.get(session, operator_id)
    operator_name = op_record.full_name if op_record else f"id={operator_id}"

    cfg_repo = repo_sync.load_config_from_env_and_settings(
        author_name=name, author_email=email,
    )
    if cfg_repo is None:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=(
                "❌ Не настроено GitHub-подключение.\n"
                "──────────\n"
                "Заполните GITHUB_PAT в .env и/или\n"
                "автора коммитов в меню «👤 Автор»."
            ),
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return
    if not dirty:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="Нет несинхронизированных изменений.",
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return

    result = await repo_sync.create_settings_pr(
        cfg_repo,
        runtime_config=runtime_config,
        dirty_keys=dirty,
        operator_name=operator_name,
        operator_id=operator_id,
    )
    if not result.ok:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=(
                "❌ Не удалось создать PR.\n"
                "──────────\n"
                f"Причина: {result.reason}\n"
                f"{result.message}"
            ),
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return

    async with session_scope() as session:
        await settings_store.mark_synced(session, dirty)
        await ops_svc.write_audit(
            session,
            operator_max_user_id=operator_id,
            action="settings_pr_created",
            target=cfg_repo.repo,
            details={
                "pr_number": result.pr_number,
                "pr_url": result.pr_url,
                "branch": result.branch,
                "keys": dirty,
            },
        )
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            f"✅ PR создан: #{result.pr_number}\n"
            f"──────────\n"
            f"Ветка: {result.branch}\n"
            f"Изменено ключей: {len(dirty)}\n\n"
            f"Откройте PR в браузере, проверьте diff\n"
            f"и нажмите Merge.\n\n"
            f"Auto-deploy подхватит изменения в течение\n"
            f"10 минут после мержа."
        ),
        attachments=[kbds.op_settings_pr_done_keyboard(result.pr_url)],
    )


async def _show_pr_diff(event) -> None:
    from aemr_bot.services import repo_sync

    async with session_scope() as session:
        dirty = await settings_store.get_dirty_keys(session)
        local = await settings_store.export_synced(session)
        name = await settings_store.get(session, "commit_author_name")
        email = await settings_store.get(session, "commit_author_email")

    if not os.environ.get("GITHUB_PAT", "").strip():
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=(
                "📥 Проверка расхождений с репо\n"
                "──────────\n"
                "GITHUB_PAT не задан в .env.\n\n"
                f"Локально dirty-ключей: {len(dirty)}\n"
                + ("\n".join(f"• {k}" for k in dirty[:10]) if dirty else "—")
            ),
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return
    cfg_repo = repo_sync.load_config_from_env_and_settings(
        author_name=name or "bot", author_email=email or "bot@example.com",
    )
    if cfg_repo is None:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="❌ Не настроено GitHub-подключение.",
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return
    remote, reason = await repo_sync.fetch_main_runtime_config(cfg_repo)
    if remote is None and reason == "not_in_repo":
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=(
                "📥 Проверка расхождений с репо\n"
                "──────────\n"
                "Файла seed/runtime_config.json в main\n"
                "пока нет. Первый PR создаст его."
            ),
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return
    if remote is None:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=f"❌ Не удалось скачать из репо: {reason}",
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return

    diffs: list[str] = []
    for key in settings_store.SYNCED_KEYS:
        local_val = local.get(key)
        remote_val = remote.get(key)
        if local_val != remote_val:
            diffs.append(key)
    if not diffs:
        body = "✅ Локально и в репо всё одинаково."
    else:
        body = (
            f"⚠️ Различаются {len(diffs)} ключей:\n"
            + "\n".join(f"• {k}" for k in diffs)
            + "\n\nЕсли локальные изменения новее — создайте PR.\n"
            + "Если в репо есть изменения, которых нет\n"
            + "локально (например, через ручной PR) —\n"
            + "перезапустите бота, он перечитает seed."
        )
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            "📥 Проверка расхождений с репо\n"
            "──────────\n"
            + body
        ),
        attachments=[kbds.op_back_to_settings_keyboard()],
    )


# ──────────────────────────────────────────────────────────────────────
# Старая экспертная карточка ключа (op:setkey:<key>)
# ──────────────────────────────────────────────────────────────────────


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
            f"──────────\n"
            f"Текущее значение:\n{rendered}\n"
            f"──────────\n"
            f"Изменить: /setting {key} <новое значение>\n"
            f"Для списков и объектов передавайте JSON."
        ),
        attachments=[kbds.op_back_to_settings_keyboard()],
    )


# ──────────────────────────────────────────────────────────────────────
# Перехватчик текстовых сообщений для редактирования
# ──────────────────────────────────────────────────────────────────────


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

    if kind == "single":
        await _apply_single_edit(event, operator_id, key, new_text)
        _intent_drop(operator_id)
        return True
    if kind == "list_add":
        await _apply_list_add(event, operator_id, key, new_text)
        _intent_drop(operator_id)
        return True
    if kind == "obj_add":
        await _apply_obj_add(event, operator_id, key, new_text)
        _intent_drop(operator_id)
        return True
    return False


async def _apply_single_edit(
    event, operator_id: int, key: str, new_text: str
) -> None:

    ok, msg = settings_store.validate(key, new_text)
    if not ok:
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=f"❌ {msg}",
            attachments=[kbds.op_settings_text_cancel_keyboard(key)],
        )
        return
    async with session_scope() as session:
        old_value = await settings_store.get(session, key)
        await settings_store.set_value(session, key, new_text)
        # Полный audit-trail: храним «было → стало» (clip до 200 симв,
        # чтобы не раздувать audit_log на длинных текстах вроде
        # `goodbye_message`). PII под защитой retention (по умолчанию
        # 365 дней, потом `_job_audit_log_retention` чистит).
        await ops_svc.write_audit(
            session,
            operator_max_user_id=operator_id,
            action="setting_update",
            target=key,
            details={
                "len": len(new_text),
                "before": _clip_audit_value(old_value),
                "after": _clip_audit_value(new_text),
            },
        )
    if key in {"commit_author_name", "commit_author_email"}:
        await _show_author_card(event)
    else:
        await _show_text_card(event, key)


async def _apply_list_add(
    event, operator_id: int, key: str, new_text: str
) -> None:

    if len(new_text) < 1:
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text="❌ Пустая строка.",
            attachments=[kbds.op_settings_text_cancel_keyboard(key)],
        )
        return
    async with session_scope() as session:
        items = await settings_store.get(session, key) or []
        if not isinstance(items, list):
            items = []
        if new_text in items:
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text="❌ Такая запись уже есть.",
                attachments=[kbds.op_settings_text_cancel_keyboard(key)],
            )
            return
        items.append(new_text)
        ok, msg = settings_store.validate(key, items)
        if not ok:
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text=f"❌ {msg}",
                attachments=[kbds.op_settings_text_cancel_keyboard(key)],
            )
            return
        await settings_store.set_value(session, key, items)
        await ops_svc.write_audit(
            session,
            operator_max_user_id=operator_id,
            action="setting_list_add",
            target=key,
            details={"added": new_text},
        )
    await _show_list_card(event, key)


async def _apply_obj_add(
    event, operator_id: int, key: str, new_text: str
) -> None:

    lines = [ln.strip() for ln in new_text.split("\n") if ln.strip()]
    if len(lines) < 2:
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text="❌ Нужно две строки (название/маршруты и телефон).",
            attachments=[kbds.op_settings_text_cancel_keyboard(key)],
        )
        return
    if key == "emergency_contacts":
        # Третья строка — необязательный раздел (Экстренные службы,
        # Электроэнергия, Отопление и т.п.). Если оператор её прислал,
        # сохраняем — UI потом сгруппирует контакты по разделам. Если
        # не прислал — item уходит в визуальную секцию «Прочее».
        item: dict[str, str] = {"name": lines[0], "phone": lines[1]}
        if len(lines) >= 3 and lines[2]:
            item["section"] = lines[2]
    elif key == "transport_dispatcher_contacts":
        item = {"routes": lines[0], "phone": lines[1]}
    else:
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=f"❌ Ключ «{key}» не поддерживает добавление через две строки.",
            attachments=[kbds.op_settings_text_cancel_keyboard(key)],
        )
        return
    async with session_scope() as session:
        items = await settings_store.get(session, key) or []
        if not isinstance(items, list):
            items = []
        items.append(item)
        ok, msg = settings_store.validate(key, items)
        if not ok:
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text=f"❌ {msg}",
                attachments=[kbds.op_settings_text_cancel_keyboard(key)],
            )
            return
        await settings_store.set_value(session, key, items)
        await ops_svc.write_audit(
            session,
            operator_max_user_id=operator_id,
            action="setting_obj_add",
            target=key,
            details={"added": item},
        )
    await _show_obj_card(event, key)
