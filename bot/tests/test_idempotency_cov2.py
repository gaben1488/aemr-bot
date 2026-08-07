"""Покрытие оставшихся pure-веток services/idempotency.build_idempotency_key.

Базовый test_idempotency.py покрывает основные пути. Здесь — микро-ветки:
- callback присутствует, но callback_id пустой → cb= не добавляется
  (ветка 46->49), ключ собирается из других полей.
- event.timestamp отсутствует, но message.timestamp задан → ts берётся
  из сообщения (строки 60-61), даже когда body=None.

claim() / has_processed_raw() / try_mark_processed_raw() используют
pg_inster(on_conflict_do_nothing) и тестируются на Postgres в CI —
здесь не дублируем (sqlite не поддерживает этот dialect-specific INSERT).
"""
from __future__ import annotations

from types import SimpleNamespace as NS

from aemr_bot.services.idempotency import build_idempotency_key


def test_callback_present_but_empty_id_not_appended() -> None:
    event = NS(
        update_type="cb",
        callback=NS(callback_id=None),
        message=None,
        timestamp=500,
        chat_id=None,
        user=None,
    )
    key = build_idempotency_key(event)
    # cb= не добавлен (id пустой), но ts= есть → ключ не None.
    assert key is not None
    assert "cb=" not in key
    assert "ts=500" in key


def test_timestamp_falls_back_to_message_timestamp() -> None:
    event = NS(
        update_type="m",
        callback=None,
        message=NS(body=None, timestamp=777),
        timestamp=None,
        chat_id=None,
        user=None,
    )
    key = build_idempotency_key(event)
    assert key is not None
    assert "ts=777" in key
