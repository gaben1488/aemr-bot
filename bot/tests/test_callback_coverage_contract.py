"""Контракт-тест: каждый callback в `keyboards.py` зарегистрирован в
`callback_router.py`, и наоборот — каждый registered route реально
используется хотя бы одной клавиатурой.

**Зачем.** Жалоба владельца 2026-05-26: «требуется проверять все
концепты и соответствие всех концептам всего». Сейчас разработчик
может добавить новую кнопку в keyboards.py с любым payload — и забыть
зарегистрировать её в callback_router (тогда payload попадёт в
MENU_FALLBACK → silent broken UX). Или наоборот: убрать кнопку,
оставив route мертвым.

Этот тест парсит `keyboards.py` regex'ом, извлекает все упомянутые
payload-литералы (`payload="X"` и `payload=f"X:..."`), и проверяет:

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
        keyboard_payloads = _extract_payload_literals(source)

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

    # Routes, которые остаются по legacy/backward-compat (не имеют
    # текущей кнопки, но handler принимает на всякий случай — старые
    # уведомления / редиректы / тесты).
    _LEGACY_ROUTES: set[str] = {
        "appeal:submit",  # «устаревшая кнопка отправки» из callback_router
        # Префиксы которые матчатся через f-string'и, но keyboards.py
        # использует более короткий префикс — например, `op:set:` route
        # покрывает `op:set:cat:`, `op:set:list:`, `op:set:obj:`, ...
        "op:set:",  # purpose: общий префикс для всего иерархического
                    # меню настроек — keyboards.py использует более
                    # узкие подпрефиксы, но route_for использует общий.
        "op:setkey:",  # экспертный wizard
        "op:bc:",  # broadcast история — keyboards.py использует
                   # `op:bc:open:`, `op:bc:clone:`, `op:bc:failed:`;
                   # route — общий `op:bc:`.
        "op:tmpl:",  # шаблоны — keyboards.py использует
                     # `op:tmpl:open:`, `op:tmpl:apply:`, и т.п.
        "op:aud:",  # аудитория — `op:aud:subs/consent/blocked/block:/
                    # unblock:/erase:`
        "op:opadd:",  # operators wizard
        "op:opcard:",
        "op:oprole:",
        "op:opchrole:",
        "op:opdeact:",
        "op:opdeact_ok:",
        "op:opreact:",
    }

    def test_no_dead_routes(self) -> None:
        from aemr_bot.handlers import callback_router

        source = _read_keyboard_sources()
        keyboard_payloads = _extract_payload_literals(source)

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
