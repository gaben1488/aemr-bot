"""Контракт-тест: каждый callback в `keyboards.py` зарегистрирован в
`callback_router.py`, и наоборот — каждый registered route реально
используется хотя бы одной клавиатурой.

**Зачем.** Жалоба владельца 2026-05-26: «требуется проверять все
концепты и соответствие всех концептам всего». Сейчас разработчик
может добавить новую кнопку в keyboards.py с любым payload — и забыть
зарегистрировать её в callback_router (тогда payload попадёт в
MENU_FALLBACK → silent broken UX). Или наоборот: убрать кнопку,
оставив route мертвым.

Этот тест собирает все payload'ы, упомянутые в UI, двумя путями:

* **Сырые литералы** — `payload="X"` / `payload=f"X:..."`
  (`_extract_payload_literals`). Остаются у не до конца мигрированных
  модулей (например, role-picker в `wizard_keyboards.py`).
* **Слой билдеров** — `payload=cp.MENU_MAIN` (EXACT-константа) и
  `payload=cp.op_tmpl(f"open:{id}")` (PREFIX-билдер) после миграции
  P1.2. Их сырой regex не видит, поэтому `_resolve_cp_payloads`
  импортирует :mod:`handlers.callback_payloads` и отображает каждое
  обращение `cp.*` обратно в строку/префикс, который оно эмитит.

Объединение этих двух множеств и есть «payload'ы UI». Дальше проверяем:

1. **Coverage**: каждый payload из UI имеет матч в EXACT_ROUTES (для
   точных) или в PREFIX_ROUTES (для f-string'овых вида `op:open_card:`).
2. **Orphan-проверка**: каждый route в EXACT/PREFIX действительно
   соответствует хотя бы одному payload в UI (нет dead routes).

**Что НЕ проверяет** (намеренно):
- Что каждый callback имеет handler. Это уже неявно покрыто:
  `dispatch_admin_callback` валит `_EXACT`/`_PREFIX_*` через словарь;
  отсутствие handler → MENU_FALLBACK или silent fail. Можно добавить
  третий тест: «каждый OPERATOR_ADMIN route → handler в
  admin_callback_dispatch._EXACT/_PREFIX_*». TODO в отдельный тест.
- Семантику payload (что `op:close:5` ведёт к закрытию обращения #5).
  Это покрывается handler-тестами.

**Допуски (whitelist):**
- `appeals:page:noop` — это явный «отключённый» payload для disabled
  pagination (визуально кнопка есть, но не делает ничего). Не нужен
  route.
- Эти исключения собраны в `_IGNORED_PAYLOADS` ниже.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest


# Источник истины (импорт лениво в каждом тесте, чтобы тест-файл сам
# падал с понятным импорт-ошибкой, а не на module-load).
#
# После Cluster A (2026-05-27, PR #128) keyboards.py — compatibility
# facade на 25 строк (`from aemr_bot.ui.* import *`). Реальные payload-
# литералы живут в 5 доменных модулях под `aemr_bot/ui/`. Сканируем оба
# слоя: и facade, и ui/*_keyboards.py — на случай если когда-то добавят
# inline-payload в facade или появится новый ui-модуль.
KEYBOARDS_PATH = (
    Path(__file__).parent.parent / "aemr_bot" / "keyboards.py"
)
_UI_DIR = Path(__file__).parent.parent / "aemr_bot" / "ui"


def _read_keyboard_sources() -> str:
    """Считать исходники всех UI-модулей в одну строку для парсинга.

    Включает `keyboards.py` (facade) + все `aemr_bot/ui/*_keyboards.py`.
    Если ui-директории ещё нет (старый код до Cluster A) — возвращает
    только facade. Это делает контракт-тесты forward- и backward-
    совместимыми с обоими layout'ами.
    """
    parts: list[str] = []
    if KEYBOARDS_PATH.exists():
        parts.append(KEYBOARDS_PATH.read_text(encoding="utf-8"))
    if _UI_DIR.exists():
        for module_file in sorted(_UI_DIR.glob("*_keyboards.py")):
            parts.append(module_file.read_text(encoding="utf-8"))
    return "\n".join(parts)


# Payload'ы, которые не должны иметь route в callback_router:
# - `appeals:page:noop` — заглушка для disabled-pagination кнопки.
# - Empty payloads (если внезапно появятся) — invalid.
_IGNORED_PAYLOADS = {
    "appeals:page:noop",
}


def _extract_payload_literals(source: str) -> set[str]:
    """Вытащить все `payload="..."` и `payload=f"..."` литералы,
    включая f-string'и, присвоенные через переменную (conditional payload).

    Возвращает:
    - точные payload'ы (без `{` интерполяции) — как есть;
    - f-string'и с `{var}` — как префикс до первого `{` (например,
      `op:open_card:{appeal_id}` → `op:open_card:`);
    - conditional payload pattern `var = f"X:..." if ... else f"Y:..."` —
      обе ветки распарсиваются (пример: `block_payload` в
      `appeal_admin_actions`, где payload зависит от is_blocked).
    """
    # Direct literals: `payload="X"` and `payload=f"X:..."`.
    direct_pattern = re.compile(r'payload=f?"([^"]+)"')
    # Conditional assignment: `varname_payload = f"X:..." if ... else f"Y:..."`.
    # Limit to vars with "payload" in name — иначе ловим text-переменные.
    # Захватывает обе ветки через два f-string'а на одной строке.
    cond_pattern = re.compile(
        r'\w*payload\w*\s*=\s*\(?\s*f?"([^"]+)"\s+if\s+[^"]+\s+else\s+f?"([^"]+)"'
    )

    out: set[str] = set()

    def _add_literal(literal: str) -> None:
        if not literal:
            return
        if "{" in literal:
            prefix = literal.split("{", 1)[0]
            if prefix:
                out.add(prefix)
        else:
            out.add(literal)

    for match in direct_pattern.finditer(source):
        _add_literal(match.group(1))
    for match in cond_pattern.finditer(source):
        _add_literal(match.group(1))
        _add_literal(match.group(2))
    return out


# Маркер-зонд для определения префикса билдера. Не может встретиться внутри
# реального callback-префикса (NUL-байты), поэтому «голова» строки до первого
# зонда — это в точности фиксированный префикс, который билдер приклеивает.
_BUILDER_PROBE = "\x00\x00CP_PROBE\x00\x00"


def _builder_emitted_prefix(fn) -> str | None:
    """Фиксированный префикс, который эмитит payload-билдер из `cp`.

    Все билдеры в :mod:`handlers.callback_payloads` строят строку как
    ``PREFIX_X + <хвосты>`` (см. сам модуль). Вызов билдера с зондом во
    всех позиционных аргументах даёт ``PREFIX_X + зонд[ + ":" + зонд...]``;
    берём голову до первого зонда — это и есть ``PREFIX_X``.

    Подход не зависит от соглашения об именах: если билдер переименуют
    или сменят его ``PREFIX_*``-константу, зонд всё равно вернёт актуальный
    эмитируемый префикс. Билдеры — чистые функции склейки строк, побочных
    эффектов у вызова нет. ``None`` — если интроспекция/вызов не удались
    (тогда маршрут просто не пометится этим путём и проявится как обычно).
    """
    params = [
        p
        for p in inspect.signature(fn).parameters.values()
        if p.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    try:
        produced = fn(*([_BUILDER_PROBE] * len(params)))
    except Exception:
        return None
    if not isinstance(produced, str):
        return None
    head = produced.split(_BUILDER_PROBE, 1)[0]
    return head or None


def _resolve_cp_payloads(source: str) -> set[str]:
    """Распознать обращения к слою билдеров ``cp`` и вернуть payload'ы.

    После миграции P1.2 клавиатуры строят callback не сырыми f-строками,
    а типизированными вызовами из :mod:`handlers.callback_payloads`
    (импортируется как ``cp``):

    * ``payload=cp.MENU_MAIN`` → значение EXACT-константы (``menu:main``);
    * ``payload=cp.PREFIX_APPEALS_PAGE`` → значение PREFIX-константы
      (``appeals:page:``);
    * ``payload=cp.op_tmpl(f"open:{id}")`` → префикс билдера (``op:tmpl:``),
      которого достаточно, чтобы пометить PREFIX-маршрут используемым.

    Источник истины — сам импортированный модуль (а не захардкоженная
    таблица), поэтому новая константа/билдер учитывается автоматически.
    Сырые литералы здесь НЕ ищем — это забота
    :func:`_extract_payload_literals`; вызывающий объединяет оба множества.
    """
    from aemr_bot.handlers import callback_payloads as cp

    # Alias из самого импорта (на практике везде ``cp``); дефолт — на случай
    # сниппета без import-строки (self-check).
    aliases = set(re.findall(r"callback_payloads\s+as\s+(\w+)", source)) or {"cp"}

    # Имя → строка. EXACT- и PREFIX-константы — все верхнерегистровые str.
    const_values = {
        name: value
        for name, value in vars(cp).items()
        if name.isupper() and isinstance(value, str)
    }
    # Имя билдера → эмитируемый префикс. Только функции, объявленные в модуле
    # (исключает импортированные имена и класс CallbackGroup).
    builder_prefixes: dict[str, str] = {}
    for name, obj in vars(cp).items():
        if inspect.isfunction(obj) and obj.__module__ == cp.__name__:
            prefix = _builder_emitted_prefix(obj)
            if prefix:
                builder_prefixes[name] = prefix

    ref_pattern = re.compile(
        r"(?<![\w.])(?:"
        + "|".join(re.escape(a) for a in aliases)
        + r")\.(\w+)"
    )

    out: set[str] = set()
    for name in ref_pattern.findall(source):
        if name in const_values:
            out.add(const_values[name])
        elif name in builder_prefixes:
            out.add(builder_prefixes[name])
    return out


def _is_covered_by_routes(
    payload: str,
    exact_routes: set[str],
    prefix_routes: set[str],
) -> bool:
    """Покрыт ли payload точным или префиксным маршрутом."""
    if payload in exact_routes:
        return True
    for prefix in prefix_routes:
        if payload.startswith(prefix):
            return True
    return False


class TestKeyboardsCoverContractRouter:
    """Каждый payload из keyboards.py должен иметь route в callback_router."""

    def test_all_keyboard_payloads_have_routes(self) -> None:
        from aemr_bot.handlers import callback_router

        source = _read_keyboard_sources()
        keyboard_payloads = _extract_payload_literals(source) | _resolve_cp_payloads(source)

        exact_routes = {r.pattern for r in callback_router.EXACT_ROUTES}
        prefix_routes = {r.pattern for r in callback_router.PREFIX_ROUTES}

        uncovered: list[str] = []
        for payload in sorted(keyboard_payloads):
            if payload in _IGNORED_PAYLOADS:
                continue
            if not _is_covered_by_routes(payload, exact_routes, prefix_routes):
                uncovered.append(payload)

        assert not uncovered, (
            "Найдены callback payload'ы в keyboards.py без route в "
            "callback_router.py — silent fallthrough в MENU_FALLBACK:\n"
            + "\n".join(f"  - {p}" for p in uncovered)
        )


class TestRouterRoutesAreUsedByKeyboards:
    """Каждый route в callback_router должен использоваться UI'ом.

    Защита от dead routes: если кнопка удалена, route остаётся «по
    инерции» и сбивает с толку. Если route намеренно сохранён для
    backward compatibility — добавить в `_LEGACY_ROUTES` ниже.
    """

    # Routes, которые остаются по legacy/backward-compat: handler их
    # принимает (старое уведомление / редирект / тест), но НИ ОДНА кнопка
    # их не строит — ни сырым литералом, ни через билдер `cp`.
    #
    # Раньше тут лежали и общие префиксы `op:set:` / `op:bc:` / `op:tmpl:` /
    # `op:aud:` / `op:op*:` — не потому что они мёртвые, а потому что сырой
    # regex не видел их f-string-билдеров и они ложно срабатывали. Теперь
    # `_resolve_cp_payloads` понимает слой билдеров, эти префиксы покрыты
    # по-настоящему, и держать их в whitelist'е значило бы зря выключать
    # для них orphan-проверку. Поэтому whitelist сжат до единственного
    # действительно бескнопочного маршрута.
    _LEGACY_ROUTES: set[str] = {
        "appeal:submit",  # «устаревшая кнопка отправки»: route есть,
                          # кнопки нет (исторический payload из старых
                          # сообщений). Подтверждено grep'ом по `ui/`.
    }

    def test_no_dead_routes(self) -> None:
        from aemr_bot.handlers import callback_router

        source = _read_keyboard_sources()
        keyboard_payloads = _extract_payload_literals(source) | _resolve_cp_payloads(source)

        # Для exact-routes: должен быть точный матч в keyboards.
        unused_exact: list[str] = []
        for route in callback_router.EXACT_ROUTES:
            if route.pattern in self._LEGACY_ROUTES:
                continue
            if route.pattern not in keyboard_payloads:
                unused_exact.append(route.pattern)

        # Для prefix-routes: должен быть payload, начинающийся с route.
        unused_prefix: list[str] = []
        for route in callback_router.PREFIX_ROUTES:
            if route.pattern in self._LEGACY_ROUTES:
                continue
            if not any(p.startswith(route.pattern) for p in keyboard_payloads):
                unused_prefix.append(route.pattern)

        unused = unused_exact + unused_prefix
        assert not unused, (
            "Dead routes в callback_router.py — не используются ни одной "
            "клавиатурой. Удалить или добавить в _LEGACY_ROUTES:\n"
            + "\n".join(f"  - {r}" for r in unused)
        )


class TestOperatorRoutesHaveHandlers:
    """Каждый OPERATOR_ADMIN/BROADCAST_ADMIN route в callback_router имеет
    handler в admin_callback_dispatch._EXACT / _PREFIX_ID / _PREFIX_RAW.

    Защита от «phantom route»: route задекларирован, но handler не
    зарегистрирован — payload приходит, ack делается, действие не
    выполняется → silent broken UX. Этот тест ловит drift сразу.

    Citizen routes (CITIZEN_FLOW / GEO_FLOW) НЕ проверяются —
    обрабатываются через `handlers/menu.py:handle_callback`
    fallthrough, который не имеет жёсткой declarative-таблицы.
    Можно расширить в будущем (TODO: similar contract test для
    menu._MENU_ROUTES / _PREFIX_HANDLERS).
    """

    def test_admin_routes_have_handlers_in_dispatch(self) -> None:
        from aemr_bot.handlers import admin_callback_dispatch as dispatch
        from aemr_bot.handlers import callback_router
        from aemr_bot.handlers.callback_router import CallbackGroup

        admin_groups = {
            CallbackGroup.OPERATOR_ADMIN,
            CallbackGroup.BROADCAST_ADMIN,
        }

        # Собрать handler-таблицы.
        exact_handlers = set(dispatch._EXACT.keys())
        prefix_id_handlers = {p for p, _ in dispatch._PREFIX_ID}
        prefix_raw_handlers = {p for p, _ in dispatch._PREFIX_RAW}
        prefix_handlers = prefix_id_handlers | prefix_raw_handlers

        missing: list[str] = []
        for route in callback_router.EXACT_ROUTES:
            if route.group not in admin_groups:
                continue
            if route.pattern not in exact_handlers:
                missing.append(f"EXACT {route.pattern} ({route.description})")

        for route in callback_router.PREFIX_ROUTES:
            if route.group not in admin_groups:
                continue
            # Route может быть «общий» префикс (например, `op:bc:`), а
            # handler'ы — гранулярные подпрефиксы (`op:bc:open:`,
            # `op:bc:clone:`, `op:bc:failed:`). Проверяем что есть хотя
            # бы один handler на самом route'е ИЛИ на его расширении.
            has_handler = (
                route.pattern in prefix_handlers
                or any(h.startswith(route.pattern) for h in prefix_handlers)
            )
            if not has_handler:
                missing.append(f"PREFIX {route.pattern} ({route.description})")

        assert not missing, (
            "OPERATOR_ADMIN/BROADCAST_ADMIN routes без handler'а в "
            "admin_callback_dispatch._EXACT/_PREFIX_*:\n"
            + "\n".join(f"  - {m}" for m in missing)
        )


class TestExtractPayloadLiteralsSelfCheck:
    """Sanity-check самой утилиты `_extract_payload_literals` — чтобы
    rewriting её не сломал контракт-тесты молча.
    """

    @pytest.mark.parametrize(
        "source,expected",
        [
            ('payload="menu:main"', {"menu:main"}),
            ('payload=f"op:open_card:{x}"', {"op:open_card:"}),
            (
                'payload="a"\npayload=f"b:{c}"\npayload="d"',
                {"a", "b:", "d"},
            ),
            ('payload=""', set()),  # пустой payload игнорируем
            ('payload="x"  # с комментарием', {"x"}),
        ],
    )
    def test_extract(self, source: str, expected: set[str]) -> None:
        assert _extract_payload_literals(source) == expected


class TestResolveCpPayloadsSelfCheck:
    """Sanity-check `_resolve_cp_payloads` — чтобы рефактор резолвера не
    сломал контракт-тесты молча. Использует РЕАЛЬНЫЕ имена из
    `callback_payloads` (резолвер импортирует сам модуль как источник
    истины), поэтому покрываем константы, билдеры, alias и границы.
    """

    @pytest.mark.parametrize(
        "source,expected",
        [
            # EXACT-константа → её строковое значение.
            ("payload=cp.MENU_MAIN", {"menu:main"}),
            ("payload=cp.CONSENT_YES", {"consent:yes"}),
            # PREFIX-константа → префикс (citizen: `cp.PREFIX_APPEALS_PAGE + "noop"`).
            ('payload=cp.PREFIX_APPEALS_PAGE + "noop"', {"appeals:page:"}),
            # Билдер с f-string-хвостом → эмитируемый префикс.
            ('payload=cp.op_tmpl(f"open:{x}")', {"op:tmpl:"}),
            # Билдер с int-аргументом → префикс (аргумент не важен).
            ("payload=cp.topic(idx)", {"topic:"}),
            # Двухаргументный билдер: префикс до первого аргумента.
            ('payload=cp.op_bc("open", bc.id)', {"op:bc:"}),
            # Несколько обращений объединяются в одно множество.
            ("cp.MENU_MAIN\ncp.op_close(5)", {"menu:main", "op:close:"}),
            # Неизвестный атрибут `cp` игнорируется (не падаем, не шумим).
            ("cp.totally_not_a_real_symbol", set()),
            # Обращение НЕ через alias `cp` игнорируется.
            ("foo.MENU_MAIN\nbc.id", set()),
            # Lookbehind: `xcp.` — это не наш alias, а хвост идентификатора.
            ("xcp.MENU_MAIN", set()),
        ],
    )
    def test_resolve(self, source: str, expected: set[str]) -> None:
        assert _resolve_cp_payloads(source) == expected

    def test_respects_import_alias(self) -> None:
        """Alias берётся из import-строки, не захардкожен в `cp`."""
        src = (
            "from aemr_bot.handlers import callback_payloads as kb\n"
            "payload=kb.CONSENT_YES"
        )
        assert _resolve_cp_payloads(src) == {"consent:yes"}

    def test_fires_on_real_sources(self) -> None:
        """Канарейка: на реальных клавиатурах резолвер обязан находить и
        EXACT-константу, и builder-префикс. Пустой результат означал бы,
        что резолвер «ослеп» (сломан alias/import) и контракт-тесты прошли
        бы ложно-зелёными — без этой проверки регрессию не заметить.
        """
        resolved = _resolve_cp_payloads(_read_keyboard_sources())
        assert "menu:main" in resolved  # EXACT-константа (citizen)
        assert "op:tmpl:" in resolved  # builder-префикс (broadcast/operator)
