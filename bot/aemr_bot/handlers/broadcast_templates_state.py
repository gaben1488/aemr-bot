"""Разделяемое ядро UI шаблонов рассылок: wizard-state + apply-dedupe.

Декомпозиция god-объекта `broadcast_templates.py` (DDD tactical,
2026-06-01). Группы ответственности (list/crud/wizard) вынесены в
соседние подмодули, но все они опираются на ОДНО in-memory состояние:

  - `_wizards` — словарь «оператор → шаг wizard'а». Один wizard на
    оператора (за сценарий — один шаг ввода);
  - `_apply_dedupe` — окно дедупликации двойного тапа по `op:tmpl:apply`.

Состояние держим здесь, в едином месте, и импортируем по ссылке в
подмодули и фасад. Так `patch("...broadcast_templates._wizards")` в
тестах и обращения из подмодулей видят ОДИН и тот же объект — иначе
группы разъехались бы на разные словари (ложно-зелёные тесты).

Сюда же — мелкие helper'ы без доменной привязки: TTL-метки, форматтер
даты, GC dedupe-словаря.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from aemr_bot import texts
from aemr_bot.config import settings as cfg
from aemr_bot.services import broadcast_templates as templates_service


_WIZARD_TTL_SEC = 600  # 10 минут — те же лимиты, что у broadcast wizard


# ---- wizard state ----------------------------------------------------

WizardStep = Literal[
    "new_awaiting_name",
    "new_awaiting_text",
    "new_preview",
    "rename_awaiting_name",
    "edit_awaiting_text",
    "edit_preview",
    "clone_awaiting_name",
    "search_awaiting_query",
]


@dataclass
class _TmplWizardState:
    step: WizardStep
    # Имя нового шаблона (new flow) или нового имени клона (clone flow).
    pending_name: str = ""
    # Текст и вложения, накопленные на шаге 2 — используются для
    # превью и итогового сохранения.
    pending_text: str = ""
    pending_attachments: list = field(default_factory=list)
    # Для rename/edit/clone — id источника / редактируемого шаблона.
    target_id: int | None = None
    # Имя шаблона-источника при клонировании (для prompt'а).
    source_name: str = ""
    expires_at: float = field(
        default_factory=lambda: time.monotonic() + _WIZARD_TTL_SEC
    )

    def expired(self) -> bool:
        return time.monotonic() > self.expires_at

    def renew(self) -> None:
        self.expires_at = time.monotonic() + _WIZARD_TTL_SEC


# Один wizard на оператора (за один сценарий — один шаг ввода).
_wizards: dict[int, _TmplWizardState] = {}


def _drop_expired() -> None:
    stale = [uid for uid, st in _wizards.items() if st.expired()]
    for uid in stale:
        _wizards.pop(uid, None)


def _format_dt(dt) -> str:
    """ISO дату в локальный «дд.мм.гггг ЧЧ:ММ» Камчатского времени."""
    from zoneinfo import ZoneInfo

    if dt is None:
        return "—"
    return dt.astimezone(ZoneInfo(cfg.timezone)).strftime("%d.%m.%Y %H:%M")


def _validate_tmpl_name(text: str) -> str | None:
    """Проверить имя шаблона: непустое и в пределах MAX_NAME_LEN.

    Единая точка валидации имени для всех wizard-шагов, спрашивающих имя
    (создание, переименование, клон). Возвращает готовый текст ошибки для
    показа оператору либо None, если имя валидно. Тексты и лимит —
    те же, что были инлайн в каждом шаге (NAME_EMPTY / NAME_TOO_LONG).
    """
    if not text:
        return texts.OP_TMPL_NAME_EMPTY
    if len(text) > templates_service.MAX_NAME_LEN:
        return texts.OP_TMPL_NAME_TOO_LONG.format(
            actual=len(text), limit=templates_service.MAX_NAME_LEN
        )
    return None


# ---- apply double-tap dedupe -----------------------------------------

# P3 #25 — double-tap dedupe для apply. MAX иногда задваивает callback
# (пользовательский tap и retry клиента). Без guard'а `record_usage`
# инкрементируется дважды, статистика «частоты обращений к шаблону»
# распухает. In-memory dict (actor_id, template_id) → monotonic-ts.
# Окно 3 сек: достаточно для защиты от двойного тапа, мало для блока
# легитимного «применил → отменил → применил снова».
_apply_dedupe: dict[tuple[int, int], float] = {}
_APPLY_DEDUPE_WINDOW_SEC = 3.0


def _is_recent_apply(actor_id: int, template_id: int) -> bool:
    key = (actor_id, template_id)
    prev = _apply_dedupe.get(key)
    if prev is None:
        return False
    return time.monotonic() - prev < _APPLY_DEDUPE_WINDOW_SEC


def _mark_apply(actor_id: int, template_id: int) -> None:
    _apply_dedupe[(actor_id, template_id)] = time.monotonic()
    # GC: чистим записи старше 5 окон, чтобы dict не разрастался.
    if len(_apply_dedupe) > 256:
        cutoff = time.monotonic() - _APPLY_DEDUPE_WINDOW_SEC * 5
        for k in list(_apply_dedupe.keys()):
            if _apply_dedupe[k] < cutoff:
                _apply_dedupe.pop(k, None)
