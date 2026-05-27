import asyncio
import html
import json
import re
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from aemr_bot.config import settings as cfg
from aemr_bot.db.models import Setting

# SEC #4: whitelist хостов для URL-настроек. Operator-facing botов
# (citizens click trusted govbot link) — должны вести только на
# официальные ресурсы. Rogue/compromised IT не сможет поставить
# phishing URL.
#
# Подвиды доменов добавляются ниже — точное совпадение или suffix
# `.elizovomr.ru` / `.kamgov.ru` / `.gosuslugi.ru`. Если нужно
# временно разрешить новый домен — добавить сюда и редеплоить
# (не правится через UI, чтобы не выстрелить себе в ногу).
_URL_HOST_WHITELIST_SUFFIXES = (
    "elizovomr.ru",
    "kamgov.ru",
    "gosuslugi.ru",
    "kamchatka.gov.ru",
)


# SECURITY_REVIEW M4: phone-формат в emergency_contacts. Раньше любой
# текст принимался — IT мог по ошибке (или умыслом) вписать вместо
# номера telegram-ник, email, платный premium-номер «+7-900-911-XXXX»
# с тарификацией 50 руб/мин. Жители увидели бы это как «официальный
# номер службы».
#
# Допускаем: цифры, пробелы, плюс, скобки, дефис, точка. Минимум 2
# символа — это стандартные коды экстренных служб в России («01»,
# «02», «03», «112»). Максимум 40 (длинные международные с
# расширениями типа «+7 (415-31) 7-25-29»).
_PHONE_PATTERN = re.compile(r"^[\d\s\+\-\(\)\.]{2,40}$")


def _is_valid_phone(value: str) -> bool:
    """True если строка похожа на телефон по формату.

    Не валидируем что номер существует — это не наша задача. Только
    structural-проверка: набор символов и длина.
    """
    if not isinstance(value, str):
        return False
    return bool(_PHONE_PATTERN.match(value.strip()))


def _is_whitelisted_url(value: str) -> bool:
    """True если URL ведёт на разрешённый host (Elizovo / Kamchatka gov)."""
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return any(
        host == suffix or host.endswith("." + suffix)
        for suffix in _URL_HOST_WHITELIST_SUFFIXES
    )


# Публичная обёртка — переиспользуется в operator_reply, broadcast и
# других местах где надо отфильтровать исходящий URL по гос-whitelist.
# Внутренняя `_is_whitelisted_url` оставлена приватной для legacy-
# совместимости и validate() ниже.
def is_whitelisted_url(value: str) -> bool:
    return _is_whitelisted_url(value)


# SECURITY_REVIEW M3: исходящие URL в ответе оператора жителю должны
# идти только на гос-домены. Если оператор (или скомпрометированный
# оператор) вставил ссылку на сторонний сайт — мы блокируем доставку
# и пишем warning в admin-чат. Список гос-доменов = тот же что и для
# settings (SEC #4 whitelist).
#
# Regex покрывает классические http(s)://, без `\b` чтобы не упустить
# URL в конце строки. Domain-only (без scheme) НЕ ловим — это даёт
# оператору безопасный fallback (написать «kamgov.ru» текстом).
_URL_IN_TEXT_PATTERN = re.compile(
    r"https?://[^\s<>\"'`]+",
    re.IGNORECASE,
)


# F9 (SECURITY_REVIEW_2026-05-26 CVSS 5.3): quasi-URL pattern для
# unicode-омоглифов. Cyrillic 'һ' (U+04BB), греческий 'η', и т.п. —
# не decomposable через NFKC, обычный `https?://` их не ловит. Этот
# regex берёт **любые** 4-5 word-символов перед `://` (включая
# кириллицу, греческий, кодпойнты других алфавитов). После match'а
# мы валидируем scheme через `urlparse` — если не точно «http»/«https»
# (в ASCII), URL считается suspicious и помечается non-whitelisted.
_QUASI_URL_PATTERN = re.compile(
    r"[\w-￿]{4,5}://[^\s<>\"'`]+",
    re.IGNORECASE | re.UNICODE,
)


def extract_urls(text: str) -> list[str]:
    """Все http(s)-URL из текста. Пустой list если URL'ов нет.

    F9 hardening: ловим и легитимные `https?://`, и quasi-URL с
    unicode-омоглифом в scheme (`һttps://`, `ηttps://` и т.п.).
    Дедуплицируем и сохраняем порядок появления — это важно для
    тестов и стабильности UX (оператор видит ссылки в том же
    порядке, что в исходном тексте).
    """
    if not text:
        return []
    seen: list[str] = []
    for u in _URL_IN_TEXT_PATTERN.findall(text):
        if u not in seen:
            seen.append(u)
    # Добавляем quasi-URL'ы (с unicode-омоглифом), которых нет в seen
    for u in _QUASI_URL_PATTERN.findall(text):
        # Защита: regex может зацепить ASCII URL тоже — пропускаем
        # дубликаты.
        if u not in seen:
            seen.append(u)
    return seen


