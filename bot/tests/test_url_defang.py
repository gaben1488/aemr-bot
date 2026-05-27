"""Тесты на utils/url_defang.py — защита оператора от случайного
клика на phishing-URL в admin-карточке."""
from __future__ import annotations

from aemr_bot.utils.url_defang import (
    defang_for_admin,
    defang_url_in_text,
    has_defangable_url,
)

# Z-W-S = Zero-Width Space (U+200B). Используем chr() — bandit B613.
ZWSP = chr(0x200B)


class TestDefangUrlInText:
    def test_https_breaks_autolink(self) -> None:
        defanged = defang_url_in_text("Перейдите на https://attacker.com")
        # Между https и :// должен появиться ZWSP — MAX-парсер уже
        # не распознаёт это как URL.
        assert f"https{ZWSP}://attacker.com" in defanged

    def test_http_also_defanged(self) -> None:
        defanged = defang_url_in_text("http://evil.example")
        assert f"http{ZWSP}://evil.example" in defanged

    def test_case_insensitive(self) -> None:
        defanged = defang_url_in_text("HTTPS://EVIL.EXAMPLE")
        # Сохраняем регистр схемы (через backreference \1)
        assert f"HTTPS{ZWSP}://" in defanged

    def test_no_url_unchanged(self) -> None:
        text = "Просто текст без ссылок"
        assert defang_url_in_text(text) == text

    def test_already_defanged_idempotent(self) -> None:
        """Повторный defang не добавляет второй ZWSP."""
        once = defang_url_in_text("https://x.com")
        twice = defang_url_in_text(once)
        assert once == twice

    def test_multiple_urls(self) -> None:
        text = "См. https://elizovomr.ru и http://evil.com"
        defanged = defang_url_in_text(text)
        assert f"https{ZWSP}://elizovomr.ru" in defanged
        assert f"http{ZWSP}://evil.com" in defanged

    def test_empty_input(self) -> None:
        assert defang_url_in_text("") == ""
        assert defang_url_in_text(None) is None


class TestDefangForAdmin:
    def test_none_returns_empty(self) -> None:
        assert defang_for_admin(None) == ""

    def test_empty_returns_empty(self) -> None:
        assert defang_for_admin("") == ""

    def test_passes_through(self) -> None:
        out = defang_for_admin("Адрес: ул. Ленина, 5, https://map.evil")
        assert "ул. Ленина, 5" in out
        assert "map.evil" in out
        assert f"https{ZWSP}://" in out


class TestHasDefangableUrl:
    def test_with_url(self) -> None:
        assert has_defangable_url("есть https://x.com") is True

    def test_with_bare_domain(self) -> None:
        """2026-05-27: warning должен показываться и для domain-only.
        Жители часто пишут `ya.ru` без схемы — MAX это auto-linkify'ит.
        """
        assert has_defangable_url("посмотри на ya.ru") is True
        assert has_defangable_url("заходи на bit.ly/abc") is True

    def test_without_url(self) -> None:
        assert has_defangable_url("без ссылок") is False

    def test_empty(self) -> None:
        assert has_defangable_url("") is False
        assert has_defangable_url(None) is False


class TestDefangBareDomain:
    """2026-05-27 (жалоба владельца): MAX-клиент auto-linkify'ит
    `ya.ru`, `bit.ly/x` и подобное даже без `http://` — оператор-
    бабушка может случайно тапнуть. Defang ZWSP между именем и TLD
    разрывает auto-linkify."""

    def test_bare_domain_ru(self) -> None:
        out = defang_url_in_text("Зайди на ya.ru и посмотри")
        assert f"ya{ZWSP}.ru" in out
        # Текст вокруг не пострадал
        assert "Зайди на" in out
        assert "и посмотри" in out

    def test_bare_domain_com_with_path(self) -> None:
        out = defang_url_in_text("ссылка bit.ly/abc")
        assert f"bit{ZWSP}.ly/abc" in out

    def test_bare_domain_cyrillic_tld(self) -> None:
        out = defang_url_in_text("госуслуги.рф работают")
        assert f"госуслуги{ZWSP}.рф" in out

    def test_bare_domain_uppercase(self) -> None:
        out = defang_url_in_text("Заходи на YA.RU прямо сейчас")
        assert f"YA{ZWSP}.RU" in out

    def test_no_match_unknown_tld(self) -> None:
        """`under.score` или `foo.bar` — не в списке TLD, не defang'им."""
        text = "файл backup.bar лежит в /home"
        assert defang_url_in_text(text) == text

    def test_no_match_decimal_ip(self) -> None:
        """`192.168.1.1` — не URL, не defang'им."""
        text = "сервер 192.168.1.1 недоступен"
        assert defang_url_in_text(text) == text

    def test_no_double_defang_when_scheme_present(self) -> None:
        """`https://ya.ru` — scheme defang срабатывает, bare domain
        регекс **не** добавляет второй ZWSP внутрь домена
        (negative lookbehind `(?<!//)` блокирует)."""
        out = defang_url_in_text("https://ya.ru")
        # Между https и :// → ZWSP. Между ya и .ru — НЕТ второго.
        assert f"https{ZWSP}://ya.ru" in out
        assert f"ya{ZWSP}.ru" not in out

    def test_idempotent_bare(self) -> None:
        once = defang_url_in_text("ya.ru")
        twice = defang_url_in_text(once)
        assert once == twice

    def test_multiple_bare_domains(self) -> None:
        out = defang_url_in_text("проверь ya.ru и google.com тоже")
        assert f"ya{ZWSP}.ru" in out
        assert f"google{ZWSP}.com" in out
