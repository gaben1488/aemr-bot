"""SECURITY: исходящий URL-whitelist обязан ловить и «голые» домены.

Контекст. `find_non_whitelisted_urls` гейтит ИСХОДЯЩИЙ текст (ответ
оператора жителю, рассылка подписчикам, welcome/consent). Раньше он ловил
только `http(s)://…`, а «голый» домен без схемы (`vk-gosuslugi.top`)
проходил мимо — при том что MAX-клиент авто-линкует такой домен в
кликабельную ссылку. То есть скомпрометированный/небрежный оператор мог
прислать жителю кликабельный фишинг-домен под официальной шапкой, обойдя
контрол, который бот рекламирует («исходящие ссылки — только гос-домены»).

Эти тесты фиксируют закрытие дыры: голые не-гос домены (вкл. поддомены,
кириллицу, mixed-case) блокируются, а гос-домены и легитимный текст —
нет. Плюс регрессия на старое `http(s)://`-поведение и на отсутствие
ложных срабатываний на фрагментах пути гос-ссылок.
"""

from __future__ import annotations

from aemr_bot.services.settings_store import extract_urls, find_non_whitelisted_urls


# --- Голые не-гос домены: должны блокироваться (суть фикса) ---


def test_bare_non_gov_domain_blocked() -> None:
    bad = find_non_whitelisted_urls("Подтвердите вход: vk-gosuslugi.top")
    assert "vk-gosuslugi.top" in bad


def test_bare_non_gov_subdomain_blocked() -> None:
    bad = find_non_whitelisted_urls("перейдите на secure.login-gosuslugi.top/auth")
    assert any("login-gosuslugi.top" in b for b in bad)


def test_bare_cyrillic_homoglyph_domain_blocked() -> None:
    # Кириллический домен не в ASCII-whitelist → блок (как §A4 для http).
    bad = find_non_whitelisted_urls("зайдите на гоуслуги.рф сейчас")
    assert bad, "кириллический голый домен должен быть заблокирован"


def test_bare_mixed_case_domain_blocked() -> None:
    # §A4: mixed-case host подозрителен, блокируем даже gov-похожий.
    bad = find_non_whitelisted_urls("ссылка Vk-Gosuslugi.Top тут")
    assert bad


def test_bare_domain_adjacent_to_gov_url_still_caught() -> None:
    bad = find_non_whitelisted_urls("Сравните vk-gosuslugi.top и https://gosuslugi.ru")
    assert "vk-gosuslugi.top" in bad


# --- Гос-домены и легитимный текст: НЕ блокируются ---


def test_bare_gov_domain_allowed() -> None:
    assert find_non_whitelisted_urls("Подробнее на elizovomr.ru") == []


def test_bare_gov_subdomain_allowed() -> None:
    assert find_non_whitelisted_urls("новости: news.elizovomr.ru") == []


def test_plain_text_with_abbreviations_no_false_positive() -> None:
    # «т.д.», «п.2» и т.п. не должны считаться доменами.
    assert find_non_whitelisted_urls("Смотрите п.2 и т.д. на стенде у входа.") == []


def test_gov_url_with_tld_like_path_no_false_positive() -> None:
    # Фрагмент пути гос-ссылки, оканчивающийся на TLD-слово (`.app`),
    # не должен ловиться как «голый» домен.
    assert find_non_whitelisted_urls("https://elizovomr.ru/files/doc.app") == []


# --- Регрессия: прежнее http(s)://-поведение не сломано ---


def test_http_gov_url_allowed() -> None:
    assert find_non_whitelisted_urls("https://gosuslugi.ru/profile") == []


def test_http_non_gov_url_blocked() -> None:
    bad = find_non_whitelisted_urls("http://evil.example/login")
    assert bad == ["http://evil.example/login"]


def test_gov_url_clean_no_bare_double_count() -> None:
    # Хост внутри http-URL не должен дублироваться «голым» сканом.
    assert find_non_whitelisted_urls("https://elizovomr.ru/news/123") == []


# --- extract_urls: дедуп с сохранением порядка (O(n²)→O(n)) ---


def test_extract_urls_dedup_preserves_order() -> None:
    urls = extract_urls("http://a.ru x http://a.ru y http://b.ru")
    assert urls == ["http://a.ru", "http://b.ru"]


def test_extract_urls_many_distinct_ok() -> None:
    text = " ".join(f"http://h{i}.ru/p" for i in range(500))
    urls = extract_urls(text)
    assert len(urls) == 500
    assert urls[0] == "http://h0.ru/p"
