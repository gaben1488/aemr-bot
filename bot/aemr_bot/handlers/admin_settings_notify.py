"""Wizard «🔔 Уведомления» — модульные тумблеры служебных уведомлений
в админ-чат.

Выделено по образцу `admin_settings_quiet.py` (тихий режим по времени
суток). Здесь — шесть НЕЗАВИСИМЫХ тумблеров, каждый гейтит конкретный
вид уведомления НЕЗАВИСИМО от времени суток (см. подробный контракт в
`services/notify_toggles.py`):

- pulse (pulse-hourly + pulse-workhours-extra);
- согласие на ПДн;
- подписки/отписки от рассылки;
- open-reminder / overdue-reminder по отдельности;
- месячный отчёт.

Юридически значимые события (`notify_consent_revoked`,
`notify_data_erased`) сюда НЕ входят — они всегда `critical=True`,
не подчиняются ни этой карточке, ни quiet hours (152-ФЗ, см.
`services/admin_events.py`).

Карточка — плоский список из шести кнопок-тумблеров (тап = toggle +
перерисовка), без wizard'а правки часов (в отличие от quiet hours тут
нечего вводить текстом — только on/off).
"""
from __future__ import annotations

from aemr_bot import keyboards as kbds
from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import notify_toggles, settings_store
from aemr_bot.ui.settings_keyboards import NOTIFY_LABELS  # noqa: F401 (re-export для UI-текста)
from aemr_bot.utils.event import send_or_edit_screen


async def _get_all_toggles(session) -> dict[str, bool]:
    """Текущие значения всех шести тумблеров из БД (не из кэша —
    карточка должна показывать актуальное состояние сразу после
    правки через /setting в обход UI)."""
    out: dict[str, bool] = {}
    for key in notify_toggles.TOGGLE_KEYS:
        value = await settings_store.get(session, key)
        out[key] = True if value is None else bool(value)
    return out


async def _show_notify_card(event) -> None:
    """Карточка «🔔 Уведомления» — шесть тумблеров, тап = toggle."""
    async with session_scope() as session:
        # refresh cache из БД на случай если оператор только что
        # поправил тумблер через `/setting` без рестарта.
        await notify_toggles.refresh_cache_from_db(session)
        values = await _get_all_toggles(session)

    lines = [
        "🔔 Уведомления в админ-чат",
        "· · · · · · · ·",
        "Каждый тумблер отключает свой вид уведомления НЕЗАВИСИМО",
        "от времени суток (в отличие от «🌙 Тихого режима» — тот",
        "глушит всё скопом только ночью).",
        "",
    ]
    for key in notify_toggles.TOGGLE_KEYS:
        mark = "✅" if values[key] else "⛔"
        lines.append(f"{mark} {NOTIFY_LABELS[key]}")
    lines.extend([
        "",
        "Отзыв согласия на ПДн и удаление данных жителем (/erase)",
        "не отключаются — юридически значимые события по 152-ФЗ,",
        "идут всегда, даже в тихий режим.",
    ])

    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text="\n".join(lines),
        attachments=[kbds.op_settings_notify_keyboard(values)],
    )


async def _toggle_notify(event, key: str) -> None:
    """Переключить один из шести `admin_notify_*` + обновить cache."""
    if key not in notify_toggles.TOGGLE_KEYS:
        # Незнакомый ключ (порча payload'а) — просто перерисовываем
        # карточку без изменений, ничего не ломаем.
        await _show_notify_card(event)
        return
    async with session_scope() as session:
        current = await settings_store.get(session, key)
        new_value = not (True if current is None else bool(current))
        await settings_store.set_value(session, key, new_value)
        await session.commit()
        await notify_toggles.refresh_cache_from_db(session)
    await _show_notify_card(event)
