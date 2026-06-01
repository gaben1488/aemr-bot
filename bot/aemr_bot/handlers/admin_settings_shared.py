"""Общие примитивы подсистемы «⚙️ Настройки бота».

Выделено из god-объекта `admin_settings.py` как фундамент без
зависимостей на другие admin_settings_*-подмодули (чтобы не было
циклического импорта: фасад и подмодули импортируют отсюда).

Содержит:
- intent-кэш `_edit_intents` + `_intent_set/_get/_drop` — TTL-карта
  «следующее текстовое сообщение оператора = новое значение ключа»;
- `_clip_audit_value` — подготовка значения настройки к записи в
  audit_log.details (clip до 200 симв);
- `_render_value` — рендер значения для UI-карточки.

Фасад `admin_settings.py` re-export'ит эти имена, поэтому
`mod._intent_set` / `from ...admin_settings import _clip_audit_value`
в тестах продолжают работать. Подмодули, которым нужен intent
(`_text`, `_obj`, `_quiet`), импортируют `_intent_set` отсюда —
ссылка на тот же объект, общий мутабельный dict.
"""
from __future__ import annotations

import json
import time as _time
from typing import Any

# Intent: «следующее текстовое сообщение этого оператора — новое
# значение для ключа». TTL 5 минут (см. _EDIT_INTENT_TTL_SEC).
# operator_max_user_id -> {"key": str, "kind": str, "expires_at": float, "extra": dict}
_edit_intents: dict[int, dict] = {}
_EDIT_INTENT_TTL_SEC = 300.0

# Audit-trail: длинные значения настроек (welcome_text, goodbye_message,
# consent_text) могут быть до нескольких тысяч символов. Полный
# `before`/`after` в каждой записи audit_log раздул бы таблицу. Лимит
# 200 симв — достаточно, чтобы видеть «что поменялось» при расследовании
# инцидента, не теряя сути правки.
_AUDIT_VALUE_CLIP_LEN = 200


def _clip_audit_value(value: object) -> str:
    """Подготовить значение настройки к записи в audit_log.details.

    Списки/dict сериализуем через repr (компактнее json для коротких
    структур и не требует encoding-кода). Усечение через многоточие,
    чтобы было видно, что значение было длиннее.
    """
    if value is None:
        text = "—"
    elif isinstance(value, str):
        text = value
    else:
        text = repr(value)
    if len(text) > _AUDIT_VALUE_CLIP_LEN:
        return text[: _AUDIT_VALUE_CLIP_LEN - 1] + "…"
    return text


def _intent_set(operator_id: int, **kwargs) -> None:
    state = dict(kwargs)
    state["expires_at"] = _time.monotonic() + _EDIT_INTENT_TTL_SEC
    _edit_intents[operator_id] = state
    # Reliability-pass: opportunistic GC. _intent_get чистит только
    # тот ключ, который пришёл в get. Если оператор настроил intent
    # и не дёрнул его (закрыл клиент), запись висит вечно (то же на
    # каждом ребуте session-mid'ов). Раз в set'е (~10/день per
    # admin) — лёгкий проход с удалением истёкших. O(N) по числу
    # операторов — единицы записей; не hot path.
    if len(_edit_intents) > 16:
        now = _time.monotonic()
        for k in [k for k, v in _edit_intents.items() if v.get("expires_at", 0) < now]:
            _edit_intents.pop(k, None)


def _intent_get(operator_id: int) -> dict | None:
    state = _edit_intents.get(operator_id)
    if state is None:
        return None
    if _time.monotonic() > state.get("expires_at", 0):
        _edit_intents.pop(operator_id, None)
        return None
    return state


def _intent_drop(operator_id: int) -> None:
    _edit_intents.pop(operator_id, None)


def _render_value(value: Any, *, limit: int = 1500) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "\n…(обрезано)"
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    return rendered if len(rendered) <= limit else rendered[:limit] + "\n…(обрезано)"


def _parse_key_idx(suffix: str) -> tuple[str, int] | None:
    """Разобрать callback-суффикс «<key>:<idx>» в пару (key, idx).

    Единый парсер для list/obj-операций по индексу (удаление строки,
    просмотр/удаление объекта). Возвращает None, если разделителя нет
    или индекс не целое — вызывающий тогда молча выходит (как было в
    каждой инлайн-копии: `if len(parts) != 2: return` /
    `except ValueError: return`).
    """
    parts = suffix.split(":", 1)
    if len(parts) != 2:
        return None
    key, idx_str = parts[0], parts[1]
    try:
        idx = int(idx_str)
    except ValueError:
        return None
    return key, idx
