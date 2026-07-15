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


# --- P2 (2026-07-16): голый хост на НЕ-курируемом TLD тоже гейтится ---
# Детекция для гейта список-независима: любой `label.TLD` c ASCII-буквенным
# TLD ловится и проверяется по whitelist. Раньше `.click`/`.zip` и сотни
# других TLD не были в `_DEFANG_TLDS` → фишинг-домен уходил жителю.


def test_bare_domain_uncurated_tld_click_blocked() -> None:
    # `.click` НЕ в старом `_DEFANG_TLDS` — раньше проходил мимо гейта.
    bad = find_non_whitelisted_urls("Подтвердите: gosuslugi-kamchatka.click")
    assert "gosuslugi-kamchatka.click" in bad


def test_bare_domain_uncurated_tld_zip_blocked() -> None:
    # `.zip` — реальный TLD (Google) и одновременно расширение файла;
    # MAX авто-линкует `foo.zip`, поэтому гейтим.
    bad = find_non_whitelisted_urls("скачайте архив foo.zip прямо сейчас")
    assert "foo.zip" in bad


def test_bare_domain_uncurated_tld_variety_blocked() -> None:
    # Набор дешёвых фишинг-TLD, которых нет в курируемом списке.
    for host in ("evil.link", "pay.mobi", "login.pro", "get.life", "x.cyou"):
        bad = find_non_whitelisted_urls(f"перейдите на {host} немедленно")
        assert host in bad, host


def test_bare_gov_domain_still_allowed_after_broadening() -> None:
    # Легитимный гос-домен по-прежнему проходит (whitelist решает).
    assert find_non_whitelisted_urls("Подробнее на elizovomr.ru") == []
    assert find_non_whitelisted_urls("новости: news.kamgov.ru") == []


def test_version_number_not_treated_as_host() -> None:
    # Числа/версии/IP/время не считаются хостами (TLD должен быть буквенным ≥2).
    assert find_non_whitelisted_urls("обновление версии 1.2.3 доступно") == []
    assert find_non_whitelisted_urls("адрес шлюза 192.168.1.1 в сети") == []
    assert find_non_whitelisted_urls("приём с 8.00 до 17.30") == []


def test_russian_missing_space_not_false_positive() -> None:
    # Рус. предложение без пробела после точки НЕ должно ловиться как хост:
    # кириллический «TLD» вне закрытого IDN-набора не матчится.
    assert find_non_whitelisted_urls("Добрый день.Меня зовут Иван Петров.") == []
    assert find_non_whitelisted_urls("Заявка принята.Ответ придёт позже.") == []


def test_file_names_not_treated_as_host() -> None:
    # Имена файлов с заведомо не-доменным расширением НЕ придерживают
    # штатный ответ оператора (MAX их не авто-линкует): «справка.pdf»,
    # «фото.jpg» — это файлы, а не домены.
    assert find_non_whitelisted_urls("Направьте справку.pdf на почту") == []
    assert find_non_whitelisted_urls("Приложите фото.jpg и скан.png") == []
    assert find_non_whitelisted_urls("документ отчёт.docx и таблица.xlsx") == []


def test_zip_still_gated_despite_file_extension() -> None:
    # `.zip`/`.mov` — реальные gTLD (Google) и цель фишинга: остаются под
    # гейтом, несмотря на то, что это ещё и файловые расширения.
    assert "foo.zip" in find_non_whitelisted_urls("скачайте foo.zip сейчас")
