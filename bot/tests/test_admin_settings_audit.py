"""Тесты audit-логирования при правке настроек через UI (`⚙️ Настройки бота`).

Проверяем helper `_clip_audit_value`: коротко резюмирует значение
до 200 симв с многоточием, нормализует None/list/dict через repr.
Полный audit-trail (before → after) у `setting_update` нужен для
расследований инцидентов в окне `audit_log_retention_days`.
"""
from __future__ import annotations

import pytest


pytest.importorskip("maxapi", reason="нужен maxapi для admin_settings импорта")


def test_clip_audit_value_none_to_dash() -> None:
    from aemr_bot.handlers.admin_settings import _clip_audit_value

    assert _clip_audit_value(None) == "—"


def test_clip_audit_value_short_str_passthrough() -> None:
    from aemr_bot.handlers.admin_settings import _clip_audit_value

    assert _clip_audit_value("hello") == "hello"


def test_clip_audit_value_long_str_truncated_with_ellipsis() -> None:
    from aemr_bot.handlers.admin_settings import _clip_audit_value

    long = "x" * 500
    out = _clip_audit_value(long)
    assert len(out) == 200
    assert out.endswith("…")


def test_clip_audit_value_list_via_repr() -> None:
    from aemr_bot.handlers.admin_settings import _clip_audit_value

    out = _clip_audit_value(["a", "b", "c"])
    assert "['a', 'b', 'c']" in out


def test_clip_audit_value_dict_via_repr() -> None:
    from aemr_bot.handlers.admin_settings import _clip_audit_value

    out = _clip_audit_value({"k": "v"})
    assert "'k': 'v'" in out


def test_clip_audit_value_long_list_truncated() -> None:
    from aemr_bot.handlers.admin_settings import _clip_audit_value

    huge_list = [f"item-{i}" for i in range(200)]
    out = _clip_audit_value(huge_list)
    assert len(out) == 200
    assert out.endswith("…")