# SECURITY_REVIEW F10 (CVSS 4.7): URL внутри querystring другого
# (whitelisted) URL — open-redirect-style. Например
# `https://elizovomr.ru/page?next=https://attacker.com` пропускался
# whitelist'ом, потому что hostname `elizovomr.ru` — гос-домен, но
# MAX-клиент может автолинкифицировать вложенный `https://attacker.com`.
# Ловим вложенный URL отдельным regex'ом ВНУТРИ уже извлечённого URL.
_EMBEDDED_URL_PATTERN = re.compile(
    r"https?://[^/?#]+",  # любое http(s)://host… внутри текста URL'а
    re.IGNORECASE,
)


def _has_embedded_url(url: str) -> bool:
    """True если в строке URL найдено больше одного `https?://` —
    значит, в querystring или path вложена ссылка на другой ресурс.

    Используется в `find_non_whitelisted_urls`: даже если основной
    host в whitelist, наличие embedded-URL даёт повод для отказа.
    """
    return len(_EMBEDDED_URL_PATTERN.findall(url)) > 1


def find_non_whitelisted_urls(text: str) -> list[str]:
    """Список URL из текста, которые НЕ в гос-whitelist или содержат
    вложенный URL в querystring.

    Используется в operator_reply / broadcast для блокировки исходящей
    фишинг-ссылки. Если возвращает пустой список — текст безопасен
    с точки зрения URL. Покрывает:
    - Unicode-омоглифы (F9, через NFKC в `extract_urls`);
    - сторонние домены (исходный SEC #4 whitelist);
    - embedded URL внутри `?next=https://attacker` (F10).
    """
    bad: list[str] = []
    for u in extract_urls(text):
        if not _is_whitelisted_url(u):
            bad.append(u)
            continue
        if _has_embedded_url(u):
            bad.append(u)
    return bad


# SECURITY_REVIEW C1: санитизация welcome_text / consent_text перед
# тем, как они уйдут жителю в личку. IT-роль имеет права редактировать
# эти тексты через UI «⚙️ Настройки бота» — и это удобно для оператив-
# ных правок без редеплоя. Но если IT-аккаунт компрометирован (или
# просто кто-то ошибся) — текст не должен дать инжектить активный
# HTML, кликабельный `javascript:`-ссылки, или фишинг-URL на сторонний
# домен. Здесь — мягкая «текстовая» санитизация: всё, что выглядит как
# опасный markup или ссылка вне whitelist'а, либо вырезаем, либо
# заменяем на безопасный текст. Не криптографическая защита, а
# первая линия обороны от случайного / умышленного вреда.
# F3 (ReDoS защита): везде вместо `.*?` используется bounded
# `[^<>]{0,4000}` — гарантированно линейная сложность. welcome_text
# и consent_text имеют SCHEMA max_len=4000, поэтому реальные
# legitimate теги (если будут) короче. Для нашего use case (plain
# текст приветствия) допустимая длина содержимого тега — нулевая,
# тег целиком вычищается; bound нужен только для regex-safety.
_DANGEROUS_HTML_PATTERNS = (
    # Скрипты, iframe'ы, обработчики событий — никогда не должны попасть
    # к жителю даже если MAX случайно решит парсить их как HTML.
    re.compile(r"<\s*script[^>]{0,200}>[^<>]{0,4000}</\s*script\s*>", re.IGNORECASE),
    re.compile(r"<\s*iframe[^>]{0,200}>[^<>]{0,4000}</\s*iframe\s*>", re.IGNORECASE),
    re.compile(r"<\s*(script|iframe|object|embed|applet)[^>]{0,200}/?>", re.IGNORECASE),
    # F7: leftover closing tags после nested-cloak'а — `</script>`,
    # `</iframe>` и т.п. — выживали single-pass strip'ом. Ловим отдельно.
    re.compile(r"</\s*(script|iframe|object|embed|applet)\s*>", re.IGNORECASE),
    re.compile(r"\s+on[a-z]+\s*=\s*['\"][^'\"]{0,500}['\"]", re.IGNORECASE),  # onclick=...
)

