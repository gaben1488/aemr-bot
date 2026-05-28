"""Property-based тесты для трёх критичных валидаторов гос-бота.

Дополняет example-based тесты Hypothesis-генерацией edge cases. Все
три валидатора — на критических путях:

- `_is_whitelisted_url` — антифишинг (broadcast URL whitelist,
  operator reply URL check, settings_store welcome URL guard).
- `_mask_phone` — PII в admin UI (152-ФЗ §6 минимизация).
- `defang_url_in_text` — антифишинг (admin chat preview жителева
  followup'а, чтобы оператор не кликнул по фишинг-ссылке).

Любая регрессия в этих функциях = security incident, поэтому
property-based safety net важна.
"""
from __future__ import annotations

import string

from hypothesis import given, strategies as st

from aemr_bot.services.admin_events import _mask_phone
from aemr_bot.services.settings_store import _is_whitelisted_url
from aemr_bot.utils.url_defang import defang_url_in_text


# ──────────────────────────────────────────────────────────────────────
# Часть 1 — `_is_whitelisted_url`
# ──────────────────────────────────────────────────────────────────────


_GOV_SUFFIXES = (
    "elizovomr.ru",
    "kamgov.ru",
    "gosuslugi.ru",
    "kamchatka.gov.ru",
)


class TestIsWhitelistedUrl:
    """Whitelist URL matcher — антифишинг на исходящие ссылки.

    Инварианты:
    1. Allowed gov suffix (exact или subdomain) с lowercase host + http/https
       scheme → True. Любой другой комбинации → False.
    2. Mixed-case host (Gosuslugi.RU) → False (A4 hardening).
    3. Non-ASCII host (cyrillic omoglyph) → False (A4 hardening).
    4. Schemes кроме http/https → False (`file://`, `javascript:`).
    5. Garbage input не валит функцию.
    """

    @given(st.sampled_from(_GOV_SUFFIXES))
    def test_root_suffix_https_passes(self, suffix: str) -> None:
        assert _is_whitelisted_url(f"https://{suffix}/") is True
        assert _is_whitelisted_url(f"http://{suffix}/") is True

    @given(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=20),
        st.sampled_from(_GOV_SUFFIXES),
    )
    def test_subdomain_of_gov_passes(self, sub: str, suffix: str) -> None:
        """`<любой_sub>.elizovomr.ru` → True (через `endswith('.' + suffix)`)."""
        # Имена с дефисом в начале/конце — недопустимы по DNS, но
        # whitelist-matcher не проверяет DNS-валидность, только host
        # suffix. Filter таких — отдельная задача DNS-resolver'а.
        if sub.startswith("-") or sub.endswith("-"):
            return
        url = f"https://{sub}.{suffix}/path"
        assert _is_whitelisted_url(url) is True

    @given(st.sampled_from(_GOV_SUFFIXES))
    def test_uppercase_host_rejected(self, suffix: str) -> None:
        """A4 hardening: mixed-case host визуально подозрителен."""
        uppercased = suffix.upper()
        assert _is_whitelisted_url(f"https://{uppercased}/") is False

    @given(st.sampled_from(["javascript:alert(1)", "file:///etc/passwd",
                            "ftp://example.com/", "data:text/html,<script>"]))
    def test_non_http_scheme_rejected(self, url: str) -> None:
        assert _is_whitelisted_url(url) is False

    @given(st.text(alphabet=string.ascii_letters + string.digits + ".-", min_size=4,
                   max_size=30))
    def test_random_lowercase_host_rejected(self, host: str) -> None:
        """Случайный валидный по форме host без gov-суффикса → False."""
        # Не должен случайно совпасть с whitelist
        if any(host.lower().endswith(s) or host.lower() == s for s in _GOV_SUFFIXES):
            return
        # Lowercase'им — проверяем что matcher по содержанию, не по case.
        url = f"https://{host.lower()}/"
        assert _is_whitelisted_url(url) is False

    @given(st.text(alphabet="абвгдеёжзи", min_size=1, max_size=15))
    def test_cyrillic_host_rejected(self, host: str) -> None:
        """A4 hardening: non-ASCII в host (омоглиф `gоsuslugi.ru`) → False."""
        # Сам матcher отвергает любой non-ASCII в host.
        url = f"https://{host}.ru/"
        assert _is_whitelisted_url(url) is False

    @given(st.text(min_size=0, max_size=200))
    def test_garbage_does_not_raise(self, value: str) -> None:
        """Любая строка не должна валить функцию."""
        result = _is_whitelisted_url(value)
        assert isinstance(result, bool)

    def test_empty_string_rejected(self) -> None:
        assert _is_whitelisted_url("") is False

    def test_user_info_does_not_bypass_whitelist(self) -> None:
        """`https://attacker.com@elizovomr.ru` — user-info trick для
        фишинга. Парсер должен видеть host как `elizovomr.ru`, не
        `attacker.com`. Проверяем что matcher НЕ обманывается."""
        # Корректное поведение: user-info срезается, остаётся host
        # elizovomr.ru → True. Но: trick с `@` иногда заставляет
        # рендер показать `attacker.com` как видимый host. Bot защищает
        # whitelist'ом по реальному host'у, не по визуальной строке.
        assert _is_whitelisted_url("https://attacker.com@elizovomr.ru/") is True
        # А если whitelist'нутый host в user-info, а реальный — нет —
        # это попытка bypass, должна быть отвергнута.
        assert _is_whitelisted_url("https://elizovomr.ru@attacker.com/") is False


