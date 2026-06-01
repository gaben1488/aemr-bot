"""Контракт-тест: построители payload'ов ⟷ реестр маршрутов.

Цель — закрыть класс дрейфа «build vs registry»: гарантировать, что строки,
которые собирает :mod:`handlers.callback_payloads`, байт-в-байт ложатся на
паттерны :mod:`handlers.callback_router`. Если кто-то поправит f-строку в
билдере или переименует маршрут — этот тест покраснеет до прода.

Структура:

* (а) каждая EXACT-константа == зарегистрированный EXACT-паттерн, её
  ``route_for(...).group`` совпадает с группой реестра, и множество
  констант покрывает ВЕСЬ ``EXACT_ROUTES`` (ни одной лишней, ни одной
  забытой);
* (б) каждый PREFIX-билдер на примере аргумента даёт строку, начинающуюся
  ровно с зарегистрированного префикса, и ``route_for`` относит её к верной
  группе;
* (в) round-trip: ``parse_int_tail(builder(N), prefix) == N`` для билдеров с
  числовым хвостом.
"""
from __future__ import annotations

import pytest

from aemr_bot.handlers import callback_payloads as cp
from aemr_bot.handlers import callback_router as cr


def _exact_constant_values() -> dict[str, str]:
    """UPPER_SNAKE строковые атрибуты модуля, не являющиеся PREFIX_*."""
    out: dict[str, str] = {}
    for name in dir(cp):
        if not name.isupper() or name.startswith("PREFIX_"):
            continue
        value = getattr(cp, name)
        if isinstance(value, str):
            out[name] = value
    return out


def _registry_group(pattern: str) -> str:
    """Группа маршрута из реестра по точному паттерну (EXACT или PREFIX)."""
    for route in (*cr.EXACT_ROUTES, *cr.PREFIX_ROUTES):
        if route.pattern == pattern:
            return str(route.group)
    raise AssertionError(f"паттерн {pattern!r} не найден в реестре")


# Пример аргумента для каждого PREFIX-билдера + ожидаемый префикс.
# Числовые билдеры помечены int_tail=True для round-trip проверки (в).
# Это единственная «ручная» таблица; её полноту против модуля и реестра
# стерегут тесты ниже, так что забытый билдер не пройдёт молча.
_BUILDER_CASES: tuple[tuple[object, tuple, str, bool], ...] = (
    # CITIZEN_FLOW — числовой хвост
    (cp.locality, (3,), cp.PREFIX_LOCALITY, True),
    (cp.topic, (5,), cp.PREFIX_TOPIC, True),
    (cp.appeal_show, (42,), cp.PREFIX_APPEAL_SHOW, True),
    (cp.appeal_followup, (42,), cp.PREFIX_APPEAL_FOLLOWUP, True),
    (cp.appeal_repeat, (42,), cp.PREFIX_APPEAL_REPEAT, True),
    (cp.appeal_atts, (42,), cp.PREFIX_APPEAL_ATTS, True),
    (cp.appeals_page, (2,), cp.PREFIX_APPEALS_PAGE, True),
    # BROADCAST_ADMIN — числовой хвост
    (cp.broadcast_stop, (7,), cp.PREFIX_BROADCAST_STOP, True),
    (cp.broadcast_cancel_cooldown, (7,), cp.PREFIX_BROADCAST_CANCEL_COOLDOWN, True),
    # BROADCAST_ADMIN — строковый хвост
    (cp.op_tmpl, ("list",), cp.PREFIX_OP_TMPL, False),
    (cp.op_bc, ("open", 9), cp.PREFIX_OP_BC, False),
    # OPERATOR_ADMIN — строковый хвост
    (cp.op_aud, ("subs",), cp.PREFIX_OP_AUD, False),
    # OPERATOR_ADMIN — числовой хвост (id обращения)
    (cp.op_reply, (11,), cp.PREFIX_OP_REPLY, True),
    (cp.op_replyint, (11,), cp.PREFIX_OP_REPLYINT, True),
    (cp.op_reopen, (11,), cp.PREFIX_OP_REOPEN, True),
    (cp.op_close, (11,), cp.PREFIX_OP_CLOSE, True),
    (cp.op_erase, (11,), cp.PREFIX_OP_ERASE, True),
    (cp.op_block, (11,), cp.PREFIX_OP_BLOCK, True),
    (cp.op_unblock, (11,), cp.PREFIX_OP_UNBLOCK, True),
    (cp.op_atts, (11,), cp.PREFIX_OP_ATTS, True),
    (cp.op_open_card, (11,), cp.PREFIX_OP_OPEN_CARD, True),
    # OPERATOR_ADMIN — строковый хвост (мастер операторов)
    (cp.op_opadd, ("list",), cp.PREFIX_OP_OPADD, False),
    # OPERATOR_ADMIN — числовой хвост (max_user_id)
    (cp.op_opcard, (123,), cp.PREFIX_OP_OPCARD, True),
    (cp.op_oprole, (123,), cp.PREFIX_OP_OPROLE, True),
    (cp.op_opdeact, (123,), cp.PREFIX_OP_OPDEACT, True),
    (cp.op_opdeact_ok, (123,), cp.PREFIX_OP_OPDEACT_OK, True),
    (cp.op_opreact, (123,), cp.PREFIX_OP_OPREACT, True),
    # OPERATOR_ADMIN — двусегментный / строковый хвост
    (cp.op_opchrole, (123, "it"), cp.PREFIX_OP_OPCHROLE, False),
    (cp.op_setkey, ("welcome_text",), cp.PREFIX_OP_SETKEY, False),
    (cp.op_set, ("cat:texts",), cp.PREFIX_OP_SET, False),
)