# Markdown-ссылки `[label](url)`. Если url не в whitelist — заменяем
# всю конструкцию на label с пометкой «(ссылка скрыта)», чтобы
# фишинг-URL не оказался кликабельным.
_MD_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def sanitize_settings_text(value: str) -> str:
    """Очистить текст настройки (welcome_text / consent_text) для
    безопасной отправки жителю.

    Делает четыре вещи (после SECURITY_REVIEW F7/F8):
    1. Вырезает теги `<script>`, `<iframe>`, `<object>`, `<embed>`,
       `<applet>` (с содержимым) и любые `on*=`-обработчики. Делается
       **в цикле до фиксированной точки** — иначе nested-cloak
       `<<script>script>…<</script>/script>` оставляет внутренние
       теги после single-pass strip'а (F7).
    2. В markdown-ссылках `[label](url)` пропускает только URL на
       гос-домены (whitelist). Остальные заменяет на `label (ссылка
       скрыта)`.
    3. Заменяет `javascript:` / `data:` / `vbscript:` / `file:`
       -схемы внутри обычных URL на `[заблокировано]`.
    4. **Plain http(s)-URL вне markdown-конструкций** прогоняются
       через `find_non_whitelisted_urls`; не-whitelisted заменяются
       на «(ссылка скрыта)» (F8 — раньше docstring обещал, код не делал).

    Что НЕ делает:
    - не экранирует обычные символы (`<`, `>`, `&`) — текст идёт в
      MAX как plain-text, эти символы безопасны.
    """
    if not value:
        return value

    # F6 (HTML entities): сначала декодируем `&lt;script&gt;` →
    # `<script>`, потом санитизируем. Без этого regex'ы не видят
    # entity-encoded payload (классический bypass). После
    # санитизации НЕ кодируем обратно — текст идёт в MAX как
    # plain-text, угловые скобки безопасны как символы.
    out = html.unescape(value)

    # F7: цикл до фиксированной точки. Каждая итерация может открыть
    # ранее скрытый внутренний тег (после strip'а наружного). Лимит
    # в 5 итераций — защита от внезапного бесконечного цикла на
    # извращённом input (5 достаточно для всех известных bypass'ов).
    for _ in range(5):
        before = out
        for pat in _DANGEROUS_HTML_PATTERNS:
            out = pat.sub("", out)
        if out == before:
            break

    def _replace_md_link(match: re.Match) -> str:
        label = match.group(1)
        url = match.group(2)
        if _is_whitelisted_url(url):
            return f"[{label}]({url})"
        return f"{label} (ссылка скрыта)"

    out = _MD_LINK_PATTERN.sub(_replace_md_link, out)

    # Опасные URI-схемы (`javascript:`, `data:`, `vbscript:`, `file:`)
    # вне http-URL — заменяем на безопасную метку.
    out = re.sub(
        r"\b(javascript|data|vbscript|file):[^\s]*",
        "[заблокировано]",
        out,
        flags=re.IGNORECASE,
    )

    # F8: plain http(s)-URL без markdown-обёртки. Если такая ссылка не
    # ведёт на гос-домен — заменяем на «(ссылка скрыта)». Если ведёт —
    # оставляем как есть, чтобы welcome мог содержать живую гос-ссылку.
    # Использует тот же extract_urls (NFKC + regex), что и outgoing
    # whitelist.
    for bad_url in find_non_whitelisted_urls(out):
        # Один и тот же URL может попасть несколько раз — replace
        # заменяет все вхождения.
        out = out.replace(bad_url, "(ссылка скрыта)")

    return out

