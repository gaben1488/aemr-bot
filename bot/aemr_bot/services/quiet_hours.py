"""Тихий режим админ-чата (quiet hours).

Когда `admin_quiet_hours_enabled=True` в settings_store, не-критические
сообщения от бота в служебную группу подавляются в окне
[`admin_quiet_hours_start`, `admin_quiet_hours_end`) по локальному
времени `Asia/Kamchatka`. Окно может пересекать полночь — например,
default 18:00–09:00 включает всю ночь.

**Что подавляется:**
- pulse-hourly / pulse-workhours-extra (heartbeat'ы).
- Уведомления о новых обращениях, followup'ах, подписках/отписках,
  /erase ack от жителя (admin_events).
- Прогресс рассылок (broadcast progress lines).

**Что НЕ подавляется (критические сообщения):**
- Фейл бэкапа (admin не узнает иначе, пока ситуация не станет хуже).
- Алёрты cron-jobs о реальных проблемах (`backup-failed`,
  `stale-operators-cleanup error`, etc.).
- Прямые ответы оператора жителю и обратно (это не cron, это оператор
  работает прямо сейчас и ждёт реакции бота).

**Архитектура caching (2026-05-27):**
Чтобы `admin_bus.send` не открывал новую DB-сессию при каждом
сообщении (это создавало pool-contention в pytest и общий
перерасход коннектов в проде), флаг и часы кэшируются в-памяти.
`refresh_cache_from_db(session)` обновляет кэш — вызывается из
- старта бота (один раз при boot);
- pulse-cron'а каждый час;
- `settings_store.set_value` при правке `admin_quiet_hours_*`.

`is_quiet_hours_now()` — sync, читает только cached values. По
умолчанию False (cache не initialized → не подавляем, лучше шум,
чем потеря критичного уведомления).

См. `services/admin_bus.send(critical=...)` и `services/cron.py`
pulse-job'ы.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aemr_bot.config import settings as cfg
from aemr_bot.services import settings_store

log = logging.getLogger(__name__)
_TZ = ZoneInfo(cfg.timezone)


# In-memory cache. Sync read через `is_quiet_hours_now()`. Async
# refresh через `refresh_cache_from_db(session)` — вызывается из мест,
# где session уже открыта (cron, set_value).
_cache: dict = {
    "enabled": False,  # default: не подавляем пока БД не прочитана
    "start": 18,
    "end": 9,
}


def _is_in_window(now_hour: int, start: int, end: int) -> bool:
    """Внутри ли часа `now_hour` окно [start, end) с учётом перехода
    через полночь.

    Примеры:
    - start=18, end=9, now=22 → True (вечер, попадает).
    - start=18, end=9, now=2  → True (ночь, попадает).
    - start=18, end=9, now=10 → False (утро, мимо).
    - start=9, end=18, now=12 → True (день, прямое окно).
    - start=start, now==start → True (включительно начало).
    - start=end → пустое окно (никогда не True).

    Не валидируем диапазон — это задача SCHEMA validate.
    """
    if start == end:
        # Пустое окно — не подавляем ничего.
        return False
    if start < end:
        # Окно в одних сутках.
        return start <= now_hour < end
    # Окно пересекает полночь: [start, 24) ∪ [0, end).
    return now_hour >= start or now_hour < end


def is_quiet_hours_now() -> bool:
    """True если сейчас тихий режим И он включён в кэше.

    Sync, без DB-доступа. Читает только cached values, обновляемые
    через `refresh_cache_from_db`. До первого refresh возвращает
    False (cache initialised as disabled). Это **намеренный** default —
    лучше пропустить настройку чем тихо проглотить алёрты.
    """
    if not _cache.get("enabled"):
        return False
    start = _cache.get("start")
    end = _cache.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        return False
    now_hour = datetime.now(_TZ).hour
    return _is_in_window(now_hour, start, end)


async def refresh_cache_from_db(session) -> None:
    """Обновить cache из settings_store. Best-effort: при любой ошибке
    БД оставляем cache в текущем состоянии (предыдущие values, либо
    initial defaults).

    Вызывается из:
    - старта бота (main.py после seed);
    - pulse-cron'а (каждый час обновляет cache);
    - `settings_store.set_value('admin_quiet_hours_*', ...)` — чтобы
      тогда же отразить смену UI'ем.
    """
    try:
        enabled = await settings_store.get(session, "admin_quiet_hours_enabled")
        start = await settings_store.get(session, "admin_quiet_hours_start")
        end = await settings_store.get(session, "admin_quiet_hours_end")
    except Exception:
        log.debug(
            "quiet_hours.refresh_cache_from_db: settings read failed; "
            "оставляем cache в текущем состоянии",
            exc_info=False,
        )
        return

    if enabled is None:
        enabled = False
    if not isinstance(start, int):
        start = 18
    if not isinstance(end, int):
        end = 9

    _cache["enabled"] = bool(enabled)
    _cache["start"] = start
    _cache["end"] = end
    log.debug(
        "quiet_hours.refresh_cache_from_db: enabled=%s window=[%d, %d)",
        _cache["enabled"], _cache["start"], _cache["end"],
    )


def reset_cache_for_tests() -> None:
    """Сбросить cache в initial state — для изоляции test'ов."""
    _cache["enabled"] = False
    _cache["start"] = 18
    _cache["end"] = 9