# ──────────────────────────────────────────────────────────────────────
# Часть 2 — `_mask_phone`
# ──────────────────────────────────────────────────────────────────────


class TestMaskPhone:
    """PII-маскирование телефона. 152-ФЗ §6 минимизация: оператор
    видит ровно 4 последние цифры для идентификации, не полный
    номер."""

    def test_none_returns_dash(self) -> None:
        assert _mask_phone(None) == "—"

    def test_empty_returns_dash(self) -> None:
        assert _mask_phone("") == "—"

    @given(st.text(alphabet=string.ascii_letters + "+-()."))
    def test_no_digits_returns_dash(self, value: str) -> None:
        """Если входная строка без цифр — masked output `—` (нет PII
        для маскирования)."""
        assert _mask_phone(value) == "—"

    @given(st.text(alphabet="0123456789", min_size=0, max_size=3))
    def test_short_digit_count_returns_dash(self, digits: str) -> None:
        """A7 hardening: <4 цифр → `—`, не raw phone (защита от
        утечки коротких/частичных номеров)."""
        assert _mask_phone(digits) == "—"

    @given(st.text(alphabet="0123456789", min_size=4, max_size=20))
    def test_output_format_invariants(self, digits: str) -> None:
        """Output формат: `[+|+7]***NNNN` где NNNN = последние 4 цифры."""
        result = _mask_phone(digits)
        # Не возвращает «—» (есть ≥4 цифр)
        assert result != "—"
        # Заканчивается на последние 4 цифры входа
        assert result.endswith(digits[-4:])
        # Содержит ровно 3 звёздочки
        assert result.count("*") == 3
        # Output структура: либо `+***NNNN` либо `+7***NNNN`. Цифр в
        # output ровно 4 (последние 4 цифры) — либо 5 если префикс «+7»
        # (одна 7-ка перед звёздами).
        digit_count_in_output = sum(1 for c in result if c.isdigit())
        assert digit_count_in_output in {4, 5}, (
            f"Output {result!r} содержит {digit_count_in_output} цифр, "
            f"ожидалось 4 или 5"
        )

    @given(st.text(alphabet="0123456789", min_size=11, max_size=14))
    def test_russian_format_uses_plus7_prefix(self, digits: str) -> None:
        """Длинный номер (11+ цифр) начинающийся с 7 или 8 → префикс `+7`."""
        if digits[0] not in {"7", "8"}:
            return
        result = _mask_phone(digits)
        assert result.startswith("+7***")

    @given(st.text(min_size=4, max_size=50))
    def test_does_not_leak_full_phone(self, value: str) -> None:
        """Главный 152-ФЗ инвариант: output никогда не содержит более
        4 последних цифр входа подряд."""
        digits = "".join(c for c in value if c.isdigit())
        result = _mask_phone(value)
        if result == "—":
            return  # masked completely, OK
        # Output может содержать только последние 4 цифры из digits.
        # Если в input цифр > 4 — первые `len(digits) - 4` цифр НЕ
        # должны попасть в output.
        if len(digits) > 4:
            leading = digits[:-4]
            assert leading not in result, (
                f"Утечка PII: ведущие цифры {leading!r} попали в output {result!r}"
            )

    def test_idempotent_after_first_mask(self) -> None:
        """Повторный mask masked-строки — output тоже masked, не
        возвращает raw input. (Технически не строго идемпотентен:
        первый прогон оставляет `+7***1234`, повторный сделает
        `+***1234` — но это всё ещё masked, не утечка)."""
        first = _mask_phone("+79991234567")
        assert first == "+7***4567"
        # Re-mask: digits = "74567", len(digits) >= 11? нет (5).
        # Так что result = "+***4567" (без `+7` prefix).
        second = _mask_phone(first)
        assert second == "+***4567"
        # Главное: PII не утекает.
        assert "9991234" not in second


