"""Тесты на services/settings_store — валидация ключей и значений.

Сама БД (set_value, get) тестируется в интеграционном тесте с PG;
здесь — только pure validation/SCHEMA."""
from __future__ import annotations

import pytest

from aemr_bot.services.settings_store import SCHEMA, validate


class TestSchemaContents:
    def test_required_keys_present(self) -> None:
        """Эти ключи бот ожидает — без них будет crash при первом
        обращении. Регрессия: если кто-то удалит из SCHEMA — тест упадёт."""
        required = {
            "policy_url",
            "topics",
            "localities",
            "appointment_text",
            "emergency_contacts",
        }
        missing = required - SCHEMA.keys()
        assert not missing, f"missing in SCHEMA: {missing}"


class TestValidate:
    def test_unknown_key_rejected(self) -> None:
        ok, reason = validate("nonexistent_key_xyz", "value")
        assert ok is False
        assert "не разрешён" in reason or "unknown" in reason.lower()

    def test_string_key_accepts_string(self) -> None:
        # SEC #4: URL должен быть в host whitelist (gov-домены).
        ok, reason = validate("policy_url", "https://elizovomr.ru/policy.pdf")
        assert ok is True, reason

    def test_url_rejects_non_whitelisted_host(self) -> None:
        """SEC #4: даже https://, но чужой host → reject."""
        ok, reason = validate(
            "policy_url", "https://example.com/policy.pdf"
        )
        assert ok is False
        assert "whitelist" in reason.lower() or "host" in reason.lower()

    def test_list_key_rejects_string(self) -> None:
        ok, reason = validate("topics", "not-a-list")
        assert ok is False

    def test_list_key_accepts_list(self) -> None:
        ok, _ = validate("topics", ["Дороги", "ЖКХ"])
        assert ok is True

    def test_localities_list(self) -> None:
        ok, _ = validate("localities", ["Елизовское ГП", "Паратунское СП"])
        assert ok is True

    def test_str_too_long_rejected(self) -> None:
        ok, reason = validate("appointment_text", "x" * 100_000)
        assert ok is False
        # max_len ограничение
        assert "длин" in reason.lower() or "max" in reason.lower()

    def test_welcome_text_max_len_below_max_api_limit(self) -> None:
        """D1 (SECURITY_REVIEW_2026-05-27): max_len для welcome_text
        не должен совпадать с MAX-API hard limit 4000. SCHEMA-лимит
        оставляет 200 char запаса под будущие ack-маркеры и
        event_header'ы; иначе IT-оператор через UI может сохранить
        текст ровно 4000 char, бот добавит маркер → silent overflow
        с ValueError, аналогично закрытому в PR #101
        OP_HELP_FULL_LEGACY (8348 char)."""
        assert SCHEMA["welcome_text"]["max_len"] <= 3800, (
            "welcome_text max_len в SCHEMA должен быть ≤ 3800 — "
            "200 char запаса перед MAX-API hard limit 4000 на ack/event_header."
        )

    def test_consent_text_max_len_below_max_api_limit(self) -> None:
        """D1: то же, что и welcome_text. Дополнительно consent_text
        содержит placeholder `{policy_url}` (≤200 char URL), который
        при render подставляется поверх 12-char-шаблона → нетто +188
        char на каждом рендере. Запас должен покрывать и это."""
        assert SCHEMA["consent_text"]["max_len"] <= 3800, (
            "consent_text max_len в SCHEMA должен быть ≤ 3800 — "
            "запас под `{policy_url}` подстановку (до +188 char/render)."
        )

    @pytest.mark.parametrize(
        "key,value,expected_ok",
        [
            ("emergency_contacts", [{"name": "01", "phone": "01"}], True),
            # мультиномер через запятую — легитимный камчатский формат (seed/contacts.json)
            (
                "emergency_contacts",
                [{"name": "ЦЭС", "phone": "8 (415-31) 2-00-22, 6-11-02"}],
                True,
            ),
            # буквы по-прежнему запрещены (премиум-ник/email/текст вместо номера)
            ("emergency_contacts", [{"name": "x", "phone": "позвоните нам"}], False),
            ("emergency_contacts", [{"name": "01"}], False),  # без phone
            ("emergency_contacts", [], False),  # пустой список
            ("emergency_contacts", "not-a-list", False),
        ],
    )
    def test_emergency_contacts_validation(
        self, key: str, value, expected_ok: bool
    ) -> None:
        ok, _ = validate(key, value)
        assert ok is expected_ok

    def test_emergency_contacts_section_allowed(self) -> None:
        """`section` — опциональное поле, которое UI использует для
        группировки (см. seed/contacts.json). Валидация должна его
        пропускать, иначе baseline-данные из seed не пройдут set_value.
        """
        ok, _ = validate(
            "emergency_contacts",
            [
                {"section": "Электроэнергия", "name": "Камчатскэнерго", "phone": "8-800"},
                {"name": "01", "phone": "01"},  # без section тоже ok
            ],
        )
        assert ok is True