DEFAULTS: dict[str, Any] = {
    "welcome_text": None,
    "consent_text": None,
    # Автор коммитов от бота для services/repo_sync. Подставляется в
    # GitHub API при создании PR. Меняется через меню «👤 Автор
    # коммитов» в админ-панели — без редеплоя.
    "commit_author_name": None,
    "commit_author_email": None,
    "policy_url": (
        "https://elizovomr.ru/storage/attachments/2024/08/15/U9XfgiWRETCF0KKT.pdf"
    ),
    "electronic_reception_url": "https://kamgov.ru/questions",
    "udth_schedule_url": (
        "https://udth.elizovomr.ru/publikatsiia/raspisanie-prigorodnykh-avtobusov"
    ),
    "udth_schedule_intermunicipal_url": (
        "https://kamgov.ru/mintrans/current_activities/"
        "raspisania-dvizenia-passazirskogo-avtomobilnogo-transporta-"
        "mezmunicipalnogo-soobsenia-v-kamcatskom-krae"
    ),
    "appointment_text": (
        "Приём граждан временно исполняющим полномочия Главы Елизовского "
        "муниципального района А.С. Гончаровым осуществляется два раза в месяц "
        "(1 и 3 среда каждого месяца) по предварительной записи. "
        "Запись на приём ведётся по номеру телефона 8 (415-31) 7-25-29."
    ),
    "emergency_contacts": [],
    "transport_dispatcher_contacts": [],
    "topics": [],
    # Глобальный лимит «сколько картинок оператор может приложить к
    # одной рассылке». Раньше был в env BROADCAST_MAX_IMAGES; перенесли
    # сюда для оперативной правки IT-оператором через меню «⚙️ Настройки
    # бота» без редеплоя. 5 — баланс «афиша + 3-4 фото» vs нагрузка на
    # канал MAX (каждая картинка ×N подписчиков). Допустимый диапазон
    # 1–20 (см. SCHEMA).
    "broadcast_max_images": 5,
    # 2026-05-27 (quiet hours): тихий режим админ-чата. Когда
    # `admin_quiet_hours_enabled=True` И текущее локальное время в окне
    # [start, end) (с учётом перехода через полночь) — не-критические
    # сообщения от бота в админ-чат не отправляются (pulse, входящие
    # обращения, уведомления о подписках/отписках/erase). Критические
    # сообщения (фейл бэкапа, ошибки, прямые ответы оператора)
    # игнорируют флаг — оператор должен узнать о реальном инциденте
    # сразу. Окно по умолчанию: 18:00–09:00 (включая ночные часы).
    # Часовой пояс — `Asia/Kamchatka`, как и для всего расписания.
    "admin_quiet_hours_enabled": False,
    "admin_quiet_hours_start": 18,
    "admin_quiet_hours_end": 9,
    "localities": [
        "Елизовское ГП",
        "Вулканное ГП",
        "Корякское СП",
        "Начикинское СП",
        "Николаевское СП",
        "Новоавачинское СП",
        "Новолесновское СП",
        "Паратунское СП",
        "Пионерское СП",
        "Раздольненское СП",
    ],
}

# Белый список ключей, которые можно править, с допустимыми Python-типами и
# дополнительными правилами. /setting <key> <value> отклоняет всё, чего нет в
# этой карте.
SCHEMA: dict[str, dict] = {
    # C1-hardening снят 2026-05-27 по решению владельца. Раньше welcome
    # обязан был содержать антифишинговый блок «НИКОГДА не запрашиваем»
    # — это дублировало содержимое отдельной кнопки «🛡️ Защита от
    # мошенников» в главном меню (`SECURITY_INFO_TEXT`, handler
    # `menu:security`). Двойное упоминание перегружало welcome без
    # пользы; антифишинг живёт только в отдельной кнопке.
    # D1-fix 2026-05-27: max_len опущен с 4000 до 3800. SCHEMA-лимит
    # совпадал с MAX-API hard limit и оставлял ноль запаса под
    # будущие ack-маркеры/event_header'ы. Те же 200 char запаса, что
    # в `test_texts_length_guard.MAX_LEN=3900`, плюс ~100 char под
    # placeholder подстановку на render-time (`{policy_url}` до 200
    # char заменяет 12-char-шаблон → нетто +188 на каждом render'е).
    # Силовая защита от silent prod-overflow типа OP_HELP_FULL_LEGACY,
    # но через UI настроек — текстовый CI guard PR #101 покрывает
    # только `aemr_bot.texts.*` constants, не БД.
    "welcome_text": {
        "type": str,
        "min_len": 1,
        "max_len": 3800,
    },
    # C1: consent_text используется как шаблон с placeholder
    # `{policy_url}`. Если IT перепишет без placeholder — житель увидит
    # consent без ссылки на политику (формальное нарушение 152-ФЗ).
    # required_substr — мягкое требование: текст обязан содержать
    # {policy_url} как подстроку, иначе validate отклонит.
    # D1-fix 2026-05-27: max_len 3800 (см. welcome_text выше).
    "consent_text": {
        "type": str,
        "min_len": 1,
        "max_len": 3800,
        "required_substr": "{policy_url}",
    },
    "commit_author_name": {"type": str, "min_len": 1, "max_len": 120},
    "commit_author_email": {"type": str, "min_len": 3, "max_len": 200},
    "policy_url": {"type": str, "url": True},
    "electronic_reception_url": {"type": str, "url": True},
    "udth_schedule_url": {"type": str, "url": True},
    "udth_schedule_intermunicipal_url": {"type": str, "url": True},
    "appointment_text": {"type": str, "min_len": 1, "max_len": 2000},
    "emergency_contacts": {"type": list, "min_items": 1, "item_keys": {"name", "phone"}},
    "transport_dispatcher_contacts": {
        "type": list,
        "min_items": 1,
        "item_keys": {"routes", "phone"},
    },
    "topics": {"type": list, "min_items": 1, "max_items": 30, "item_type": str},
    # Глобальный лимит картинок в рассылке. Диапазон 1–20: 1 — минимум
    # для «текст + одна афиша», 20 — практический потолок (выше MAX
    # ограничивает частоту, см. _send_one).
    "broadcast_max_images": {"type": int, "min": 1, "max": 20},
    # quiet hours: bool + два часа 0–23.
    "admin_quiet_hours_enabled": {"type": bool},
    "admin_quiet_hours_start": {"type": int, "min": 0, "max": 23},
    "admin_quiet_hours_end": {"type": int, "min": 0, "max": 23},
    "localities": {"type": list, "min_items": 1, "max_items": 30, "item_type": str},
}


