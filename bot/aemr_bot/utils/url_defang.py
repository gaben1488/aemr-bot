"""Экранирование URL'ов («defang») для безопасного показа в admin-чате.

SECURITY: житель в обращении или followup может прислать кликабельную
фишинговую ссылку. Оператор открывает карточку в MAX-клиенте и **видит
кликабельный URL** — один тап = вход на phishing-страницу с
правами оператора (cookies браузера, авто-вход в Госуслуги и т.п.).

Решение: перед отображением жителю входящего текста в admin-чате —
вставляем **невидимый разделитель** (Zero-Width Space U+200B) в URL.
Visually текст не меняется, но MAX-парсер уже не распознаёт это как
URL и не делает auto-link. Если оператор реально хочет открыть ссылку
— копирует, видит ZWSP при вставке в браузер и удаляет.

Два уровня покрытия (2026-05-27 расширено):

1. **URL со схемой** (`https://attacker.com`) — ZWSP между `https` и
   `://`. Покрывает явные ссылки.
2. **URL без схемы** (`ya.ru`, `bit.ly/abc`, `phish.com/login`) —
   ZWSP между именем 2-го уровня и TLD (`ya<ZWSP>.ru`). Покрывает
   жалобу владельца 2026-05-27: «оператор-бабушка случайно нажмёт».
   MAX-клиент (как и Telegram, Slack) auto-linkify'ит domain-only
   ссылки если TLD известный — `.ru/.com/.org/.net/...`. ZWSP между
   именем и точкой ломает auto-linkify.

Используется в admin-карточке (summary + timeline), в followup-
карточке, в /export файлах. НЕ используется в operator reply к
жителю (там URL жителю нужен кликабельный — но whitelist гарантирует
что URL только на гос-домены).

Зачем не «замена https → hxxps» (классический IOC-defang):
- visually шумно для оператора, который привык к URL'ам;
- наша задача — не сделать URL читаемым как «опасный», а сделать
  его **некликабельным**. ZWSP решает это без изменения внешнего вида.
"""

from __future__ import annotations

import re

# Zero-width space (U+200B) — невидимый символ, разрывает auto-linkify
# в большинстве чат-клиентов (Telegram, MAX, Slack, Discord, WhatsApp).
# Не используем U+200D (ZWJ) — он, наоборот, склеивает символы в
# некоторых рендерах. ZWSP — самый предсказуемый no-op для рендера.
_ZWSP = "​"


# TLD-list для defang-without-scheme. Собран из топ-доменов, которые
# **реально встречаются** в фишинг-кампаниях против российских жителей
# (2024–2026):
# - `ru/su/рф/by/kz/ua` — кириллический рынок;
# - `com/org/net/info/biz` — старые общие TLD;
# - `bit.ly`/`t.me`/`vk.com`-like — shortener'ы и соц-сети;
# - `io/co/me/cc` — модные «коротыши» для фишинга;
# - `xyz/top/club/online/site/shop` — самые дешёвые регистрации,
#   часто используются для одноразового phishing.
# Не пытаемся покрыть IANA-полный список (~1500 TLD) — оставляем только
# те, что (а) могут быть auto-linkify'нуты MAX, (б) реально опасны.
# Под-домены (`.co.uk`, `.gov.ru`) покрываются suffix-match'ем: `gov.ru`
# не в списке как отдельный TLD — но `someorg.gov.ru` всё равно
# попадёт под match на `.ru`.
_DEFANG_TLDS = (
    # Кириллица и СНГ
    "ru",
    "su",
    "рф",
    "by",
    "kz",
    "ua",
    "uz",
    "tj",
    "kg",
    "am",
    "md",
    # Общемировые
    "com",
    "org",
    "net",
    "info",
    "biz",
    "edu",
    "gov",
    "mil",
    # Country (включая популярные shortener-домены `bit.ly`, `t.co`,
    # `goo.gl`, `youtu.be` — все часто используются в фишинге).
    "io",
    "co",
    "me",
    "cc",
    "tv",
    "ws",
    "tk",
    "ml",
    "ga",
    "ly",
    "gl",
    "be",
    "to",
    "im",
    # Дешёвые/новые (частый фишинг)
    "xyz",
    "top",
    "club",
    "online",
    "site",
    "shop",
    "store",
    "win",
    "vip",
    "icu",
    "app",
    "dev",
    # IDN/punycode (SECURITY_REVIEW_2026-05-28 §A3). MAX-клиент
    # auto-linkify'ит punycode-домены так же, как обычные ASCII.
    # `xn--p1ai` = «.рф» в punycode, `xn--p1acf` = «.рус»,
    # `xn--80aswg` = «.сайт». Не покрывать → фишинг через
    # `xn--evil.xn--p1ai` пройдёт мимо defang'а.
    "xn--p1ai",
    "xn--p1acf",
    "xn--80aswg",
    # Country (расширение под §A3) — TLD'ы регулярно используются
    # в международных скам-кампаниях против рунета.
    "cn",
    "tr",
    "in",
    "ng",
    "mx",
    "br",
    "id",
    "vn",
    "ph",
    "th",
    # 2024-2026 расширения — относительно новые TLD'ы,
    # доступные за копейки, любимы скам-операциями.
    "bot",
    "tech",
    "cloud",
    "live",
    "work",
    "social",
    "world",
)