# ── (а) EXACT-константы ───────────────────────────────────────────────────

class TestExactConstants:
    def test_every_constant_is_a_registered_exact_pattern(self) -> None:
        exact_patterns = {r.pattern for r in cr.EXACT_ROUTES}
        for name, value in _exact_constant_values().items():
            assert value in exact_patterns, (
                f"{name}={value!r} нет среди EXACT_ROUTES реестра"
            )

    def test_constant_group_matches_registry(self) -> None:
        for name, value in _exact_constant_values().items():
            route = cr.route_for(value)
            assert str(route.group) == _registry_group(value), (
                f"{name}={value!r}: группа {route.group} != реестр"
            )
            # route_for должен находить именно EXACT-ветку, не fallback.
            assert route.group != cr.CallbackGroup.MENU_FALLBACK

    def test_constants_cover_all_exact_routes(self) -> None:
        # Зеркало полное: ни одного забытого EXACT-маршрута, ни одного
        # лишнего значения-константы.
        const_values = set(_exact_constant_values().values())
        exact_patterns = {r.pattern for r in cr.EXACT_ROUTES}
        assert const_values == exact_patterns

    def test_constant_count_mirrors_registry(self) -> None:
        # Источник истины — реестр. Не магическое число: сколько EXACT
        # маршрутов, столько и констант. (На 2026-06-01 их 54.)
        assert len(_exact_constant_values()) == len(cr.EXACT_ROUTES)

    def test_module_group_enum_mirrors_router(self) -> None:
        # CallbackGroup задублирован в модуле — значения обязаны совпасть.
        assert {g.value for g in cp.CallbackGroup} == {
            g.value for g in cr.CallbackGroup
        }


# ── (б) PREFIX-билдеры ────────────────────────────────────────────────────

class TestPrefixBuilders:
    def test_prefix_constants_match_registry(self) -> None:
        # Каждый PREFIX_* модуля == зарегистрированный префикс, и наоборот.
        module_prefixes = {
            getattr(cp, n) for n in dir(cp) if n.startswith("PREFIX_")
        }
        registry_prefixes = {r.pattern for r in cr.PREFIX_ROUTES}
        assert module_prefixes == registry_prefixes

    @pytest.mark.parametrize("case", _BUILDER_CASES, ids=lambda c: c[0].__name__)
    def test_builder_output_starts_with_registered_prefix(self, case) -> None:
        builder, args, prefix, _int_tail = case
        result = builder(*args)
        # Префикс зарегистрирован в реестре.
        assert any(
            r.pattern == prefix for r in cr.PREFIX_ROUTES
        ), f"{prefix!r} нет в PREFIX_ROUTES"
        # Строка начинается ровно с него и имеет непустой хвост.
        assert result.startswith(prefix)
        assert len(result) > len(prefix)

    @pytest.mark.parametrize("case", _BUILDER_CASES, ids=lambda c: c[0].__name__)
    def test_builder_routes_to_registered_group(self, case) -> None:
        builder, args, prefix, _int_tail = case
        result = builder(*args)
        route = cr.route_for(result)
        assert str(route.group) == _registry_group(prefix)
        assert route.pattern == prefix
        assert route.group != cr.CallbackGroup.MENU_FALLBACK

    def test_all_prefix_routes_have_a_builder_case(self) -> None:
        # Полнота: на каждый PREFIX-маршрут реестра есть кейс билдера.
        covered = {prefix for _b, _a, prefix, _t in _BUILDER_CASES}
        registry_prefixes = {r.pattern for r in cr.PREFIX_ROUTES}
        assert covered == registry_prefixes

    def test_builder_count_mirrors_registry(self) -> None:
        # Один билдер-кейс на каждый PREFIX-маршрут реестра, без магического
        # числа. (На 2026-06-01 их 30.)
        assert len(_BUILDER_CASES) == len(cr.PREFIX_ROUTES)


# ── (в) round-trip для числовых хвостов ───────────────────────────────────

class TestIntTailRoundTrip:
    @pytest.mark.parametrize(
        "case",
        [c for c in _BUILDER_CASES if c[3]],
        ids=lambda c: c[0].__name__,
    )
    def test_parse_int_tail_recovers_argument(self, case) -> None:
        builder, args, prefix, _int_tail = case
        # У числовых билдеров id/индекс — единственный аргумент.
        (value,) = args
        payload = builder(value)
        assert cr.parse_int_tail(payload, prefix) == value

    @pytest.mark.parametrize(
        "case",
        [c for c in _BUILDER_CASES if c[3]],
        ids=lambda c: c[0].__name__,
    )
    def test_parse_int_tail_zero_and_large(self, case) -> None:
        builder, _args, prefix, _int_tail = case
        for value in (0, 999_999):
            assert cr.parse_int_tail(builder(value), prefix) == value