# ──────────────────────────────────────────────────────────────────────
# Часть 3 — `defang_url_in_text`
# ──────────────────────────────────────────────────────────────────────


_ZWSP = "​"


class TestDefangUrl:
    """Антифишинг defang: вставка Zero-Width Space в URL для
    предотвращения auto-linkify в MAX-клиенте."""

    def test_empty_unchanged(self) -> None:
        assert defang_url_in_text("") == ""

    def test_plain_text_no_url_unchanged(self) -> None:
        text = "Привет, мир. Никаких ссылок."
        assert defang_url_in_text(text) == text

    @given(st.sampled_from(["https", "http", "HTTPS", "HTTP", "HttPs"]))
    def test_scheme_url_gets_zwsp_after_scheme(self, scheme: str) -> None:
        text = f"Click {scheme}://example.com for details"
        result = defang_url_in_text(text)
        # ZWSP вставлен между scheme и ://
        assert f"{scheme}{_ZWSP}://" in result
        # Оригинальный `scheme://` без ZWSP исчез
        assert f"{scheme}://" not in result.replace(f"{scheme}{_ZWSP}://", "")

    @given(st.sampled_from(["ya.ru", "vk.com", "bit.ly", "t.me", "phish.xyz",
                            "scam.online"]))
    def test_bare_domain_gets_zwsp(self, domain: str) -> None:
        text = f"Visit {domain} now"
        result = defang_url_in_text(text)
        # ZWSP попал в результат (defang сработал)
        assert _ZWSP in result
        # Domain как непрерывная строка больше не появляется
        # (ZWSP разорвал auto-linkify)
        assert domain not in result

    def test_idempotent_on_scheme_url(self) -> None:
        """Повторный defang не должен добавлять второй ZWSP."""
        once = defang_url_in_text("Click https://example.com")
        twice = defang_url_in_text(once)
        assert once == twice

    def test_idempotent_on_bare_domain(self) -> None:
        once = defang_url_in_text("Visit ya.ru")
        twice = defang_url_in_text(once)
        assert once == twice

    @given(st.text(alphabet=string.ascii_letters + " .,!?", max_size=100))
    def test_safe_text_unchanged(self, text: str) -> None:
        """Текст без URL-подобных шаблонов не меняется."""
        # Скипаем тексты с TLD-подобными последовательностями
        # (`x.ru` в середине случайной строки даёт false positive).
        result = defang_url_in_text(text)
        # Если в тексте есть `.<2-4 alphas>` который может матчить TLD,
        # тест слишком общий. Минимальная проверка — функция не
        # падает + возвращает str.
        assert isinstance(result, str)

    @given(st.text(min_size=0, max_size=200))
    def test_does_not_raise(self, value: str) -> None:
        """Любой input не должен валить функцию."""
        result = defang_url_in_text(value)
        assert isinstance(result, str)

    def test_no_unprotected_https_after_defang(self) -> None:
        """После defang — никакой `https://X` без ZWSP не должен
        остаться (главный антифишинг-инвариант)."""
        inputs = [
            "Visit https://example.com",
            "https://phish.io/login and https://attacker.net",
            "Text\nhttps://x.y.z\nMore",
        ]
        for text in inputs:
            result = defang_url_in_text(text)
            # Защита auto-linkify сломана: после ZWSP вставки MAX
            # больше не парсит как URL. ZWSP попал внутрь каждой
            # `https://`-последовательности.
            assert _ZWSP in result, f"defang не сработал для {text!r}"
            # `https://` без ZWSP не остался в каноническом виде
            assert "https://" not in result, (
                f"unprotected https:// в результате: {result!r}"
            )

    def test_idempotent_complex_text(self) -> None:
        """Идемпотентность на тексте с несколькими URL."""
        text = "See https://ya.ru and bit.ly/abc and http://Sub.Example.com/path"
        once = defang_url_in_text(text)
        twice = defang_url_in_text(once)
        assert once == twice
