"""Модульные тумблеры служебных уведомлений в админ-чат.

В отличие от `services/quiet_hours.py` (глушение НЕ-критических
сообщений скопом по времени суток), этот модуль отключает/включает
КОНКРЕТНЫЙ вид уведомления НЕЗАВИСИМО от времени суток:

- `admin_notify_pulse` — pulse-hourly / pulse-workhours-extra
  (гейт в `services/cron.py::_job_pulse`). startup-pulse НЕ подчиняется
  этому флагу — это разовое диагностическое событие при рестарте
  процесса, а не рутинный heartbeat.
- `admin_notify_consent` — уведомление о даче согласия
  (`services/admin_events.py::notify_consent_given`).
- `admin_notify_subscriptions` — подписка/отписка от рассылки
  (`notify_broadcast_subscribed` / `notify_broadcast_unsubscribed`).
- `admin_notify_open_reminder` / `admin_notify_overdue_reminder` —
  почасовые напоминалки операторам по отдельности
  (`_job_working_hours_open_reminder` / `_job_working_hours_overdue_reminder`).
- `admin_notify_monthly_stats` — месячный XLSX-отчёт (`_job_monthly_report`).

**НЕ подчиняются тумблеру** (юридически значимые события, 152-ФЗ):
`notify_consent_revoked` и `notify_data_erased` — они всегда `critical=True`
в `admin_bus.send`, см. обоснование в `services/admin_events.py`.

**Архитектура caching** — та же, что у `quiet_hours.py`: чтобы гейт не
открывал новую DB-сессию на каждую проверку (hot path у pulse, у каждого
события consent/subscribe), шесть флагов кэшируются в-памяти.
`refresh_cache_from_db(session)` обновляет кэш — вызывается из:
- старта бота (main.py, вместе с прогревом quiet_hours);
- pulse-cron'а каждый час (там уже есть открытая сессия для quiet_hours);
- UI-обработчика тумблера сразу после `settings_store.set_value`
  (по образцу `handlers/admin_settings_quiet.py::_toggle_quiet`).

`is_enabled(key)` — sync, читает только cached values. Default **True**
до первого refresh — сознательно противоположно default `quiet_hours`
(там default False = «не подавляем»). Здесь та же философия: «лучше шум,
чем потерянный алёрт» — до прогрева кэша уведомления идут как обычно,
никто не потеряется из-за того, что кэш ещё не прочитан.
"""
from __future__ import annotations

import logging

from aemr_bot.services import settings_store

log = logging.getLogger(__name__)

# Шесть управляемых ключей — единый источник истины для этого модуля.
# Совпадают с ключами в settings_store.DEFAULTS/SCHEMA (admin_notify_*).
TOGGLE_KEYS: tuple[str, ...] = (
    "admin_notify_pulse",
    "admin_notify_consent",
    "admin_notify_subscriptions",
    "admin_notify_open_reminder",
    "admin_notify_overdue_reminder",
    "admin_notify_monthly_stats",
)

# In-memory cache. Sync read через `is_enabled()`. Async refresh через
# `refresh_cache_from_db(session)`. Default True для всех — «лучше шум,
# чем потерянный алёрт» до первого refresh (см. docstring модуля).
_cache: dict[str, bool] = {key: True for key in TOGGLE_KEYS}


def is_enabled(key: str) -> bool:
    """True если уведомление `key` включено (по кэшу).

    Sync, без DB-доступа. Неизвестный `key` (опечатка/будущий рефактор)
    трактуется как «включено» — тумблер должен явно отключать, а не
    молча глушить всё незнакомое.
    """
    return bool(_cache.get(key, True))


async def refresh_cache_from_db(session) -> None:
    """Обновить cache всех шести тумблеров из settings_store.

    Best-effort: при любой ошибке БД cache остаётся в текущем
    состоянии (предыдущие values либо initial defaults — все True).
    """
    try:
        values = {key: await settings_store.get(session, key) for key in TOGGLE_KEYS}
    except Exception:
        log.debug(
            "notify_toggles.refresh_cache_from_db: settings read failed; "
            "оставляем cache в текущем состоянии",
            exc_info=False,
        )
        return

    for key, value in values.items():
        # Не-bool (испорченное значение в БД, ручная правка через psql) —
        # трактуем как True (see-above философия «лучше шум»).
        _cache[key] = True if value is None else bool(value)

    log.debug(
        "notify_toggles.refresh_cache_from_db: %s",
        {key: _cache[key] for key in TOGGLE_KEYS},
    )


def reset_cache_for_tests() -> None:
    """Сбросить cache в initial state (все True) — для изоляции test'ов."""
    for key in TOGGLE_KEYS:
        _cache[key] = True