class TestObjListGrouping:
    """Чистая функция format_obj_list — рендер тела карточки списка
    объектов (emergency_contacts, transport_dispatcher_contacts).
    """

    def test_empty(self) -> None:
        from aemr_bot.services.settings_store import format_obj_list
        assert format_obj_list([]) == "(список пуст)"

    def test_flat_list_no_section(self) -> None:
        """Если у всех item'ов нет section — секционные заголовки не
        добавляем, остаётся плоский нумерованный список."""
        from aemr_bot.services.settings_store import format_obj_list
        body = format_obj_list([
            {"name": "Пожарная", "phone": "01"},
            {"name": "Скорая", "phone": "03"},
        ])
        assert "▸" not in body
        assert body.startswith("1. Пожарная — 01")
        assert "2. Скорая — 03" in body

    def test_grouped_by_section(self) -> None:
        """Если секций несколько — добавляются заголовки `▸ Секция`,
        порядок секций — по первому появлению (стабильность UI)."""
        from aemr_bot.services.settings_store import format_obj_list
        body = format_obj_list([
            {"section": "Экстренные службы", "name": "Пожарная", "phone": "01"},
            {"section": "Электроэнергия", "name": "Камчатскэнерго", "phone": "8-800"},
            {"section": "Экстренные службы", "name": "Скорая", "phone": "03"},
        ])
        # Заголовок секции первого появления — раньше других:
        first_section_idx = body.index("▸ Экстренные службы")
        second_section_idx = body.index("▸ Электроэнергия")
        assert first_section_idx < second_section_idx
        # Глобальная нумерация сохранена (idx 1..N совпадает с порядком
        # в исходном списке — это критично, иначе click-by-index сломает
        # навигацию obj_item).
        assert "1. Пожарная" in body
        assert "2. Камчатскэнерго" in body
        assert "3. Скорая" in body

    def test_mixed_with_other(self) -> None:
        """Item без section падает в визуальную секцию «Прочее»."""
        from aemr_bot.services.settings_store import format_obj_list
        body = format_obj_list([
            {"section": "Электроэнергия", "name": "Камчатскэнерго", "phone": "8-800"},
            {"name": "01", "phone": "01"},  # без section
        ])
        assert "▸ Электроэнергия" in body
        assert "▸ Прочее" in body


class TestIsWhitelistedUrl:
    """SECURITY_REVIEW_2026-05-28 §A4: hardening URL whitelist matcher.

    Существующая логика принимала `https://Gosuslugi.RU` (через
    `urlparse + .lower()`). Защитимся явно от:
    - mixed-case host (визуально подозрительно, обманывает пожилого
      жителя);
    - не-ASCII символов в host (unicode-омоглифы, ноль-width).
    """

    def test_valid_lowercase_passes(self) -> None:
        from aemr_bot.services.settings_store import is_whitelisted_url
        assert is_whitelisted_url("https://elizovomr.ru/news") is True
        assert is_whitelisted_url("https://www.gosuslugi.ru") is True
        assert is_whitelisted_url("https://kamgov.ru/path") is True

    def test_phishing_lookalike_rejected(self) -> None:
        """Phishing-домен `gosuslugi.ru.evil.example.com` не должен
        пройти suffix-match (host endswith `.example.com`, не
        `.gosuslugi.ru`)."""
        from aemr_bot.services.settings_store import is_whitelisted_url
        assert is_whitelisted_url(
            "https://gosuslugi.ru.evil.example.com"
        ) is False

    def test_mixed_case_host_rejected(self) -> None:
        """§A4: `https://Gosuslugi.RU` визуально подозрителен —
        rejected даже хотя `urlparse + .lower()` дал бы валидный
        suffix-match. Lowercase — стандартная DNS-практика."""
        from aemr_bot.services.settings_store import is_whitelisted_url
        assert is_whitelisted_url("https://Gosuslugi.RU") is False
        assert is_whitelisted_url("https://ELIZOVOMR.RU") is False
        assert is_whitelisted_url("https://kamgov.RU/x") is False

    def test_unicode_homoglyph_rejected(self) -> None:
        """§A4: `gоsuslugi.ru` с cyrillic «о» (U+043E) — типичный
        омоглиф-фишинг. Host содержит non-ASCII → rejected."""
        from aemr_bot.services.settings_store import is_whitelisted_url
        # cyrillic 'о' вместо latin 'o'
        assert is_whitelisted_url("https://gоsuslugi.ru") is False

    def test_non_http_scheme_rejected(self) -> None:
        from aemr_bot.services.settings_store import is_whitelisted_url
        assert is_whitelisted_url("ftp://elizovomr.ru") is False
        assert is_whitelisted_url("javascript:alert(1)") is False

    def test_empty_or_garbage_rejected(self) -> None:
        from aemr_bot.services.settings_store import is_whitelisted_url
        assert is_whitelisted_url("") is False
        assert is_whitelisted_url("not a url at all") is False
        assert is_whitelisted_url("https://") is False

    def test_subdomain_of_whitelisted_passes(self) -> None:
        from aemr_bot.services.settings_store import is_whitelisted_url
        assert is_whitelisted_url(
            "https://news.elizovomr.ru/article/1"
        ) is True
        assert is_whitelisted_url(
            "https://lk.gosuslugi.ru/profile"
        ) is True


