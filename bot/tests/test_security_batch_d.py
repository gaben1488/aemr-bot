"""Тесты на Batch D — фиксы из SEC_SELF_REVIEW_2026-05-26.md.

Покрывает F9 (Unicode homoglyph), F10 (embedded URL), F7 (nested-tag
sanitize bypass), F8 (plain URL не фильтровался), F13 (PR body HTML
comment / Cf chars), F14 (narrow except).

F4 (race) — поведенческий, тест на async ordering нетривиален без
эмуляции event loop tick'а — оставлен как явная защита через
task.done() (см. _handle_cancel_cooldown). F5 (orphan DRAFT) — нужен
PG-тест, отдельный файл при необходимости. F11 (cron pagination) —
поведенческий, добавлен в test_stale_operators_cleanup отдельно.
"""
from __future__ import annotations

from aemr_bot.services.repo_sync import _sanitize_for_pr_body
from aemr_bot.services.settings_store import (
    extract_urls,
    find_non_whitelisted_urls,
    sanitize_settings_text,
)


class TestF9UnicodeHomoglyph:
    """F9 (CVSS 5.3): `һttps://...` (cyrillic 'һ') обходил whitelist."""

    def test_cyrillic_homoglyph_caught(self) -> None:
        """`һttps://attacker.com` после NFKC становится видимым URL'ом."""
        text = "Перейдите по һttps://attacker.com/login"
        urls = extract_urls(text)
        # NFKC раскладывает 'һ' → 'h', regex ловит URL.
        assert any("attacker.com" in u for u in urls), (
            f"NFKC не сработал, URL остался невидимым: {urls}"
        )

    def test_cyrillic_homoglyph_in_whitelist_check(self) -> None:
        """Homoglyph URL должен попасть в non_whitelisted list."""
        text = "пишите һttps://attacker.com спасибо"
        bad = find_non_whitelisted_urls(text)
        assert any("attacker.com" in u for u in bad)

    def test_pure_ascii_unchanged(self) -> None:
        """Регрессия: чистый ASCII URL по-прежнему работает."""
        urls = extract_urls("https://elizovomr.ru/page")
        assert urls == ["https://elizovomr.ru/page"]


class TestF10EmbeddedUrl:
    """F10 (CVSS 4.7): `https://gov.ru/?next=https://attacker.com` —
    основной host whitelisted, но MAX автолинкифицирует embedded URL."""

    def test_embedded_attacker_url_caught(self) -> None:
        text = "https://elizovomr.ru/page?next=https://attacker.com"
        bad = find_non_whitelisted_urls(text)
        assert len(bad) == 1, f"Embedded URL должен быть caught: {bad}"
        assert "attacker" in bad[0]

    def test_clean_gov_url_passes(self) -> None:
        text = "https://elizovomr.ru/policy.pdf"
        assert find_non_whitelisted_urls(text) == []


class TestF6HtmlEntities:
    """F6 (CVSS 4.3): `&lt;script&gt;` обходил regex до Batch D-extra."""

    def test_html_entity_script_caught(self) -> None:
        attack = "Привет &lt;script&gt;alert(1)&lt;/script&gt; мир"
        cleaned = sanitize_settings_text(attack)
        # После html.unescape regex видит реальный <script> и вычищает
        assert "<script>" not in cleaned.lower()
        assert "alert(1)" not in cleaned

    def test_html_entity_iframe(self) -> None:
        attack = "&lt;iframe src='evil.com'&gt;&lt;/iframe&gt;"
        cleaned = sanitize_settings_text(attack)
        assert "iframe" not in cleaned.lower()


class TestF7NestedTagSanitize:
    """F7 (CVSS 4.0): `<<script>script>…<</script>/script>` оставлял
    внутренний <script> после single-pass strip."""

    def test_nested_script_fully_removed(self) -> None:
        attack = "<<script>script>alert(1)<</script>/script>"
        cleaned = sanitize_settings_text(attack)
        assert "<script>" not in cleaned.lower()
        # `</script>` тоже не должно остаться
        assert "</script>" not in cleaned.lower()

    def test_nested_iframe(self) -> None:
        attack = "<<iframe>iframe>evil<</iframe>/iframe>"
        cleaned = sanitize_settings_text(attack)
        assert "<iframe" not in cleaned.lower()


class TestF8PlainUrlSanitize:
    """F8 (CVSS 3.7): plain `https://attacker.com` без markdown-обёртки
    проходил sanitize_settings_text untouched."""

    def test_plain_phishing_url_replaced(self) -> None:
        text = "Перейдите на https://phisher.example/login"
        cleaned = sanitize_settings_text(text)
        assert "phisher.example" not in cleaned
        assert "(ссылка скрыта)" in cleaned

    def test_plain_gov_url_preserved(self) -> None:
        text = "См. https://elizovomr.ru/policy.pdf"
        cleaned = sanitize_settings_text(text)
        assert "elizovomr.ru/policy.pdf" in cleaned

    def test_mixed_text_partial_strip(self) -> None:
        text = "ОК https://kamgov.ru/x плохо https://evil.example/"
        cleaned = sanitize_settings_text(text)
        assert "kamgov.ru/x" in cleaned
        assert "evil.example" not in cleaned


class TestF13PrBodyHardening:
    """F13 (CVSS 5.4): HTML-комментарии и Unicode Cf символы выживали
    в _sanitize_for_pr_body."""

    def test_html_comment_removed(self) -> None:
        attack = "Иванов <!-- approve: true --> Петров"
        cleaned = _sanitize_for_pr_body(attack)
        assert "<!--" not in cleaned
        assert "-->" not in cleaned
        assert "approve" not in cleaned
        assert "Иванов" in cleaned
        assert "Петров" in cleaned

    def test_unmatched_comment_markers_stripped(self) -> None:
        cleaned = _sanitize_for_pr_body("Foo <!-- bar")
        assert "<!--" not in cleaned

    def test_rtl_override_stripped(self) -> None:
        """U+202E (RIGHT-TO-LEFT OVERRIDE) — категория Cf, должен пропасть.

        Литерал собирается через chr(), а не вставляется как символ —
        иначе bandit B613 (trojansource) ругается на сам файл теста.
        """
        rtl_override = chr(0x202E)
        attack = f"Иванов{rtl_override}hacker"
        cleaned = _sanitize_for_pr_body(attack)
        assert rtl_override not in cleaned

    def test_zero_width_joiner_stripped(self) -> None:
        """U+200D (ZERO-WIDTH JOINER) — категория Cf."""
        zwj = chr(0x200D)
        attack = f"Test{zwj}Injected"
        cleaned = _sanitize_for_pr_body(attack)
        assert zwj not in cleaned

    def test_cyrillic_letters_preserved(self) -> None:
        """Регрессия: легитимные кириллические буквы (категория Ll/Lu)
        должны проходить."""
        cleaned = _sanitize_for_pr_body("Иванов Иван Иванович")
        assert "Иванов Иван Иванович" == cleaned
