"""Тесты на Batch C security-fixes.

- C1: sanitize_settings_text + get_text_with_fallback + get_consent_request_text
       + required_substr валидация consent_text.
- C2: URL-whitelist на broadcast wizard (через find_non_whitelisted_urls,
      покрыто batch B) + cooldown classifier (_broadcast_cooldown_seconds).
- M5: warning при URL в admin_followup / admin_card.

PG-зависимые тесты — отдельный файл (test_settings_dynamic_texts_pg.py).
"""
from __future__ import annotations

import pytest

from aemr_bot.services.settings_store import (
    sanitize_settings_text,
    validate,
)


class TestSanitizeSettingsText:
    """C1: защита welcome/consent текстов от опасных тегов и URL."""

    def test_plain_text_unchanged(self) -> None:
        text = "Здравствуйте. Это бот Администрации."
        assert sanitize_settings_text(text) == text

    def test_script_tag_removed(self) -> None:
        text = "Привет <script>alert(1)</script> мир"
        cleaned = sanitize_settings_text(text)
        assert "<script>" not in cleaned
        assert "alert(1)" not in cleaned

    def test_iframe_tag_removed(self) -> None:
        text = "до <iframe src='evil.com'></iframe> после"
        cleaned = sanitize_settings_text(text)
        assert "iframe" not in cleaned.lower()

    def test_onclick_handler_stripped(self) -> None:
        text = '<div onclick="evil()">click</div>'
        cleaned = sanitize_settings_text(text)
        assert "onclick" not in cleaned

    def test_markdown_link_whitelisted_kept(self) -> None:
        text = "См. [политику](https://elizovomr.ru/policy.pdf)"
        cleaned = sanitize_settings_text(text)
        assert "https://elizovomr.ru/policy.pdf" in cleaned
        assert "ссылка скрыта" not in cleaned

    def test_markdown_link_phishing_replaced(self) -> None:
        text = "перейдите [сюда](https://phish.example.com/login)"
        cleaned = sanitize_settings_text(text)
        assert "phish.example.com" not in cleaned
        assert "ссылка скрыта" in cleaned
        # Label сохраняется, чтобы текст оставался читаемым
        assert "сюда" in cleaned

    def test_javascript_scheme_blocked(self) -> None:
        text = "перейдите javascript:alert(1)"
        cleaned = sanitize_settings_text(text)
        assert "javascript:" not in cleaned
        assert "[заблокировано]" in cleaned

    def test_empty_input(self) -> None:
        assert sanitize_settings_text("") == ""
        assert sanitize_settings_text(None) is None


class TestConsentTextRequiredSubstr:
    """C1: consent_text валидируется как шаблон с обязательным
    placeholder'ом {policy_url}."""

    def test_consent_with_placeholder_passes(self) -> None:
        ok, _ = validate(
            "consent_text",
            "Согласие на ПДн. Политика: {policy_url}. Нажмите Согласен.",
        )
        assert ok is True

    def test_consent_without_placeholder_rejected(self) -> None:
        ok, reason = validate(
            "consent_text",
            "Согласие. Политика: https://elizovomr.ru/policy.pdf",
        )
        assert ok is False
        assert "{policy_url}" in reason


