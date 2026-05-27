"""Pure utility functions для broadcast pipeline.

После Cluster C (Codex PR 7, 2026-05-28) — извлечены из
`handlers/broadcast.py` (1297 строк, монолит). Здесь только pure-
функции без зависимости от FSM-состояния wizard'а или БД-сессии:
форматирование прогресса/итога, парсинг Retry-After из 429,
расчёт адаптивного шага прогресс-карточки, классификация ЧС-
рассылки.

Тестируется отдельно от handler-цепочки (`test_broadcast_429_backoff.py`,
часть `test_broadcast_handlers.py`). `handlers/broadcast.py` импортирует
функции под исходными именами с подчёркиванием — старые тестовые
импорты `from aemr_bot.handlers.broadcast import _extract_retry_after`
продолжают работать через re-export.
"""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from aemr_bot import texts
from aemr_bot.config import settings as cfg


# Маркер «срочная рассылка» — текст с [ЧС] в начале или после пробела
# (case-insensitive). Для таких сокращаем cooldown до 30 секунд, чтобы
# оповещение о ЧС не задерживалось на 5 минут.
_EMERGENCY_MARKER = re.compile(r"(?:^|\s)\[ЧС\]", re.IGNORECASE)
_COOLDOWN_NORMAL_SEC = 300   # 5 минут — обычная рассылка
_COOLDOWN_EMERGENCY_SEC = 30  # 30 секунд — [ЧС] рассылка


def _broadcast_cooldown_seconds(text: str) -> int:
    """Сколько ждать перед фактической отправкой рассылки.

    [ЧС] в тексте → 30 сек (оператор всё ещё может отменить, но не
    задерживаем оповещение о реальной ЧС). Иначе — 5 минут.
    """
    return (
        _COOLDOWN_EMERGENCY_SEC
        if _EMERGENCY_MARKER.search(text)
        else _COOLDOWN_NORMAL_SEC
    )


def _format_progress(
    *, broadcast_id: int, total: int, delivered: int, failed: int
) -> str:
    """Текст карточки прогресса рассылки. Если есть failed —
    добавляем суффикс «· не доставлено: N»."""
    failed_suffix = (
        texts.OP_BROADCAST_FAILED_SUFFIX.format(failed=failed) if failed else ""
    )
    return texts.OP_BROADCAST_PROGRESS.format(
        number=broadcast_id,
        total=total,
        delivered=delivered,
        failed_suffix=failed_suffix,
    )


def _extract_retry_after(exc: Exception) -> float | None:
    """Достать Retry-After (в секундах) из MaxApiError если есть.

    maxapi 1.1.0 не парсит этот header явно, но может прокидывать в
    `exc.raw` или `exc.args`. Best-effort: пробуем атрибуты, ловим
    AttributeError, возвращаем None если не нашли — тогда вызывающий
    использует exponential backoff.
    """
    raw = getattr(exc, "raw", None)
    if isinstance(raw, dict):
        for key in ("retry_after", "Retry-After", "retryAfter"):
            value = raw.get(key)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    pass
    return None


def _compute_progress_step(total: int, rate_delay: float) -> float:
    """Адаптивный шаг обновления прогресс-карточки (в секундах).

    BROADCAST_PROGRESS_UPDATE_SEC (5 сек по умолчанию) рассчитан на
    рассылку 50–200 получателей: оператор видит около 10 обновлений.
    На совсем короткой рассылке (5 получателей × 1 сек) полоска
    обновилась бы один раз в самом конце; на очень длинной (1000
    получателей) MAX начнёт ограничивать частоту правок. Для коротких
    отправок ужимаем шаг, чтобы прогресс двигался заметно.
    """
    estimated_total_sec = max(1.0, total * rate_delay)
    return min(cfg.broadcast_progress_update_sec, estimated_total_sec / 10)


def _build_final_text(
    *, broadcast_id: int, total: int, delivered: int, failed: int, cancelled: bool
) -> str:
    """Итоговый текст рассылки для админ-карточки (отмена / готово)."""
    if cancelled:
        return texts.OP_BROADCAST_CANCELLED.format(
            number=broadcast_id, delivered=delivered, total=total
        )
    failed_line = (
        texts.OP_BROADCAST_FAILED_LINE.format(failed=failed) if failed else ""
    )
    return texts.OP_BROADCAST_DONE.format(
        number=broadcast_id,
        delivered=delivered,
        total=total,
        failed_line=failed_line,
    )


def _format_dt(dt: datetime | None, tz: ZoneInfo) -> str:
    """Локализованная дата/время в формате `DD.MM.YYYY HH:MM`.

    Принимает явно `tz`, чтобы функция оставалась pure — без
    глобального состояния. Вызывающий передаёт `ZoneInfo(cfg.timezone)`.
    """
    if dt is None:
        return "—"
    return dt.astimezone(tz).strftime("%d.%m.%Y %H:%M")


__all__ = [
    "_EMERGENCY_MARKER",
    "_COOLDOWN_NORMAL_SEC",
    "_COOLDOWN_EMERGENCY_SEC",
    "_broadcast_cooldown_seconds",
    "_format_progress",
    "_extract_retry_after",
    "_compute_progress_step",
    "_build_final_text",
    "_format_dt",
]
