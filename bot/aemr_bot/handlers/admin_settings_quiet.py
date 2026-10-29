"""Wizard «🌙 Тихий режим в админ-чате».

Выделено из god-объекта `admin_settings.py`. Связная ответственность:
карточка статуса (enabled + окно start–end), toggle, правка часов
start/end через intent-flow (как у текстовых ключей: оператор шлёт
число 0–23 одним сообщением). Кэш `quiet_hours` обновляется после
каждого изменения.

`from aemr_bot.services import quiet_hours` делается ВНУТРИ функций
(как в исходнике) — тесты патчат `quiet_hours.refresh_cache_from_db`
по реальному пути модуля, это устойчиво к месту функции.
"""
from __future__ import annotations

from aemr_bot import keyboards as kbds
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._common import op_screen, op_send

# intent на правку часа ставится здесь (`_start_quiet_hour_intent`).
from aemr_bot.handlers.admin_settings_shared import _intent_get, _intent_set
from aemr_bot.services import operators as ops_svc
from aemr_bot.services import settings_store
from aemr_bot.utils.event import get_user_id


async def _show_quiet_card(event, *, notice: str | None = None) -> None:
    """Карточка «🌙 Тихий режим в админ-чате».

    Показывает текущее состояние (enabled + окно start–end) и две
    кнопки: toggle + переход в expert-edit hours через `op:setkey:*`.
    Edit hours делается стандартным intent flow (как для текстовых
    ключей) — оператор шлёт число одним сообщением.
    """
    from aemr_bot.services import quiet_hours

    async with session_scope() as session:
        # refresh cache из БД на случай если оператор только что
        # включил через `/setting` без рестарта.
        await quiet_hours.refresh_cache_from_db(session)
        enabled, revision = await settings_store.get_versioned(session, "admin_quiet_hours_enabled")
        start = await settings_store.get(session, "admin_quiet_hours_start")
        end = await settings_store.get(session, "admin_quiet_hours_end")
    enabled = bool(enabled)
    if not isinstance(start, int):
        start = 18
    if not isinstance(end, int):
        end = 9
    status_line = "🔕 включён" if enabled else "🔔 выключен"
    if enabled and start == end:
        active_line = "⚠️ Начало и конец совпадают: период не действует."
    elif enabled and quiet_hours.is_quiet_hours_now():
        active_line = "🌙 Сейчас действует."
    elif enabled:
        active_line = "☀️ Сейчас не действует: время вне выбранного окна."
    else:
        active_line = "Сейчас не действует: режим выключен."
    await op_screen(
        event,
        "🌙 Тихий режим в админ-чате\n"
        + (f"{notice}\n\n" if notice else "") +
        "· · · · · · · ·\n"
        f"Настройка: {status_line}\n"
        f"Окно: с {start:02d}:00 до {end:02d}:00 (Камчатка)\n"
        f"{active_line}\n\n"
        "В это время подавляются пульс, регулярные напоминания об "
        "обращениях и часть служебных уведомлений, например о подписке. "
        "Они не откладываются "
        "до конца тихого режима.\n\n"
        "Карточки новых обращений и дополнений, ответы на действия "
        "оператора и критические предупреждения могут приходить "
        "как обычно. Их доступность зависит от других настроек "
        "и работы сервиса.\n\n"
        "Если часы начала и конца совпадают, тихого периода нет. "
        "Изменить часы можно кнопками ниже.",
        kbds.op_settings_quiet_keyboard(enabled=enabled, revision=revision),
    )