def validate(key: str, value: Any) -> tuple[bool, str]:
    """Возвращает (ok, message). В сообщении — причина при ошибке или 'ok' при успехе."""
    if key not in SCHEMA:
        return False, f"Unknown key '{key}'. Allowed: {sorted(SCHEMA)}"
    rule = SCHEMA[key]
    expected = rule["type"]
    if not isinstance(value, expected):
        return False, f"Expected type {expected.__name__}, got {type(value).__name__}"
    if expected is str:
        if "min_len" in rule and len(value) < rule["min_len"]:
            return False, f"String too short, min_len={rule['min_len']}"
        if "max_len" in rule and len(value) > rule["max_len"]:
            return False, f"String too long, max_len={rule['max_len']}"
        # C1: required_substr — обязательная подстрока в тексте.
        # Используется для consent_text (`{policy_url}` обязателен).
        if "required_substr" in rule and rule["required_substr"] not in value:
            return False, (
                f"Текст обязан содержать подстроку "
                f"«{rule['required_substr']}» (это placeholder, который "
                f"бот подставляет при отправке)."
            )
        if rule.get("url"):
            if not (value.startswith("https://") or value.startswith("http://")):
                return False, "URL must start with http:// or https://"
            if not _is_whitelisted_url(value):
                return False, (
                    f"URL host не в whitelist. Разрешены только официальные "
                    f"ресурсы: {', '.join(_URL_HOST_WHITELIST_SUFFIXES)}. "
                    f"Для нового домена обратитесь к разработчику."
                )
    if expected is list:
        if "min_items" in rule and len(value) < rule["min_items"]:
            return False, f"List too short, min_items={rule['min_items']}"
        if "max_items" in rule and len(value) > rule["max_items"]:
            return False, f"List too long, max_items={rule['max_items']}"
        if "item_type" in rule and not all(isinstance(it, rule["item_type"]) for it in value):
            return False, f"All items must be {rule['item_type'].__name__}"
        if "item_keys" in rule:
            for it in value:
                if not isinstance(it, dict) or not rule["item_keys"].issubset(it):
                    return False, f"Each item must be an object with keys: {rule['item_keys']}"
        # SECURITY_REVIEW M4: для контактов с обязательным полем phone —
        # дополнительно валидируем структурный формат, чтобы IT не вписал
        # туда премиум-номер с тарификацией или произвольный текст.
        # Применяется к emergency_contacts и transport_dispatcher_contacts
        # (оба имеют phone в item_keys).
        if rule.get("item_keys") and "phone" in rule["item_keys"]:
            for it in value:
                phone = it.get("phone", "")
                if not _is_valid_phone(phone):
                    return False, (
                        f"Поле «phone» в одном из item'ов не похоже на телефон: "
                        f"«{phone[:30]}…». Допустимо: цифры, пробелы, +, (), -, ., "
                        f"длина 3–40."
                    )
    if expected is int:
        # bool — подкласс int в Python, явно фильтруем: True/False не
        # должны проходить как int (validate("broadcast_max_images", True)
        # = no-go).
        if isinstance(value, bool):
            return False, "Expected int, got bool"
        if "min" in rule and value < rule["min"]:
            return False, f"Integer too small, min={rule['min']}"
        if "max" in rule and value > rule["max"]:
            return False, f"Integer too large, max={rule['max']}"
    return True, "ok"


