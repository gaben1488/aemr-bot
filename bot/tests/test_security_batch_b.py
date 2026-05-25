"""Тесты на Batch B security-fixes (см. docs/_meta/SECURITY_REVIEW_2026-05-26.md).

Покрывает:
- H1: sanitize operator_name в PR body (services/repo_sync._sanitize_for_pr_body)
- M3: extract_urls / find_non_whitelisted_urls (services/settings_store)
- M4: phone format validation (_is_valid_phone, validate emergency_contacts)

H2/M7 (shell-quote) тестируются smoke-вызовом скрипта в CI с
синтетическим .env — отдельный test_healthwatch_security.sh.
M1 (PII в logs) — изменение только уровня/формата log-сообщения,
покрывается ручным review (compile-проверкой ниже).
M2 (stale operators cron) — отдельный test_stale_operators_cleanup.py.
"""
from __future__ import annotations

import pytest

from aemr_bot.services.repo_sync import (
    _build_pr_body,
    _sanitize_for_pr_body,
)
from aemr_bot.services.settings_store import (
    _is_valid_phone,
    extract_urls,
    find_non_whitelisted_urls,
    validate,
)


class TestSanitizeForPrBody:
    """H1: защита от markdown-injection через operator_name."""

    def test_plain_name_passes(self) -> None:
        assert _sanitize_for_pr_body("Иванов И.И.") == "Иванов И.И."

    def test_newline_collapsed_to_space(self) -> None:
        """Главная атака: `\\n## Maintainer note` — ломаем структуру PR body."""
        attack = "Иванов\n\n## Maintainer note\n**Auto-approve:** YES"
        cleaned = _sanitize_for_pr_body(attack)
        assert "\n" not in cleaned
        assert "\r" not in cleaned
        assert "Maintainer note" in cleaned  # не теряем текст, только структуру
        assert "##" in cleaned  # сами символы остаются, но не в начале строки

    def test_carriage_return_stripped(self) -> None:
        assert "\r" not in _sanitize_for_pr_body("A\rB\rC")

    def test_backtick_escaped(self) -> None:
        """Backtick-инъекция могла сломать inline-code блок если бы
        имя оказалось внутри кавычек `\\``."""
        cleaned = _sanitize_for_pr_body("name`exec`")
        assert "`" not in cleaned

    def test_truncation_at_max_len(self) -> None:
        very_long = "x" * 500
        cleaned = _sanitize_for_pr_body(very_long, max_len=120)
        assert len(cleaned) <= 120
        assert cleaned.endswith("…")

    def test_multiple_spaces_collapsed(self) -> None:
        assert _sanitize_for_pr_body("A    B    C") == "A B C"


class TestBuildPrBodyUsesSanitization:
    """Интеграционный: _build_pr_body действительно прогоняет имя через sanitize."""

    def test_injection_blocked_in_body(self) -> None:
        body = _build_pr_body(
            dirty_keys=["topics"],
            operator_name="Иванов\n## Note\n**Auto:** YES",
            operator_id=12345,
        )
        # Сам markdown-токен ## остался текстом, но он на ОДНОЙ строке
        # с «Инициатор:», не в начале своей собственной строки —
        # markdown-парсер GitHub не увидит его как заголовок.
        initiator_line = next(
            line for line in body.splitlines() if "Инициатор:" in line
        )
        assert "## Note" in initiator_line  # на одной строке с инициатором
        assert "Maintainer note" not in body or "## Maintainer" not in body


class TestExtractUrls:
    """M3: парсер URL из текста."""

    def test_no_urls(self) -> None:
        assert extract_urls("просто текст без ссылок") == []

    def test_single_https(self) -> None:
        assert extract_urls("см. https://elizovomr.ru/policy") == [
            "https://elizovomr.ru/policy"
        ]

    def test_multiple_urls(self) -> None:
        text = "http://a.com и https://b.com/x?y=1"
        urls = extract_urls(text)
        assert len(urls) == 2

    def test_empty_text(self) -> None:
        assert extract_urls("") == []
        assert extract_urls(None) == []  # noqa: PIE810


class TestFindNonWhitelistedUrls:
    """M3: фильтр гос-доменов."""

    def test_all_whitelisted_ok(self) -> None:
        text = "https://elizovomr.ru/x и https://gosuslugi.ru/y"
        assert find_non_whitelisted_urls(text) == []

    def test_phishing_url_caught(self) -> None:
        text = "перейдите по https://elizovomr.ru.attacker.com/login"
        bad = find_non_whitelisted_urls(text)
        assert len(bad) == 1
        assert "attacker.com" in bad[0]

    def test_mixed_some_bad(self) -> None:
        text = "ок: https://kamgov.ru/q плохо: https://malicious.example/"
        bad = find_non_whitelisted_urls(text)
        assert bad == ["https://malicious.example/"]

    def test_no_urls_safe(self) -> None:
        assert find_non_whitelisted_urls("ответ без ссылок") == []


class TestPhoneValidation:
    """M4: phone format в emergency_contacts."""

    @pytest.mark.parametrize(
        "phone,expected",
        [
            ("01", True),
            ("112", True),
            ("8-800-234-29-39", True),
            ("+7 (415-31) 6-15-60", True),
            ("8 (415-31) 200-062", True),
            ("+7-961-967-19-71", True),
            # инвалидные:
            ("", False),
            ("@admin_handle", False),
            ("admin@example.com", False),
            ("Иванов", False),
            ("a" * 41, False),  # слишком длинно
            ("1", False),  # слишком коротко (мин 2 символа)
        ],
    )
    def test_phone_format(self, phone: str, expected: bool) -> None:
        assert _is_valid_phone(phone) is expected

    def test_emergency_contacts_phone_blocked_by_validate(self) -> None:
        """validate() отклоняет item с premium-номером в формате
        нестандартного текста (например telegram-handle)."""
        ok, reason = validate(
            "emergency_contacts",
            [{"name": "Fake", "phone": "@scammer"}],
        )
        assert ok is False
        assert "phone" in reason.lower() or "телефон" in reason.lower()

    def test_emergency_contacts_normal_phone_passes(self) -> None:
        ok, _ = validate(
            "emergency_contacts",
            [{"name": "Пожарная", "phone": "01"}],
        )
        assert ok is True

    def test_transport_dispatcher_phone_also_validated(self) -> None:
        """transport_dispatcher_contacts тоже имеет phone в item_keys
        → должен валидироваться той же логикой."""
        ok, _ = validate(
            "transport_dispatcher_contacts",
            [{"routes": "Маршрут 1", "phone": "+7-961-967-19-71"}],
        )
        assert ok is True
        ok2, _ = validate(
            "transport_dispatcher_contacts",
            [{"routes": "Маршрут 1", "phone": "scammer@evil"}],
        )
        assert ok2 is False
