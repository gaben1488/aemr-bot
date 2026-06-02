"""P3 hardening (бэклог security review #2):

1. anti-spoof карточки — текст жителя не рисует поддельную «шапку» box-
   drawing глифами, мимикрируя под секции admin-карточки;
2. флаг двойного расширения вложений (`virus.exe.pdf`) — предупреждение
   оператору до открытия;
3. health не считает пустой `remote` локальным (защита диагностики, если
   порт окажется за reverse-proxy).
"""

from __future__ import annotations

from types import SimpleNamespace

from aemr_bot.health import _is_local_request
from aemr_bot.services.card_format import _strip_card_chrome, attachments_summary_line
from aemr_bot.utils.attachments import (
    has_suspicious_double_extension,
    suspicious_attachment_names,
)


# --- Fix 1: anti-spoof box-drawing в тексте жителя ---


def test_strip_card_chrome_removes_box_drawing() -> None:
    spoof = "━━━ ОБРАЩЕНИЕ #5 ━━━\n✅ закрыто администрацией"
    out = _strip_card_chrome(spoof)
    assert out is not None
    assert "━" not in out  # фейковая «шапка» обезврежена
    assert "ОБРАЩЕНИЕ" in out  # сам текст остаётся


def test_strip_card_chrome_keeps_normal_text() -> None:
    assert _strip_card_chrome("Во дворе яма у дома 5, п.2") == "Во дворе яма у дома 5, п.2"


def test_strip_card_chrome_none_safe() -> None:
    assert _strip_card_chrome(None) is None
    assert _strip_card_chrome("") == ""


# --- Fix 2: двойное расширение вложений ---


def test_double_extension_flagged() -> None:
    assert has_suspicious_double_extension("Постановление.exe.pdf") is True
    assert has_suspicious_double_extension("справка.scr.jpg") is True


def test_normal_filename_not_flagged() -> None:
    assert has_suspicious_double_extension("photo.jpg") is False
    assert has_suspicious_double_extension("отчёт.pdf") is False
    # inner-расширение не исполняемое — не флажим.
    assert has_suspicious_double_extension("doc.pdf.pdf") is False


def test_suspicious_names_from_attachments() -> None:
    atts = [
        {"type": "file", "payload": {"filename": "x.bat.pdf"}},
        {"type": "image", "name": "ok.jpg"},
    ]
    assert suspicious_attachment_names(atts) == ["x.bat.pdf"]


def test_summary_line_flags_double_extension() -> None:
    line = attachments_summary_line([{"type": "file", "payload": {"filename": "virus.exe.pdf"}}])
    assert "virus.exe.pdf" in line
    assert "двойное расширение" in line


def test_summary_line_clean_no_flag() -> None:
    assert "⚠️" not in attachments_summary_line([{"type": "image"}])


# --- Fix 3: health пустой remote не локальный ---


def test_empty_or_none_remote_not_local() -> None:
    assert _is_local_request(SimpleNamespace(remote="")) is False
    assert _is_local_request(SimpleNamespace(remote=None)) is False


def test_loopback_remote_is_local() -> None:
    assert _is_local_request(SimpleNamespace(remote="127.0.0.1")) is True
    assert _is_local_request(SimpleNamespace(remote="::1")) is True


def test_external_remote_not_local() -> None:
    assert _is_local_request(SimpleNamespace(remote="203.0.113.7")) is False
