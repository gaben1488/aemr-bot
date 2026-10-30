"""Explain known redaction without guessing the fate of legacy missing text."""
from __future__ import annotations


def content_notice(appeal) -> str:
    if getattr(appeal, "content_erased_at", None) is not None:
        return "Содержание и вложения очищены. Сохранились сведения об обращении."
    return ""