# Pre-compile один большой regex для всех TLD. Группируем в (?:...) для
# `\b<имя>\.(TLD)\b` patterns. Имя — 1+ ASCII alphanum или дефис,
# не начинается с дефиса. TLD — case-insensitive.
#
# Примеры что ловим:
# - `ya.ru`             → match
# - `bit.ly/abc`        → match (ly не в списке, но добавим)
# - `phish.com/login`   → match
# - `under.score`       → НЕ match (score не TLD)
# - `192.168.1.1`       → НЕ match (нет TLD из списка)
# - `https://ya.ru`     → НЕ match (уже defang'ится `\b(https?)://`
#                        выше; double-defang предотвращён `\b` и
#                        anchor'ом «не префикс http(s)://»)
# Имя домена: ASCII alphanum + дефис + кириллица (для IDN/punycode-
# подобных кириллических доменов типа `госуслуги.рф`). Используем
# \w (с re.UNICODE) + явно дефис, исключая `_`, `.` и пробелы.
# Анкор `\b` использовать с unicode проблематично — берём явный
# negative-lookbehind на `\w`/`.` чтобы не отрезать середину
# составного слова.
_BARE_DOMAIN_PATTERN = re.compile(
    r"(?<!//)"  # negative lookbehind: не префикс `//` (это уже scheme defang)
    r"(?<![\w.-])"  # negative lookbehind: не середина длинного слова/домена
    r"([\w](?:[\w\-]{0,61}[\w])?)"  # имя 2-го уровня (включая кириллицу)
    r"\.(" + "|".join(re.escape(t) for t in _DEFANG_TLDS) + r")"
    r"(?![\w.-])",  # negative lookahead: не середина длинного домена
    re.IGNORECASE | re.UNICODE,
)


def defang_url_in_text(text: str) -> str:
    """Вставить ZWSP в URL'ы для защиты от случайного клика.

    Две формы:
    1. `https://attacker.com` → `https<ZWSP>://attacker.com`.
    2. `ya.ru`, `bit.ly/x`     → `ya<ZWSP>.ru`, `bit<ZWSP>.ly/x`.

    Идемпотентно: повторный вызов не добавляет второй ZWSP
    (regex'ы не матчат уже defang'нутый текст).

    Quasi-URL с unicode-омоглифом (`һttps://`) бот в принципе не должен
    показывать оператору — они блокируются на входе через
    `find_non_whitelisted_urls` (settings_store.F9-fix). Но если каким-
    то путём прошёл — выглядит как plain text, не auto-linkify'ится.
    """
    if not text:
        return text
    # Шаг 1: defang со схемой. ZWSP между `https` и `://`.
    text = re.sub(
        r"\b(https?)://",
        r"\1" + _ZWSP + "://",
        text,
        flags=re.IGNORECASE,
    )
    # Шаг 2: defang domain-only. ZWSP между именем и `.TLD`.
    # Важно: шаг 1 уже defang'нул `https://ya.ru` → `https<ZWSP>://ya.ru`.
    # Domain `ya.ru` в этой строке всё ещё matches шаг 2 — это OK,
    # double-defang безвреден, итог: `https<ZWSP>://ya<ZWSP>.ru`,
    # тоже unclickable.
    text = _BARE_DOMAIN_PATTERN.sub(
        lambda m: f"{m.group(1)}{_ZWSP}.{m.group(2)}",
        text,
    )
    return text