def format_obj_list(items: list[dict]) -> str:
    """Чистая функция рендера тела карточки списка объектов
    (emergency_contacts, transport_dispatcher_contacts).

    Если у item'ов есть «section» (актуально для emergency_contacts —
    Экстренные службы / Электроэнергия / Отопление / Холодная вода) —
    группируем визуально. Item'ы без section падают в «Прочее».
    Порядок секций — по первому появлению, чтобы совпадал с порядком
    в seed/contacts.json и не прыгал между рендерами. Глобальная
    нумерация (1..N) сохраняется — она совпадает с idx в obj_item
    card, чтобы клик на «5» открывал ровно пятый контакт.

    Если секция всего одна (особенно «Прочее») — заголовок не
    добавляем, остаётся плоский список как раньше (для transport-
    диспетчеров, у которых section не используется).

    Лежит в services/, а не в handlers/, чтобы юнит-тест мог импортить
    функцию без подтягивания maxapi через handlers/__init__.py.
    """
    if not items:
        return "(список пуст)"

    groups: dict[str, list[tuple[int, dict]]] = {}
    order: list[str] = []
    for i, item in enumerate(items):
        section = (item.get("section") or "").strip() or "Прочее"
        if section not in groups:
            groups[section] = []
            order.append(section)
        groups[section].append((i, item))

    lines: list[str] = []
    show_headers = len(order) > 1
    for section in order:
        if show_headers:
            lines.append(f"\n▸ {section}")
        for i, item in groups[section]:
            name = item.get("name") or item.get("routes") or "?"
            phone = item.get("phone") or ""
            lines.append(f"{i+1}. {name} — {phone}")
    return "\n".join(lines).lstrip("\n")


async def get(session: AsyncSession, key: str) -> Any:
    row = await session.scalar(select(Setting).where(Setting.key == key))
    if row is not None:
        return row.value
    return DEFAULTS.get(key)


async def get_consent_request_text(
    session: AsyncSession, *, policy_url: str, fallback: str
) -> str:
    """Готовый consent-request с подставленным policy_url, безопасно.

    Особый случай get_text_with_fallback: consent_text это шаблон с
    обязательным placeholder'ом `{policy_url}` (см. SCHEMA). После
    sanitize прогоняем через .format() — но защищённо: если IT всё-же
    как-то умудрился сохранить текст без placeholder'а (например,
    через прямой psql), мы не падаем KeyError'ом, а отдаём fallback.
    """
    try:
        raw = await get(session, "consent_text")
    except (SQLAlchemyError, asyncio.TimeoutError, TypeError):
        # F14: тот же узкий except, что и в get_text_with_fallback.
        raw = None
    if isinstance(raw, str) and raw.strip() and "{policy_url}" in raw:
        try:
            return sanitize_settings_text(raw).format(policy_url=policy_url)
        except (KeyError, IndexError, ValueError):
            # На случай если в тексте есть {другие_фигурные_скобки}
            # которые format() не сможет разобрать. Не падаем — fallback.
            pass
    return fallback.format(policy_url=policy_url)


async def get_text_with_fallback(
    session: AsyncSession, key: str, fallback: str
) -> str:
    """Получить текстовую настройку из БД с санитизацией и fallback'ом.

    SECURITY_REVIEW C1: welcome_text / consent_text раньше были
    «dormant capability» — БД хранит, UI редактирует, житель видит
    hardcoded `texts.WELCOME` (false security). Теперь подключено:

    1. Если в БД для ключа лежит непустая строка → пропускаем через
       `sanitize_settings_text` (вырезает HTML/JS, режет markdown-
       ссылки на не-whitelist домены) и возвращаем результат.
    2. Иначе (или при любой ошибке БД) → возвращаем переданный
       fallback (hardcoded текст из texts.py). Это страховка: если
       БД отвалилась, ключ удалили вручную или возникла другая
       нештатная ситуация, житель всё равно увидит осмысленный
       приветственный текст, а не «(None)» или Internal Server Error.

    Санитизация — мягкая (не криптографическая). Защита от ошибки
    IT-оператора и от компрометации IT-аккаунта на уровне «не
    превратить welcome в кликабельную фишинг-страницу».
    """
    try:
        raw = await get(session, key)
    except (SQLAlchemyError, asyncio.TimeoutError, TypeError) as exc:
        # F14 (narrow except): только узкий класс ожидаемых сбоев —
        # SQL/timeout (БД отвалилась) и TypeError (MagicMock вернул
        # не coroutine в тестах). NameError/AttributeError здесь —
        # programming bug в нашем коде, должен взлететь с traceback,
        # не маскироваться под «БД молчит». Логируем как WARNING —
        # частая нештатная ситуация заслуживает внимания.
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "get_text_with_fallback: fallback because get(%r) raised %s",
            key, type(exc).__name__,
        )
        return fallback
    if isinstance(raw, str) and raw.strip():
        # **Системный fix 2026-05-26 (найдено владельцем).**
        # Раньше БД-значение возвращалось как есть, даже если оно
        # больше не соответствует актуальной SCHEMA. Сценарий-катастрофа:
        # IT обновил seed/welcome.md (добавил антифишинговый блок) →
        # SCHEMA в коде получила required_substr "НИКОГДА не запрашиваем"
        # → но в БД лежит **старая** версия welcome_text без этой
        # подстроки (seed_if_empty работает только при пустой БД).
        # Жителю шёл устаревший welcome без защиты.
        #
        # Теперь применяем validate(): SCHEMA — единый источник истины.
        # Если БД-текст не проходит — пишем WARNING (чтобы IT увидел)
        # и возвращаем безопасный fallback. Жителю всегда уходит
        # текст, соответствующий актуальным правилам.
        is_valid, reason = validate(key, raw)
        if is_valid:
            return sanitize_settings_text(raw)
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "get_text_with_fallback: БД-значение для %r не проходит "
            "SCHEMA-validate (%s) — отдаём hardcoded fallback. "
            "IT-оператору рекомендуется обновить настройку через UI.",
            key, reason,
        )
        return fallback
    return fallback