class TestMaxBotExactWhitelist:
    """Точечный whitelist двух официальных ботов MAX (не гос-домен,
    поэтому не через `_URL_HOST_WHITELIST_SUFFIXES`, а через отдельный
    `_URL_EXACT_WHITELIST` — см. комментарий в settings_store.py).

    Ключевая гарантия: `max.ru` НЕ в host-suffix whitelist, поэтому
    `https://max.ru/anything_else` обязан отклоняться — точный список
    не должен случайно открыть весь домен."""

    def test_solodov_comments_bot_passes(self) -> None:
        from aemr_bot.services.settings_store import is_whitelisted_url
        assert is_whitelisted_url("https://max.ru/solodov_comments_bot") is True

    def test_pravitelstvo_kk_bot_passes(self) -> None:
        from aemr_bot.services.settings_store import is_whitelisted_url
        assert is_whitelisted_url("https://max.ru/pravitelstvo_kk_bot") is True

    def test_trailing_slash_normalized_and_passes(self) -> None:
        from aemr_bot.services.settings_store import is_whitelisted_url
        assert is_whitelisted_url("https://max.ru/solodov_comments_bot/") is True
        assert is_whitelisted_url("https://max.ru/pravitelstvo_kk_bot/") is True

    def test_bare_max_ru_root_rejected(self) -> None:
        from aemr_bot.services.settings_store import is_whitelisted_url
        assert is_whitelisted_url("https://max.ru/") is False
        assert is_whitelisted_url("https://max.ru") is False

    def test_other_bot_on_max_ru_rejected(self) -> None:
        """Суть защиты: whitelist двух ботов НЕ открывает весь max.ru."""
        from aemr_bot.services.settings_store import is_whitelisted_url
        assert is_whitelisted_url("https://max.ru/evil_bot") is False

    def test_query_string_appended_rejected(self) -> None:
        """`?x=1` — не точное совпадение, потенциальный tracking/redirect
        довесок. Строгое сравнение отклоняет, а не молча игнорирует query."""
        from aemr_bot.services.settings_store import is_whitelisted_url
        assert is_whitelisted_url(
            "https://max.ru/solodov_comments_bot?x=1"
        ) is False

    def test_uppercase_host_rejected(self) -> None:
        """§A4-подобная логика: mixed/upper-case host подозрителен и НЕ
        нормализуется в доверенный — та же дисциплина, что и для
        гос-доменов (test_uppercase_host_rejected выше)."""
        from aemr_bot.services.settings_store import is_whitelisted_url
        assert is_whitelisted_url(
            "https://MAX.ru/solodov_comments_bot"
        ) is False

    def test_find_non_whitelisted_urls_allows_both_bots(self) -> None:
        """Исходящий фильтр (operator_reply/broadcast) обязан пропускать
        оба URL — это симметрия с is_whitelisted_url."""
        from aemr_bot.services.settings_store import find_non_whitelisted_urls
        text = (
            "Комментарии губернатора: https://max.ru/solodov_comments_bot "
            "Правительство края: https://max.ru/pravitelstvo_kk_bot"
        )
        assert find_non_whitelisted_urls(text) == []

    def test_find_non_whitelisted_urls_blocks_other_max_bot(self) -> None:
        from aemr_bot.services.settings_store import find_non_whitelisted_urls
        bad = find_non_whitelisted_urls("Подпишись: https://max.ru/evil_bot")
        assert bad == ["https://max.ru/evil_bot"]


class TestNfkcNormalization:
    """NFKC-нормализация в extract_urls закрывает полноширинные и
    совместимые символы-двойники: мошенник пишет адрес полноширинными
    знаками, чтобы обойти whitelist/defang."""

    def test_fullwidth_url_extracted(self) -> None:
        from aemr_bot.services.settings_store import extract_urls

        urls = extract_urls("Пишут ｈｔｔｐｓ：／／attacker.com сюда")
        assert urls == ["https://attacker.com"]

    def test_fullwidth_url_flagged_non_whitelisted(self) -> None:
        from aemr_bot.services.settings_store import find_non_whitelisted_urls

        bad = find_non_whitelisted_urls(
            "ссылка ｈｔｔｐｓ：／／attacker.com"
        )
        assert bad == ["https://attacker.com"]

    def test_normal_url_unaffected(self) -> None:
        from aemr_bot.services.settings_store import extract_urls

        assert extract_urls("см https://example.org/x") == ["https://example.org/x"]
