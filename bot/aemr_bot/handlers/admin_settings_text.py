"""Карточки и редактирование текстовых/URL-ключей настроек.

Выделено из god-объекта `admin_settings.py` (DDD tactical: связная
ответственность «тексты и ссылки»). Тексты для жителей (welcome_text,
consent_text, appointment_text) и внешние ссылки (4 URL) делят один
паттерн «карточка → правка через intent → применение с audit-trail».

Сюда же `_apply_single_edit`: применяет введённое значение
текстового/URL-ключа (и заодно автора коммитов commit_author_*) с
полным before→after в audit_log.

Точка входа/диспетчер (`run_settings_action`, `_route_set_action`,
`handle_settings_edit_text`) и intent-кэш остаются в фасаде
`admin_settings.py`; он re-export'ит имена отсюда. Перехватчик текста
зовёт `_apply_single_edit` через фасадный namespace.
"""
from __future__ import annotations

from aemr_bot import keyboards as kbds
from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import operators as ops_svc
from aemr_bot.services import settings_store
from aemr_bot.utils.event import send_or_edit_screen

# Intent-кэш и общие helper'ы живут в admin_settings_shared —
# импортируем как ссылки на те же объекты (dict `_edit_intents`
# мутабелен и общий, функции — те же объекты). Это позволяет тестам
# `TestStartEditIntent` патчить/читать их «по месту» (на этом
# подмодуле) без расхождения с фасадом и shared. `_edit_intents` /
# `_intent_get` реэкспортируются ради этих тестов (использует их само
# приложение через `_intent_set`).
from aemr_bot.handlers.admin_settings_shared import (  # noqa: F401
    _clip_audit_value,
    _edit_intents,
    _intent_get,
    _intent_set,
    _render_value,
)
# `_show_author_card` нужен `_apply_single_edit` для commit_author_*;
# импортируем из соседнего подмодуля — это namespace, который патчат
# тесты `TestApplySingleEdit` (alias mod → admin_settings_text).
from aemr_bot.handlers.admin_settings_author import _show_author_card


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
            f"· · · · · · · ·\n"
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
            f"· · · · · · · ·"
            f"{hint}"
        ),
        attachments=[kbds.op_settings_text_cancel_keyboard(key)],
    )


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