class TestWelcomeTextValidation:
    """C1-hardening снят 2026-05-27 по решению владельца. Антифишинг-
    блок вынесен полностью в отдельную кнопку «🛡️ Защита от мошенников»
    в главном меню (`SECURITY_INFO_TEXT`, handler `menu:security`).
    SCHEMA.welcome_text больше не требует подстроки «НИКОГДА не
    запрашиваем» — IT может править welcome через UI любым текстом.

    Тесты ниже фиксируют новый контракт: welcome_text валидируется
    только по типу/длине, без required_substr.
    """

    def test_welcome_short_text_passes(self) -> None:
        """Без required_substr достаточно непустого текста."""
        ok, _ = validate(
            "welcome_text",
            "Здравствуйте. Выберите действие.",
        )
        assert ok is True

    def test_welcome_with_antiphishing_still_passes(self) -> None:
        """Текст с антифишингом (старый формат) — продолжает валидироваться,
        снятие C1 не запрещает блок, просто не требует его."""
        ok, _ = validate(
            "welcome_text",
            "Здравствуйте. 🛡️ Мы НИКОГДА не запрашиваем паспорт.",
        )
        assert ok is True

    def test_welcome_too_long_rejected(self) -> None:
        """Длина >4000 — отказ (MAX-API hard limit)."""
        ok, reason = validate("welcome_text", "x" * 5000)
        assert ok is False
        assert "max_len" in reason or "длин" in reason.lower()

    def test_welcome_empty_rejected(self) -> None:
        """Пустой текст — отказ."""
        ok, _ = validate("welcome_text", "")
        assert ok is False

    def test_hardcoded_welcome_passes_validate(self) -> None:
        """Регрессия: hardcoded texts.WELCOME (fallback при пустой БД)
        обязан проходить validate."""
        from aemr_bot.texts import WELCOME
        ok, reason = validate("welcome_text", WELCOME)
        assert ok is True, f"texts.WELCOME не прошёл validate: {reason}"


class TestBroadcastCooldownClassifier:
    """C2: classifier _broadcast_cooldown_seconds.

    Все тесты требуют импорта `aemr_bot.handlers.broadcast`, который
    тянет `maxapi` — поэтому пропускаются в чисто-unit окружении.
    """

    def setup_method(self) -> None:
        pytest.importorskip("maxapi")

    def test_normal_text_5min(self) -> None:
        from aemr_bot.handlers.broadcast import (
            _broadcast_cooldown_seconds,
            _COOLDOWN_NORMAL_SEC,
        )
        assert _broadcast_cooldown_seconds(
            "Внимание, плановое отключение электричества завтра"
        ) == _COOLDOWN_NORMAL_SEC
        assert _COOLDOWN_NORMAL_SEC == 300  # 5 минут

    def test_emergency_marker_30sec(self) -> None:
        from aemr_bot.handlers.broadcast import (
            _broadcast_cooldown_seconds,
            _COOLDOWN_EMERGENCY_SEC,
        )
        assert _broadcast_cooldown_seconds(
            "[ЧС] Отключение горячей воды в Елизово до утра"
        ) == _COOLDOWN_EMERGENCY_SEC
        assert _COOLDOWN_EMERGENCY_SEC == 30

    def test_emergency_marker_case_insensitive(self) -> None:
        from aemr_bot.handlers.broadcast import (
            _broadcast_cooldown_seconds,
            _COOLDOWN_EMERGENCY_SEC,
        )
        assert _broadcast_cooldown_seconds("[чс] срочно") == _COOLDOWN_EMERGENCY_SEC

    def test_emergency_marker_in_middle(self) -> None:
        from aemr_bot.handlers.broadcast import (
            _broadcast_cooldown_seconds,
            _COOLDOWN_EMERGENCY_SEC,
            _COOLDOWN_NORMAL_SEC,
        )
        # Marker должен быть в начале строки или после пробела —
        # «вСтречено[ЧС]внутри слова» не считается ЧС'ом.
        assert _broadcast_cooldown_seconds(
            "Сегодня в 14:00 [ЧС] ожидается ветер"
        ) == _COOLDOWN_EMERGENCY_SEC
        # А вот эта подстрока — нет:
        assert _broadcast_cooldown_seconds(
            "Обычная новость без маркера"
        ) == _COOLDOWN_NORMAL_SEC


class TestUrlWarningInAdminCard:
    """M5: при URL в followup/summary — warning к карточке."""

    def test_url_warning_helper_with_url(self) -> None:
        from aemr_bot.services.card_format import _maybe_url_warning
        warning = _maybe_url_warning("Ссылка: http://example.com")
        assert "⚠️" in warning
        assert "не открывайте" in warning.lower() or "не открыв" in warning.lower()

    def test_url_warning_helper_without_url(self) -> None:
        from aemr_bot.services.card_format import _maybe_url_warning
        assert _maybe_url_warning("Просто текст без ссылок") == ""

    def test_url_warning_helper_empty(self) -> None:
        from aemr_bot.services.card_format import _maybe_url_warning
        assert _maybe_url_warning("") == ""
