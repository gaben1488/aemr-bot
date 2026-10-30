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
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._common import op_screen, op_send

# `_show_author_card` нужен `_apply_single_edit` для commit_author_*;
# импортируем из соседнего подмодуля — это namespace, который патчат
# тесты `TestApplySingleEdit` (alias mod → admin_settings_text).
from aemr_bot.handlers.admin_settings_author import _show_author_card

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
from aemr_bot.services import operators as ops_svc
from aemr_bot.services import settings_store
from aemr_bot.utils.text_pages import split_text_units

_TITLE_MAP = {
    "welcome_text": "👋 Приветствие",
    "consent_text": "🔐 Согласие на обработку данных",
    "appointment_text": "🏛 Расписание приёма граждан",
    "electronic_reception_url": "🌐 Электронная приёмная",
    "policy_url": "📄 Политика администрации",
    "udth_schedule_url": "🚌 Пригородные автобусы",
    "udth_schedule_intermunicipal_url": "🚍 Межмуниципальные маршруты",
}


async def _show_text_card(event, key: str) -> None:

    async with session_scope() as session:
        value = await settings_store.get(session, key)
    rule = settings_store.SCHEMA.get(key, {})
    expected = rule.get("type", str)
    type_label = expected.__name__ if hasattr(expected, "__name__") else str(expected)
    is_url = bool(rule.get("url"))
    title = _TITLE_MAP.get(key, key)
    constraints = ""
    if expected is str:
        if "max_len" in rule:
            constraints = f"\nЛимит: до {rule['max_len']} символов."
        if is_url:
            constraints += "\nДолжно начинаться с http:// или https://"
    await op_screen(
        event,
        f"{title} ({type_label})\n"
        f"· · · · · · · ·\n"
        f"Текущее значение:\n{_render_value(value)}"
        f"{constraints}",
        kbds.op_settings_text_actions_keyboard(key),
    )


async def _start_edit_intent(event, operator_id: int, key: str) -> None:

    if key not in settings_store.SCHEMA:
        await op_screen(
            event,
            f"Ключ «{key}» нельзя править из меню.",
            kbds.op_back_to_settings_keyboard(),
        )
        return
    async with session_scope() as session:
        baseline, revision = await settings_store.get_versioned(session, key)
    _intent_set(operator_id, key=key, kind="single", baseline=baseline, revision=revision)
    rule = settings_store.SCHEMA[key]
    if rule.get("type") is str:
        await _show_edit_page(event, operator_id, key, 0)
        return
    hint = ""
    if rule.get("url"):
        hint = "\n\nПришлите новый URL (http:// или https://)."
    elif rule.get("type") is str:
        max_len = rule.get("max_len", "?")
        hint = f"\n\nПришлите новый текст одним сообщением (до {max_len} симв)."
    await op_screen(
        event,
        f"✏️ Редактирование «{key}»\n"
        f"· · · · · · · ·"
        f"{hint}",
        kbds.op_settings_text_cancel_keyboard(key),
    )


async def _show_edit_page(event, operator_id: int, key: str, page: int) -> None:
    intent = _intent_get(operator_id)
    if not intent or intent.get("kind") != "single" or intent.get("key") != key:
        await op_screen(event, "Редактирование уже завершено. Откройте настройку заново.",
                        kbds.op_back_to_settings_keyboard())
        return
    rule = settings_store.SCHEMA.get(key, {})
    if rule.get("type") is not str:
        return
    # Keep the same snapshot for every page: a colleague may change the
    # setting while this operator is reading or preparing a replacement.
    value = intent.get("baseline")
    _intent_set(operator_id, key=key, kind="single", baseline=value, revision=intent.get("revision"))
    parts = split_text_units(value if isinstance(value, str) else "")
    page = max(0, min(page, len(parts) - 1))
    shown = parts[page] if parts[page] else "Не задано"
    page_label = f" · часть {page + 1}/{len(parts)}" if len(parts) > 1 else ""
    hint = ("Пришлите новую ссылку (http:// или https://)." if rule.get("url") else
            f"Пришлите новый текст одним сообщением (до {rule.get('max_len', '?')} символов).")
    await op_screen(
        event,
        f"✏️ {_TITLE_MAP.get(key, key)}\n\nТекущий текст{page_label}:\n{shown}\n\n{hint}",
        kbds.op_settings_text_cancel_keyboard(key, page=page, total_pages=len(parts)),
    )


async def _apply_single_edit(
    event, operator_id: int, key: str, new_text: str
) -> bool:
    """Применить новое значение текст/URL-ключа (и commit_author_*) с
    записью before→after в audit_log.

    Возвращает True, если значение применено; False — если validate
    отклонил ввод (показана ошибка + cancel-клавиатура). На False
    перехватчик `handle_settings_edit_text` сохраняет intent, чтобы
    оператор мог прислать исправленное значение следующим сообщением.
    """

    ok, msg = settings_store.validate(key, new_text)
    if not ok:
        await op_send(event, f"❌ {msg}", kbds.op_settings_text_cancel_keyboard(key))
        return False
    try:
        async with session_scope() as session:
            intent = _intent_get(operator_id)
            if intent and intent.get("kind") == "single" and intent.get("key") == key and "baseline" in intent:
                old_value = intent["baseline"]
                revision = intent.get("revision")
            else:
                old_value, revision = await settings_store.get_versioned(session, key)
            if revision is None:
                raise settings_store.SettingConflictError(key)
            await settings_store.set_value(session, key, new_text, expected_revision=revision)
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
    except settings_store.SettingConflictError:
        await op_screen(
            event,
            "Текст изменён другим сотрудником. Ваш вариант не сохранён. "
            "Откройте настройку заново и сверьте текущее значение.",
            kbds.op_back_to_settings_keyboard(),
        )
        # The stale intent must be dropped; otherwise the next ordinary
        # message would be treated as another edit of an outdated snapshot.
        return True
    if key in {"commit_author_name", "commit_author_email"}:
        await _show_author_card(event)
    else:
        await _show_text_card(event, key)
    return True