async def _toggle_quiet(event, *, expected: bool | None = None, revision: int | None = None) -> None:
    """Переключить `admin_quiet_hours_enabled` + обновить cache."""
    from aemr_bot.services import quiet_hours

    try:
        async with session_scope() as session:
            current, current_revision = await settings_store.get_versioned(session, "admin_quiet_hours_enabled")
            if expected is None or revision is None or expected != bool(current) or revision != current_revision:
                raise settings_store.SettingConflictError("admin_quiet_hours_enabled")
            new_value = not bool(current)
            await settings_store.set_value(
                session, "admin_quiet_hours_enabled", new_value, expected_revision=revision,
            )
            await ops_svc.write_audit(session, operator_max_user_id=get_user_id(event),
                                      action="setting_update", target="admin_quiet_hours_enabled",
                                      details={"before": bool(current), "after": new_value})
            await session.commit()
            await quiet_hours.refresh_cache_from_db(session)
    except settings_store.SettingConflictError:
        await _show_quiet_card(event, notice="Настройка уже изменилась. Проверьте текущее значение.")
        return
    await _show_quiet_card(event)


async def _start_quiet_hour_intent(
    event, operator_id: int, *, which: str,
) -> None:
    """Запросить у IT-оператора новое значение часа start/end через
    intent flow (как для текстовых ключей).

    `which` = 'start' или 'end'. Сохраняет intent с
    kind='quiet_hour' и payload-полем для `handle_settings_edit_text`.
    """
    assert which in {"start", "end"}, f"unknown which={which!r}"
    label = "начала" if which == "start" else "конца"
    async with session_scope() as session:
        baseline, revision = await settings_store.get_versioned(session, f"admin_quiet_hours_{which}")
    _intent_set(
        operator_id,
        key=f"admin_quiet_hours_{which}",
        kind="quiet_hour",
        which=which,
        baseline=baseline,
        revision=revision,
    )
    await op_screen(
        event,
        f"🌙 Час {label} тихого режима\n"
        "· · · · · · · ·\n"
        f"Текущее значение: {baseline}:00.\n"
        "Пришлите одним сообщением число от 0 до 23.\n"
        "Например: 18 — для начала в 18:00; 9 — для конца в 09:00.\n\n"
        "Окно может пересекать полночь: start=18, end=9\n"
        "значит «с 18:00 до 09:00, включая всю ночь».",
        kbds.op_settings_quiet_input_cancel_keyboard(),
    )


async def _apply_quiet_hour_edit(
    event, operator_id: int, which: str, new_text: str,
) -> bool:
    """Применить введённое значение часа: parse → validate 0–23 →
    set_value → refresh cache → перерисовка карточки.

    Возвращает True, если значение применено; False — если ввод
    отклонён (не число / вне диапазона) и показана ошибка с cancel-
    клавиатурой. На False вызывающий перехватчик сохраняет intent,
    чтобы оператор мог прислать корректное значение следующим
    сообщением (иначе бот «молчал» бы на повторный ввод).
    """
    from aemr_bot.services import quiet_hours

    raw = new_text.strip()
    try:
        new_value = int(raw)
    except ValueError:
        await op_send(
            event,
            f"❌ «{raw[:40]}» — не число. Пришлите целое 0–23.",
            kbds.op_settings_quiet_input_cancel_keyboard(),
        )
        return False
    if not (0 <= new_value <= 23):
        await op_send(
            event,
            f"❌ {new_value} вне диапазона. Допустимо 0–23.",
            kbds.op_settings_quiet_input_cancel_keyboard(),
        )
        return False
    key = f"admin_quiet_hours_{which}"
    try:
        async with session_scope() as session:
            intent = _intent_get(operator_id)
            if intent and intent.get("key") == key and "baseline" in intent:
                baseline, revision = intent["baseline"], intent.get("revision")
            else:
                baseline, revision = await settings_store.get_versioned(session, key)
            if revision is None:
                raise settings_store.SettingConflictError(key)
            await settings_store.set_value(session, key, new_value, expected_revision=revision)
            await ops_svc.write_audit(
                session,
                operator_max_user_id=operator_id,
                action="setting_update",
                target=key,
                details={"before": baseline, "after": new_value},
            )
            await session.commit()
            await quiet_hours.refresh_cache_from_db(session)
    except settings_store.SettingConflictError:
        await _show_quiet_card(event, notice="Час изменён другим сотрудником. Откройте изменение заново.")
        return True
    await _show_quiet_card(event)
    return True
