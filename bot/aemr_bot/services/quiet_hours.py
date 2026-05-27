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

Реализация — best-effort: при любой ошибке чтения settings возвращаем
False (не подавляем), чтобы не молча резать всё подряд при сбое БД.

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


async def is_quiet_hours_now(session) -> bool:
    """True если сейчас тихий режим И он включён в настройках.

    Best-effort: при ошибке БД / отсутствующих ключах возвращает False
    (не подавляем — пусть лучше уведомление пройдёт, чем потеряется).
    """
    try:
        enabled = await settings_store.get(session, "admin_quiet_hours_enabled")
        if not enabled:
            return False
        start = await settings_store.get(session, "admin_quiet_hours_start")
        end = await settings_store.get(session, "admin_quiet_hours_end")
        if not isinstance(start, int) or not isinstance(end, int):
            return False
    except Exception:
        log.debug("quiet_hours: settings read failed, treating as off", exc_info=False)
        return False
    now_hour = datetime.now(_TZ).hour
    in_window = _is_in_window(now_hour, start, end)
    if in_window:
        log.debug(
            "quiet_hours: active now (hour=%d, window=[%d, %d))",
            now_hour, start, end,
        )
    return in_window
