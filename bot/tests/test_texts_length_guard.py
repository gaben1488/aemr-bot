"""Превентивный гард: ни одна str-константа в `aemr_bot.texts` после
форматирования не должна превышать MAX-API hard limit 4000 символов.

**Зачем.** 2026-05-27 жалоба владельца: тап «📋 Памятка оператора»
падал в проде с `ValueError: text должен быть меньше 4000 символов` —
константа `OP_HELP_FULL_LEGACY` разрослась до ~8230 char. Это
бесшумный fail: бот не отправлял ничего, в чате тишина, ошибка только
в docker logs.

После split'a OP_HELP на 2 экрана (OP_HELP_MAIN + OP_HELP_SECURITY)
текущий main чистый, но без guard'а любая будущая правка может
вернуть overflow. Этот тест валит CI на любом attempt'е такой
регрессии.

**Метод.** Итерируем по модулю `aemr_bot.texts`, для каждой публичной
str-константы:
- Если содержит `{...}`-плейсхолдеры → форматируем с типичными
  значениями (`answer_limit=4000`, `number=1`, `max_user_id=42`, и т.п.).
  Список значений ниже — расширяется при появлении новых placeholder'ов.
- Если без плейсхолдеров → используем как есть.
Assert `len(formatted) <= MAX_LEN` (4000, с запасом — MAX-API лимит
ровно 4000, наш guard 3900 даёт буфер на ack-маркеры и event_header).
"""
from __future__ import annotations


MAX_LEN = 3900  # с запасом перед hard limit MAX-API 4000.

# Типичные значения для всех известных placeholder'ов в текстах.
# Расширяется при появлении новых полей в .format(...). Берём
# реалистичные размеры:
# - `answer_limit` (cfg.answer_max_chars) — обычно 4000, но в тестах
#   ставим max разумное.
# - `number` — id обращения, до 6 цифр.
# - `max_user_id` — MAX user id, обычно 9 цифр.
# - `phone` — телефон, 12 символов.
# - `name` — имя оператора, до 50 char.
# - `topic`, `address`, `locality` — текстовые поля обращения.
# - `policy_url`, `policy_url_or_link` — URL до 200 char.
# - `created_at` — отформатированная дата.
# - `failed`, `total`, `delivered` — числа.
# - `limit`, `actual`, `len` — размеры контента.
_PLACEHOLDER_VALUES = {
    "answer_limit": 4000,
    "number": 999999,  # макс реалистичный id
    "max_user_id": 999999999,
    "phone": "+79991234567",
    "name": "Иванов Иван Иванович (длинное имя оператора 50 символов)",
    "topic": "Уличное освещение и дворовая территория",
    "address": "г. Елизово, ул. Ленина, д. 13, кв. 145",
    "locality": "Елизовское городское поселение",
    "policy_url": "https://elizovomr.ru/policy/personal-data-processing.pdf",
    "policy_url_or_link": "https://elizovomr.ru/p/personal-data.pdf",
    "created_at": "27.05.2026 12:34",
    "failed": 42,
    "total": 999,
    "delivered": 957,
    "limit": 4000,
    "actual": 5500,
    "len": 1234,
    "key": "welcome_text",
    "broadcast_id": 999,
    "appeal_id": 999999,
    "url": "https://elizovomr.ru/some/long/path/to/document.pdf",
    "reason": "Не валидно по SCHEMA — поле «phone» не похоже на телефон.",
    "error": "MAX API timeout после 30 секунд",
    "summary": "Краткое описание проблемы для жителя — до 200 символов.",
    "reply_text": "x" * 200,  # реалистичный ответ оператора
    "details": "Подробности об инциденте",
    "when": "27.05.2026 в 12:34 камчатского",
}


def _try_format(value: str) -> str:
    """Попытаться отформатировать строку с типичными плейсхолдерами.

    Если в шаблоне есть `{some_unknown_field}` — пропускаем (placeholder
    не вызовет overflow сам по себе, а реальное значение в проде
    подставляется тоже разумного размера). На случай KeyError используем
    `format_map` с дефолтным fallback'ом «UNKNOWN_FIELD».
    """

    class _DefaultDict(dict):
        def __missing__(self, key):
            # Возвращаем strigified имя плейсхолдера длиной ≤ 30 char
            # — заведомо безопасный proxy для unknown placeholder'ов.
            return f"[{key}]"

    try:
        return value.format_map(_DefaultDict(**_PLACEHOLDER_VALUES))
    except (ValueError, IndexError):
        # Битый format-string (например, `{` без закрытия) — возвращаем
        # raw, ассертим через сырую длину.
        return value


def _iter_text_constants():
    """Yield (name, value) для всех public str-констант в aemr_bot.texts."""
    from aemr_bot import texts

    for name in dir(texts):
        if name.startswith("_"):
            continue
        value = getattr(texts, name)
        if not isinstance(value, str):
            continue
        yield name, value


def test_all_text_constants_within_max_api_limit() -> None:
    """ВСЕ str-константы из `aemr_bot.texts` после форматирования
    должны укладываться в `MAX_LEN` (= 3900, с запасом перед hard
    limit MAX-API 4000).

    Это превентивная защита от регрессий типа `OP_HELP_FULL_LEGACY`
    (8230 char → silent ValueError в проде). При любой будущей правке
    `texts.py`, которая раздувает константу — CI валится с понятным
    сообщением «текст X превышает X char, нужен split на 2 экрана».
    """
    violations: list[tuple[str, int]] = []
    for name, value in _iter_text_constants():
        formatted = _try_format(value)
        if len(formatted) > MAX_LEN:
            violations.append((name, len(formatted)))

    assert not violations, (
        "Найдены константы в `aemr_bot.texts`, превышающие MAX-API "
        f"hard limit ({MAX_LEN} char с запасом перед 4000):\n"
        + "\n".join(
            f"  - {name}: {length} char (нужен split на 2 экрана)"
            for name, length in sorted(violations, key=lambda x: -x[1])
        )
        + "\n\nПример split'a: OP_HELP_FULL_LEGACY (8230 char) → "
        "OP_HELP_MAIN + OP_HELP_SECURITY с навигацией. См. PR #94."
    )


def test_typical_long_constants_examples() -> None:
    """Sanity-check: явно проверяем константы, которые в прошлом
    разрастались (OP_HELP*, WELCOME, SECURITY_INFO_TEXT). На регрессии
    основной тест поймает любую новую — а эти спот-проверки даёт
    быстрый сигнал «именно эта часть проекта снова раздулась»."""
    from aemr_bot import texts

    known_long = {
        "OP_HELP_MAIN": getattr(texts, "OP_HELP_MAIN", ""),
        "OP_HELP_SECURITY": getattr(texts, "OP_HELP_SECURITY", ""),
        "OP_HELP": getattr(texts, "OP_HELP", ""),
        "WELCOME": getattr(texts, "WELCOME", ""),
        "SECURITY_INFO_TEXT": getattr(texts, "SECURITY_INFO_TEXT", ""),
        "CITIZEN_REPLY_TEMPLATE": getattr(texts, "CITIZEN_REPLY_TEMPLATE", ""),
    }
    for name, value in known_long.items():
        if not value:
            continue
        formatted = _try_format(value)
        assert len(formatted) <= MAX_LEN, (
            f"{name}: {len(formatted)} char (нужен split на 2 экрана)"
        )
