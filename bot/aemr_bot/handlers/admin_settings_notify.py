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
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._common import op_screen
from aemr_bot.services import notify_toggles, settings_store
from aemr_bot.services import operators as ops_svc
from aemr_bot.ui.settings_keyboards import NOTIFY_LABELS
from aemr_bot.utils.event import get_user_id


async def _get_all_toggles(session) -> dict[str, bool]:
    """Текущие значения всех шести тумблеров из БД (не из кэша —
    карточка должна показывать актуальное состояние сразу после
    правки через /setting в обход UI)."""
    out: dict[str, bool] = {}
    for key in notify_toggles.TOGGLE_KEYS:
        value = await settings_store.get(session, key)
        out[key] = True if value is None else bool(value)
    return out


async def _show_notify_card(event, *, notice: str | None = None) -> None:
    """Карточка «🔔 Уведомления» — шесть тумблеров, тап = toggle."""
    async with session_scope() as session:
        # refresh cache из БД на случай если оператор только что
        # поправил тумблер через `/setting` без рестарта.
        await notify_toggles.refresh_cache_from_db(session)
        snapshots = {key: await settings_store.get_versioned(session, key)
                     for key in notify_toggles.TOGGLE_KEYS}
        values = {key: True if value is None else bool(value) for key, (value, _) in snapshots.items()}
        revisions = {key: rev for key, (_, rev) in snapshots.items()}

    lines = [
        "🔔 Уведомления в админ-чат",
        "· · · · · · · ·",
        "Каждый переключатель управляет своим видом уведомлений",
        "независимо от времени. Тихий режим подавляет часть",
        "служебных сообщений только в выбранные часы.",
        "Галочка — включено, знак запрета — выключено. Нажмите для изменения.",
        "«Открытые» и «Просроченные» — при изменении очереди",
        "и повторно в новый рабочий день, пока она не пуста;",
        "уведомления о новых обращениях они не отключают.",
        "",
    ]
    if notice:
        lines.extend([notice, ""])
    for key in notify_toggles.TOGGLE_KEYS:
        mark = "✅" if values[key] else "⛔"
        lines.append(f"{mark} {NOTIFY_LABELS[key]}")
    lines.extend([
        "",
        "Оповещения об отзыве согласия и удалении данных",
        "не отключаются этими переключателями и тихим режимом.",
    ])

    await op_screen(
        event,
        "\n".join(lines),
        kbds.op_settings_notify_keyboard(values, revisions),
    )


async def _toggle_notify(event, key: str, *, expected: bool | None = None,
                         revision: int | None = None) -> None:
    """Переключить один из шести `admin_notify_*` + обновить cache."""
    if key not in notify_toggles.TOGGLE_KEYS:
        # Незнакомый ключ (порча payload'а) — просто перерисовываем
        # карточку без изменений, ничего не ломаем.
        await _show_notify_card(event)
        return
    try:
        async with session_scope() as session:
            current, current_revision = await settings_store.get_versioned(session, key)
            current_enabled = True if current is None else bool(current)
            if expected is None or revision is None or expected != current_enabled or revision != current_revision:
                raise settings_store.SettingConflictError(key)
            new_value = not current_enabled
            await settings_store.set_value(session, key, new_value, expected_revision=revision)
            await ops_svc.write_audit(session, operator_max_user_id=get_user_id(event),
                                      action="setting_update", target=key,
                                      details={"before": current_enabled, "after": new_value})
            await session.commit()
            await notify_toggles.refresh_cache_from_db(session)
    except settings_store.SettingConflictError:
        await _show_notify_card(event, notice="Настройка уже изменилась. Проверьте текущее значение.")
        return
    await _show_notify_card(event)
