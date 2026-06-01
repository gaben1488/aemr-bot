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
from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import operators as ops_svc
from aemr_bot.services import settings_store
from aemr_bot.utils.event import send_or_edit_screen

# intent на правку часа ставится здесь (`_start_quiet_hour_intent`).
from aemr_bot.handlers.admin_settings_shared import _intent_set


async def _show_quiet_card(event) -> None:
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
        enabled = await settings_store.get(session, "admin_quiet_hours_enabled")
        start = await settings_store.get(session, "admin_quiet_hours_start")
        end = await settings_store.get(session, "admin_quiet_hours_end")
    enabled = bool(enabled)
    if not isinstance(start, int):
        start = 18
    if not isinstance(end, int):
        end = 9
    status_line = "🔕 включён" if enabled else "🔔 выключен"
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            "🌙 Тихий режим в админ-чате\n"
            "· · · · · · · ·\n"
            f"Сейчас: {status_line}\n"
            f"Окно: с {start:02d}:00 до {end:02d}:00 (Камчатка)\n\n"
            "Когда включён и текущее время в окне — не приходят\n"
            "пульс и рутинные уведомления (новые обращения,\n"
            "followup'ы, подписки/отписки/erase).\n\n"
            "Критичные алёрты (фейл бэкапа, ответы операторам,\n"
            "сбои retention) идут всегда, тихий режим их\n"
            "не затрагивает.\n\n"
            "Чтобы изменить часы — кнопки ниже."
        ),
        attachments=[kbds.op_settings_quiet_keyboard(enabled=enabled)],
    )


async def _toggle_quiet(event) -> None:
    """Переключить `admin_quiet_hours_enabled` + обновить cache."""
    from aemr_bot.services import quiet_hours

    async with session_scope() as session:
        current = await settings_store.get(session, "admin_quiet_hours_enabled")
        new_value = not bool(current)
        await settings_store.set_value(
            session, "admin_quiet_hours_enabled", new_value,
        )
        await session.commit()
        await quiet_hours.refresh_cache_from_db(session)
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
    _intent_set(
        operator_id,
        key=f"admin_quiet_hours_{which}",
        kind="quiet_hour",
        which=which,
    )
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            f"🌙 Час {label} тихого режима\n"
            "· · · · · · · ·\n"
            "Пришлите одним сообщением число от 0 до 23.\n"
            "Например: 18 — для начала в 18:00; 9 — для конца в 09:00.\n\n"
            "Окно может пересекать полночь: start=18, end=9\n"
            "значит «с 18:00 до 09:00, включая всю ночь»."
        ),
        attachments=[kbds.op_settings_quiet_input_cancel_keyboard()],
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
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=f"❌ «{raw[:40]}» — не число. Пришлите целое 0–23.",
            attachments=[kbds.op_settings_quiet_input_cancel_keyboard()],
        )
        return False
    if not (0 <= new_value <= 23):
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=f"❌ {new_value} вне диапазона. Допустимо 0–23.",
            attachments=[kbds.op_settings_quiet_input_cancel_keyboard()],
        )
        return False
    key = f"admin_quiet_hours_{which}"
    async with session_scope() as session:
        await settings_store.set_value(session, key, new_value)
        await ops_svc.write_audit(
            session,
            operator_max_user_id=operator_id,
            action="setting_update",
            target=key,
            details={"value": new_value},
        )
        await session.commit()
        await quiet_hours.refresh_cache_from_db(session)
    await _show_quiet_card(event)
    return True
