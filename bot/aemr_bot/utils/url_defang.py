"""Экранирование URL'ов («defang») для безопасного показа в admin-чате.

SECURITY: житель в обращении или followup может прислать кликабельную
фишинговую ссылку. Оператор открывает карточку в MAX-клиенте и **видит
кликабельный URL** — один тап = вход на phishing-страницу с
правами оператора (cookies браузера, авто-вход в Госуслуги и т.п.).

Решение: перед отображением жителю входящего текста в admin-чате —
вставляем **невидимый разделитель** между схемой и `://`. Visually
текст не меняется (оператор по-прежнему видит `https://elizovomr.ru`),
но MAX-парсер уже не распознаёт это как URL и не делает auto-link.
Если оператор реально хочет открыть ссылку — он копирует её, видит
невидимый символ при вставке в браузер и убирает.

Используется в admin-карточке (summary + timeline), в followup-карточке,
в /export файлах. НЕ используется в operator reply к жителю (там URL
жителю нужен кликабельный — но whitelist гарантирует, что URL только
на гос-домены).

Зачем не «замена https → hxxps» (классический IOC-defang):
- visually шумно для оператора, который привык к URL'ам;
- наша задача — не сделать URL читаемым как «опасный», а сделать
  его **некликабельным**. ZWSP решает это без изменения внешнего вида.
"""
from __future__ import annotations

# Zero-width space (U+200B) — невидимый символ, разрывает auto-linkify
# в большинстве чат-клиентов (Telegram, MAX, Slack, Discord, WhatsApp).
# Не используем U+200D (ZWJ) — он, наоборот, склеивает символы в
# некоторых рендерах. ZWSP — самый предсказуемый no-op для рендера.
_ZWSP = "​"


def defang_url_in_text(text: str) -> str:
    """Вставить ZWSP между схемой и `://` для всех URL в тексте.

    `https://attacker.com` → `https<ZWSP>://attacker.com`. Visually
    идентично, но MAX-парсер не auto-linkify'ит — оператор не может
    тапнуть и случайно открыть phishing.

    Работает идемпотентно: повторный вызов на уже defang'нутом тексте
    не добавляет второй ZWSP (т.к. между `https` и `://` уже стоит
    разделитель, regex `https?://` не матчит).

    Покрывает только ASCII-схемы `http://` и `https://`. Quasi-URL'ы
    с unicode-омоглифом (`һttps://`) бот в принципе не должен показывать
    оператору — они блокируются на входе через find_non_whitelisted_urls
    (см. settings_store.F9-fix); но если каким-то путём прошёл —
    выглядит как plain text, не auto-linkify'ится без явной схемы.
    """
    if not text:
        return text
    # Простая замена строки: `://` → `<ZWSP>://` где этому
    # предшествует http/https. Используем re для context-aware
    # подстановки.
    import re
    return re.sub(
        r"\b(https?)://",
        r"\1" + _ZWSP + "://",
        text,
        flags=re.IGNORECASE,
    )


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

    Используется в admin_card._maybe_url_warning — чтобы добавлять
    warning «⚠️ содержит ссылку» только когда есть что показывать.
    Не дублирует extract_urls из settings_store (та для исходящего
    whitelist'а), здесь — узкая проверка на http/https.
    """
    if not text:
        return False
    import re
    return bool(re.search(r"\bhttps?://", text, flags=re.IGNORECASE))