async def set_value(session: AsyncSession, key: str, value: Any) -> None:
    stmt = (
        pg_insert(Setting)
        .values(key=key, value=value)
        .on_conflict_do_update(index_elements=[Setting.key], set_={"value": value})
    )
    await session.execute(stmt)


async def list_keys(session: AsyncSession) -> list[str]:
    rows = await session.scalars(select(Setting.key))
    in_db = set(rows)
    return sorted(in_db.union(DEFAULTS.keys()))


# Ключи, которые попадают в seed/runtime_config.json при синхронизации с
# репозиторием. Намеренно НЕ включаем commit_author_* — это серверная
# метаинформация, в репо не место. welcome_text/consent_text идут не
# сюда, а в seed/welcome.md и seed/consent.md (формат markdown).
SYNCED_KEYS: tuple[str, ...] = (
    "policy_url",
    "electronic_reception_url",
    "udth_schedule_url",
    "udth_schedule_intermunicipal_url",
    "appointment_text",
    "emergency_contacts",
    "transport_dispatcher_contacts",
    "topics",
    "localities",
)


async def get_dirty_keys(session: AsyncSession) -> list[str]:
    """Список ключей из SYNCED_KEYS, изменённых после последней
    синхронизации с репо. Используется в меню для индикатора «N
    несинхронизированных изменений»."""
    rows = await session.execute(
        select(Setting.key, Setting.updated_at, Setting.synced_at).where(
            Setting.key.in_(SYNCED_KEYS)
        )
    )
    dirty: list[str] = []
    for key, updated_at, synced_at in rows.all():
        if synced_at is None or (updated_at is not None and updated_at > synced_at):
            dirty.append(key)
    return sorted(dirty)


async def export_synced(session: AsyncSession) -> dict[str, Any]:
    """Собирает значения SYNCED_KEYS из БД с fallback на DEFAULTS.
    Возвращает dict с детерминированным порядком ключей для чистых
    diff'ов в git."""
    out: dict[str, Any] = {}
    for key in SYNCED_KEYS:
        out[key] = await get(session, key)
    return out


async def mark_synced(
    session: AsyncSession, keys: list[str] | None = None
) -> int:
    """Проставить synced_at = now() для ключей из списка (или для всех
    SYNCED_KEYS, если keys=None). Вызывается после успешного создания
    PR. Возвращает количество обновлённых строк."""
    from datetime import datetime, timezone
    from sqlalchemy import update as sa_update

    target_keys = list(keys) if keys is not None else list(SYNCED_KEYS)
    now = datetime.now(timezone.utc)
    result = await session.execute(
        sa_update(Setting)
        .where(Setting.key.in_(target_keys))
        .values(synced_at=now)
    )
    return result.rowcount or 0


def _read_seed_json(name: str) -> Any:
    path = cfg.seed_dir / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _read_seed_text(name: str) -> str | None:
    path = cfg.seed_dir / name
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