def defang_for_admin(text: str | None) -> str:
    """Удобный wrapper: None-safe defang для показа в admin-чате.

    Возвращает пустую строку для None — это нормально для
    необязательных полей карточки (summary may be None в edge case).
    """
    if not text:
        return ""
    return defang_url_in_text(text)


def has_defangable_url(text: str | None) -> bool:
    """True если в тексте есть URL, который имеет смысл defang'ить.

    Узкая проверка на любую форму ссылки: `http(s)://...` ИЛИ bare
    domain `name.TLD` (см. `_DEFANG_TLDS`). Не дублирует extract_urls
    из settings_store (та для исходящего whitelist'а) — здесь
    domain-only детект поверх `_BARE_DOMAIN_PATTERN`.

    Расширено 2026-05-27: жители часто пишут `ya.ru` или `bit.ly/x`
    без схемы. MAX-клиент их auto-linkify'ит.

    Статус (2026-06-02): сейчас вызывающих в продакшене нет —
    `card_format._maybe_url_warning` гейтит ⚠️-warning напрямую через
    `extract_urls` (http/quasi) и эскалирует до ⛔ через
    `_BARE_DOMAIN_PATTERN` + threat-intel, не через эту функцию.
    Оставлена как публичная утилита (есть тест-контракт в
    `test_url_defang.py`); раньше docstring ошибочно утверждал, что её
    зовёт `_maybe_url_warning`.
    """
    if not text:
        return False
    if re.search(r"\bhttps?://", text, flags=re.IGNORECASE):
        return True
    return bool(_BARE_DOMAIN_PATTERN.search(text))


# Multi-label хост-паттерн для ИСХОДЯЩЕГО whitelist-гейта. В отличие от
# `_BARE_DOMAIN_PATTERN` (defang 2-го уровня для показа оператору), здесь
# ловим и поддомены — `secure.vk-gosuslugi.top`, `www.evil.com` — чтобы
# фильтр исходящих ссылок нельзя было обойти лишним поддоменом. TLD-список
# общий с defang'ом (`_DEFANG_TLDS`) — единый источник правды, без дрейфа.
_BARE_HOST_PATTERN = re.compile(
    r"(?<!//)"  # не сразу после `//`: это хост уже-явного http(s)://-URL
    r"(?<![\w.-])"  # не середина длинного слова/домена
    r"((?:[\w](?:[\w\-]{0,61}[\w])?\.)+"  # один+ label с точкой (поддомены)
    r"(?:" + "|".join(re.escape(t) for t in _DEFANG_TLDS) + r"))"  # TLD
    r"(?![\w.-])",  # не середина длинного домена
    re.IGNORECASE | re.UNICODE,
)


def extract_bare_hosts(text: str) -> list[str]:
    """«Голые» хосты (`name.tld`, в т.ч. с поддоменами) без схемы из текста.

    Возвращает то, что MAX-клиент авто-линкует БЕЗ `http(s)://`:
    `vk-gosuslugi.top`, `secure.login-gosuslugi.ru`. Хосты внутри
    уже-явных `http(s)://`-URL сюда не попадают (lookbehind на `//`),
    чтобы не дублировать `extract_urls`.

    Используется исходящим фильтром
    `settings_store.find_non_whitelisted_urls`: оператор/рассылка не
    должны слать жителю кликабельный не-гос домен в «голом» виде, минуя
    проверку `https?://`. Дедуп с сохранением порядка появления.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _BARE_HOST_PATTERN.finditer(text):
        host = match.group(1)
        if host not in seen:
            seen.add(host)
            out.append(host)
    return out