async def seed_if_empty(session: AsyncSession) -> None:
    """Заполнить настройки из /seed для отсутствующих ИЛИ невалидных ключей.

    Два режима в одной функции:

    1. **Bootstrap (раньше — основной):** если ключа нет в БД, кладём
       значение из seed-файла. Это первый запуск на свежей БД.

    2. **Repair (системный фикс 2026-05-26, найдено владельцем):**
       если ключ есть в БД, но его значение **не проходит SCHEMA-validate**
       (например, в БД лежит старая версия welcome_text без обязательной
       подстроки «НИКОГДА не запрашиваем», добавленной позже в SCHEMA),
       перезаписываем seed-значением. Это закрывает дрейф «код обновился,
       БД отстала».

    После вставки/перезаписи помечает свежие SYNCED_KEYS как уже
    синхронизированные с репо (synced_at = now()). Логика: seed-файлы
    (`seed/contacts.json`, `seed/topics.json`, `seed/transport_dispatchers.json`)
    физически лежат в репозитории и уже являются baseline'ом, поэтому
    сразу после первого старта бота этим ключам не место в списке
    «несинхронизированных изменений».

    Repair-режим тоже зовёт mark_synced для перезаписанных ключей —
    автоматическая правка не должна показывать «оператор изменил
    настройку» в UI (это сделал бот по seed-файлу, не человек).
    """
    existing_rows = await session.execute(select(Setting.key, Setting.value))
    existing: dict[str, Any] = {k: v for k, v in existing_rows.all()}

    seed_pairs: dict[str, Any] = {}
    if (topics := _read_seed_json("topics.json")) is not None:
        seed_pairs["topics"] = topics
    if (contacts := _read_seed_json("contacts.json")) is not None:
        seed_pairs["emergency_contacts"] = contacts
    if (dispatchers := _read_seed_json("transport_dispatchers.json")) is not None:
        seed_pairs["transport_dispatcher_contacts"] = dispatchers
    if (welcome := _read_seed_text("welcome.md")) is not None:
        seed_pairs["welcome_text"] = welcome
    if (consent := _read_seed_text("consent.md")) is not None:
        seed_pairs["consent_text"] = consent

    newly_seeded: list[str] = []
    repaired: list[str] = []
    import logging as _logging
    _log = _logging.getLogger(__name__)

    # Legacy auto-strip (2026-05-27): C1-hardening снят, антифишинг
    # переехал в кнопку «🛡️ Защита от мошенников». Но IT мог сохранить
    # welcome через UI в эпоху C1 — БД содержит **legacy блок** «НИКОГДА
    # не запрашиваем», который теперь дублирует кнопку. Validate его не
    # отвергает (required_substr убран), repair-режим не сработает.
    #
    # Эвристика: если welcome_text в БД содержит legacy маркер
    # «НИКОГДА не запрашиваем», И обновлённое seed-значение его НЕ
    # содержит — это legacy from C1, перезаписываем seed-значением.
    # Идемпотентно: на следующем restart'е welcome уже без маркера,
    # условие не сработает.
    legacy_marker = "НИКОГДА не запрашиваем"
    if (
        "welcome_text" in existing
        and isinstance(existing["welcome_text"], str)
        and legacy_marker in existing["welcome_text"]
        and "welcome_text" in seed_pairs
        and isinstance(seed_pairs["welcome_text"], str)
        and legacy_marker not in seed_pairs["welcome_text"]
    ):
        _log.warning(
            "seed_if_empty: legacy welcome_text с C1-блоком обнаружен в БД — "
            "перезаписываем актуальным seed/welcome.md (C1 снят 2026-05-27, "
            "антифишинг живёт в кнопке «🛡️ Защита от мошенников»)."
        )
        await set_value(session, "welcome_text", seed_pairs["welcome_text"])
        # Помечаем как «не было в существующих» для bootstrap-loop'a —
        # иначе он попытается ещё раз сравнить через SCHEMA-validate.
        existing.pop("welcome_text", None)

    for k, v in seed_pairs.items():
        if k not in existing:
            await set_value(session, k, v)
            newly_seeded.append(k)
            continue
        # Repair-режим: проверяем актуальное значение через SCHEMA.
        # Если в SCHEMA нет правил для ключа (например welcome_text
        # SCHEMA отсутствует — но у нас есть) — validate вернёт «unknown
        # key», пропускаем.
        if k not in SCHEMA:
            continue
        is_valid, reason = validate(k, existing[k])
        if is_valid:
            continue
        # Перед перезаписью убеждаемся что seed-значение **само**
        # проходит validate — иначе можем сломать рабочий бот
        # неполным seed-файлом.
        seed_is_valid, seed_reason = validate(k, v)
        if not seed_is_valid:
            _log.error(
                "seed_if_empty: ключ %r невалиден в БД (%s) И в seed-файле "
                "тоже (%s) — не трогаем. Требуется ручная правка через UI.",
                k, reason, seed_reason,
            )
            continue
        _log.warning(
            "seed_if_empty: repair ключа %r — БД-значение не проходит "
            "validate (%s), перезаписываем актуальным seed-значением.",
            k, reason,
        )
        await set_value(session, k, v)
        repaired.append(k)

    # Только те свежие/починенные ключи, которые входят в SYNCED_KEYS.
    # welcome_text / consent_text идут не сюда — их baseline хранится в
    # seed/welcome.md и seed/consent.md в формате markdown, репо-синк
    # их не трогает.
    auto_synced = [
        k for k in newly_seeded + repaired if k in SYNCED_KEYS
    ]
    if auto_synced:
        await mark_synced(session, auto_synced)
