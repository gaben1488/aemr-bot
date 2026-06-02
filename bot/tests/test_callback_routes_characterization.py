"""Характеризационная сетка ВСЕХ callback-маршрутов бота.

Это safety-net под будущее объединение трёх диспетчеров callback'ов в
единый `callback_router`. Сейчас payload-маршрутизация размазана по трём
точкам, и каждая имеет свой контракт «обработал / отдал дальше»:

  1. `handlers/appeal.py::_dispatch_citizen_callback` — воронка жителя
     (consent:*, addr:*, geo:*, locality:, topic:, cancel, menu:new_appeal,
     appeal:submit). Вызывается ПЕРВЫМ из `on_callback`.
  2. `handlers/admin_callback_dispatch.py::dispatch_admin_callback` —
     операторские / broadcast callback'и (op:*, broadcast:*). Вызывается
     ВТОРЫМ.
  3. `handlers/menu.py::handle_callback` — меню жителя + «Мои обращения»
     (menu:*, settings:*, goodbye:*, info:*, subscribe:confirm,
     broadcast:unsubscribe, appeal:show:/followup:/repeat:/atts:,
     appeals:page:). Вызывается ТРЕТЬИМ (fallthrough).

Каждый диспетчер возвращает bool: True = «я обработал, дальше не идём»,
False = «не мой payload, передай следующему». Порядок вызова в
`on_callback` (citizen → admin → menu) — часть контракта: например,
`broadcast:unsubscribe` (житель) лежит в menu._EXACT и НЕ должен быть
перехвачен admin-диспетчером (у того только broadcast:confirm/abort/edit
и broadcast:stop:/cancel-cooldown:). А `appeal:submit` (citizen exact)
не должен спутаться с `appeal:show:` (menu prefix) — их различает число
двоеточий.

**Метод.** Как и в `test_admin_callback_dispatch.py` — мок-диспетчеризация:
патчим `ack_callback` и терминальные handler'ы (admin_commands.run_*,
appeal_funnel.*, menu.open_*), вызываем диспетчер с конкретным payload'ом
и проверяем, что вызван правильный обработчик / правильная группа.
Бизнес-логику handler'ов НЕ дублируем — её покрывают их собственные тесты.
Здесь фиксируем ровно МАРШРУТИЗАЦИЮ: «префикс X попадает в обработчик Y».

Если будущий рефактор сольёт три таблицы в один `callback_router` —
эта сетка должна остаться зелёной без правок (кроме, возможно, способа
вызова), иначе какой-то маршрут потерян.

Локально skip без maxapi (диспетчеры тянут handlers-цепочку).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("maxapi", reason="диспетчеры тянут handlers-цепочку")

from aemr_bot.handlers import admin_callback_dispatch as admin_dispatch  # noqa: E402
from aemr_bot.handlers import appeal as appeal_handler  # noqa: E402
from aemr_bot.handlers import callback_router  # noqa: E402
from aemr_bot.handlers import menu as menu_handler  # noqa: E402
from aemr_bot.handlers.callback_router import CallbackGroup  # noqa: E402


def _event() -> SimpleNamespace:
    return SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock()),
        callback=SimpleNamespace(callback_id="cb-1"),
    )


@asynccontextmanager
async def _fake_session_scope():
    yield MagicMock()


# ===========================================================================
# Группа 1 — citizen-воронка: appeal._dispatch_citizen_callback
# ===========================================================================
# Терминальные handler'ы — функции этого же модуля (_cb_*), но они в свою
# очередь вызывают appeal_funnel.* / users_service.* / ack_callback. Чтобы
# характеризовать ИМЕННО маршрутизацию (payload → нужный _cb_*), патчим
# сами _cb_* на спаи и убеждаемся, что вызван ожидаемый. Это устойчиво к
# рефактору тела handler'ов и проверяет ровно таблицу _CITIZEN_EXACT /
# _CITIZEN_PREFIX.


# payload → имя атрибута _cb_* в appeal, который ДОЛЖЕН быть вызван.
_CITIZEN_EXPECTED: dict[str, str] = {
    "menu:new_appeal": "_cb_new_appeal",
    "consent:yes": "_cb_consent_yes",
    "consent:no": "_cb_consent_no",
    "cancel": "_cb_cancel",
    "addr:reuse": "_cb_addr_reuse",
    "addr:new": "_cb_addr_new",
    "geo:confirm": "_cb_geo",
    "geo:edit_address": "_cb_geo",
    "geo:other_locality": "_cb_geo",
    "appeal:submit": "_cb_appeal_submit",
    # Префиксные — числовой хвост.
    "locality:3": "_cb_locality",
    "topic:7": "_cb_topic",
}


class TestCitizenFlowRouting:
    """Каждый citizen-payload попадает в свой _cb_* и dispatch → True.

    NB: таблицы `_CITIZEN_EXACT`/`_CITIZEN_PREFIX` хранят ПРЯМЫЕ ссылки на
    `_cb_*` (см. docstring appeal.py — патчатся не они, а то, что внутри).
    Поэтому маршрут характеризуем двумя независимыми утверждениями:
    (а) таблица резолвит payload в правильную функцию; (б) реальный
    `_dispatch_citizen_callback` доходит до этой функции и возвращает True.
    Для (б) подменяем целевую запись таблицы спаем — это и есть точка,
    из которой диспетчер достаёт handler.
    """

    @pytest.mark.parametrize("payload,expected_cb", sorted(_CITIZEN_EXPECTED.items()))
    def test_table_resolves_to_expected_handler(
        self, payload: str, expected_cb: str
    ) -> None:
        expected_fn = getattr(appeal_handler, expected_cb)
        handler = appeal_handler._CITIZEN_EXACT.get(payload)
        if handler is None:
            for prefix, prefix_handler in appeal_handler._CITIZEN_PREFIX:
                if payload.startswith(prefix):
                    handler = prefix_handler
                    break
        assert handler is expected_fn, (
            f"{payload!r} должен резолвиться в {expected_cb}, а не {handler}"
        )

    @pytest.mark.parametrize("payload,expected_cb", sorted(_CITIZEN_EXPECTED.items()))
    @pytest.mark.asyncio
    async def test_dispatch_reaches_handler(
        self, payload: str, expected_cb: str
    ) -> None:
        event = _event()
        spy = AsyncMock()
        # Подменяем именно запись таблицы (exact dict или prefix tuple) —
        # это та ссылка, которую достаёт _dispatch_citizen_callback.
        if payload in appeal_handler._CITIZEN_EXACT:
            patched_exact = dict(appeal_handler._CITIZEN_EXACT)
            patched_exact[payload] = spy
            ctx = patch.object(appeal_handler, "_CITIZEN_EXACT", patched_exact)
        else:
            patched_prefix = tuple(
                (p, spy if payload.startswith(p) else h)
                for p, h in appeal_handler._CITIZEN_PREFIX
            )
            ctx = patch.object(appeal_handler, "_CITIZEN_PREFIX", patched_prefix)
        with ctx:
            handled = await appeal_handler._dispatch_citizen_callback(
                event, 7, payload
            )
        assert handled is True, f"{payload!r} должен быть обработан citizen-диспетчером"
        spy.assert_awaited_once_with(event, 7, payload)

    @pytest.mark.asyncio
    async def test_geo_payloads_share_single_handler(self) -> None:
        """geo:confirm / geo:edit_address / geo:other_locality → один _cb_geo
        (он сам различает их по payload)."""
        for payload in ("geo:confirm", "geo:edit_address", "geo:other_locality"):
            assert appeal_handler._CITIZEN_EXACT[payload] is appeal_handler._cb_geo

    @pytest.mark.asyncio
    async def test_non_citizen_payload_returns_false(self) -> None:
        """Не-воронка payload → dispatch отдаёт False (fallthrough дальше)."""
        for payload in ("op:menu", "menu:main", "broadcast:unsubscribe", "settings:help"):
            event = _event()
            handled = await appeal_handler._dispatch_citizen_callback(
                event, 7, payload
            )
            assert handled is False, f"{payload!r} не citizen — ожидался False"

    def test_citizen_prefixes_are_exactly_two(self) -> None:
        """Префиксная таблица воронки — ровно locality: и topic:.

        Если добавят третий префикс — этот тест заставит осознанно
        обновить характеризацию."""
        prefixes = {p for p, _ in appeal_handler._CITIZEN_PREFIX}
        assert prefixes == {"locality:", "topic:"}


# ===========================================================================
# Группа 2 — admin / broadcast: admin_callback_dispatch.dispatch_admin_callback
# ===========================================================================
# Здесь матчим payload → терминальный run_*/_handle_* (admin_commands /
# broadcast_handler / broadcast_templates_handler). Это дублирует и
# расширяет test_admin_callback_dispatch.py, но в формате единой сетки:
# по одному параметру на КАЖДЫЙ префикс/exact, чтобы пропажа любого была
# видна.


# (payload, объект-владелец мока, имя атрибута, ожидаемые позиционные args
#  после event ИЛИ None если проверяем только факт вызова).
_AdminCase = tuple[str, object, str, tuple]

_ADMIN_EXACT_CASES: tuple[_AdminCase, ...] = (
    ("broadcast:confirm", admin_dispatch.broadcast_handler, "_handle_confirm", ()),
    ("broadcast:abort", admin_dispatch.broadcast_handler, "_handle_abort", ()),
    ("broadcast:edit", admin_dispatch.broadcast_handler, "_handle_edit", ()),
    ("op:menu", admin_dispatch.admin_commands, "show_op_menu", None),
    ("op:stats_menu", admin_dispatch.admin_commands, "run_stats_menu", None),
    ("op:stats_week", admin_dispatch.admin_commands, "run_stats", ("week",)),
    ("op:stats_month", admin_dispatch.admin_commands, "run_stats", ("month",)),
    ("op:stats_quarter", admin_dispatch.admin_commands, "run_stats", ("quarter",)),
    ("op:stats_half_year", admin_dispatch.admin_commands, "run_stats", ("half_year",)),
    ("op:stats_year", admin_dispatch.admin_commands, "run_stats", ("year",)),
    ("op:stats_all", admin_dispatch.admin_commands, "run_stats", ("all",)),
    ("op:open_tickets", admin_dispatch.admin_commands, "run_open_tickets", None),
    ("op:diag", admin_dispatch.admin_commands, "run_diag", None),
    ("op:backup", admin_dispatch.admin_commands, "run_backup", None),
    ("op:broadcast", admin_dispatch.broadcast_handler, "_start_wizard", None),
    ("op:broadcast_list", admin_dispatch.broadcast_handler, "_list_broadcasts", None),
    ("op:operators", admin_dispatch.admin_commands, "run_operators_menu", None),
    ("op:settings", admin_dispatch.admin_commands, "run_settings_menu", None),
    ("op:audience", admin_dispatch.admin_commands, "run_audience_menu", None),
    ("op:reply_cancel", admin_dispatch.admin_commands, "run_reply_cancel", None),
)


class TestAdminExactRouting:
    @pytest.mark.parametrize(
        "payload,owner,attr,extra_args",
        _ADMIN_EXACT_CASES,
        ids=[c[0] for c in _ADMIN_EXACT_CASES],
    )
    @pytest.mark.asyncio
    async def test_exact_payload_routes(
        self, payload: str, owner: object, attr: str, extra_args: tuple | None
    ) -> None:
        event = _event()
        with patch.object(admin_dispatch, "ack_callback", AsyncMock()), patch.object(
            owner, attr, AsyncMock()
        ) as spy:
            handled = await admin_dispatch.dispatch_admin_callback(event, payload)
        assert handled is True
        spy.assert_awaited_once()
        if extra_args is not None:
            spy.assert_awaited_once_with(event, *extra_args)

    @pytest.mark.asyncio
    async def test_op_stats_today_special_branch(self) -> None:
        """op:stats_today — особый exact: показывает меню только если
        выгрузка реально ушла (run_stats_today → bool)."""
        event = _event()
        with patch.object(admin_dispatch, "ack_callback", AsyncMock()), patch.object(
            admin_dispatch.admin_commands, "run_stats_today", AsyncMock(return_value=True)
        ), patch.object(
            admin_dispatch.admin_commands, "show_op_menu", AsyncMock()
        ) as show:
            handled = await admin_dispatch.dispatch_admin_callback(event, "op:stats_today")
        assert handled is True
        show.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_op_help_full_and_security_route(self) -> None:
        """op:help_full / op:help_security — exact, ведут к внутренним
        _show_op_help_* (рендерят памятку)."""
        for payload, attr in (
            ("op:help_full", "_show_op_help_full"),
            ("op:help_security", "_show_op_help_security"),
        ):
            event = _event()
            with patch.object(admin_dispatch, "ack_callback", AsyncMock()), patch.object(
                admin_dispatch, attr, AsyncMock()
            ) as spy:
                handled = await admin_dispatch.dispatch_admin_callback(event, payload)
            assert handled is True
            spy.assert_awaited_once_with(event)


# --- prefix-id (op:<verb>:<int> / broadcast:<verb>:<int>) -------------------

# (payload, owner, attr, ожидаемые позиционные args после event).
_AdminPrefixIdCase = tuple[str, object, str, tuple]

_ADMIN_PREFIX_ID_CASES: tuple[_AdminPrefixIdCase, ...] = (
    ("broadcast:stop:9", admin_dispatch.broadcast_handler, "_handle_stop", (9,)),
    (
        "broadcast:cancel-cooldown:4",
        admin_dispatch.broadcast_handler,
        "_handle_cancel_cooldown",
        (4,),
    ),
    ("op:reply:42", admin_dispatch.admin_commands, "run_reply_intent", (42,)),
    ("op:reopen:8", admin_dispatch.admin_commands, "run_reopen", (8,)),
    ("op:close:5", admin_dispatch.admin_commands, "run_close", (5,)),
    ("op:erase:6", admin_dispatch.admin_commands, "run_erase_for_appeal", (6,)),
    ("op:atts:11", admin_dispatch.admin_commands, "run_show_attachments", (11,)),
    ("op:bc:open:3", admin_dispatch.broadcast_handler, "_open_broadcast", (3,)),
    ("op:bc:clone:3", admin_dispatch.broadcast_handler, "_clone_broadcast", (3,)),
    ("op:bc:failed:3", admin_dispatch.broadcast_handler, "_list_failed_deliveries", (3,)),
)


class TestAdminPrefixIdRouting:
    @pytest.mark.parametrize(
        "payload,owner,attr,expected_args",
        _ADMIN_PREFIX_ID_CASES,
        ids=[c[0] for c in _ADMIN_PREFIX_ID_CASES],
    )
    @pytest.mark.asyncio
    async def test_prefix_id_routes_and_parses_tail(
        self, payload: str, owner: object, attr: str, expected_args: tuple
    ) -> None:
        event = _event()
        with patch.object(owner, attr, AsyncMock()) as spy:
            handled = await admin_dispatch.dispatch_admin_callback(event, payload)
        assert handled is True
        spy.assert_awaited_once_with(event, *expected_args)

    @pytest.mark.asyncio
    async def test_op_replyint_passes_is_final_false(self) -> None:
        """op:replyint:<id> — промежуточный ответ, тот же run_reply_intent,
        но с is_final=False (характеризуем именно kwarg)."""
        event = _event()
        with patch.object(
            admin_dispatch.admin_commands, "run_reply_intent", AsyncMock()
        ) as spy:
            handled = await admin_dispatch.dispatch_admin_callback(event, "op:replyint:42")
        assert handled is True
        spy.assert_awaited_once_with(event, 42, is_final=False)

    @pytest.mark.asyncio
    async def test_op_block_unblock_pass_blocked_kwarg(self) -> None:
        """op:block:<id> и op:unblock:<id> идут в одну run_block_for_appeal,
        различаются kwarg blocked=True/False."""
        for payload, blocked in (("op:block:7", True), ("op:unblock:7", False)):
            event = _event()
            with patch.object(
                admin_dispatch.admin_commands, "run_block_for_appeal", AsyncMock()
            ) as spy:
                handled = await admin_dispatch.dispatch_admin_callback(event, payload)
            assert handled is True
            spy.assert_awaited_once_with(event, 7, blocked=blocked)

    @pytest.mark.asyncio
    async def test_op_open_card_routes(self) -> None:
        """op:open_card:<id> — особый prefix-id handler (рендерит карточку
        с timeline). Проверяем достижимость через ensure_operator-гейт."""
        event = _event()
        with patch.object(
            admin_dispatch, "ensure_operator", AsyncMock(return_value=False)
        ) as gate:
            handled = await admin_dispatch.dispatch_admin_callback(event, "op:open_card:13")
        assert handled is True
        gate.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_malformed_tail_acks_without_action(self) -> None:
        """Битый/пустой int-хвост → ack + стоп, handler НЕ вызывается."""
        for payload in ("op:reply:not-a-number", "op:close:"):
            event = _event()
            with patch.object(
                admin_dispatch, "ack_callback", AsyncMock()
            ) as ack, patch.object(
                admin_dispatch.admin_commands, "run_reply_intent", AsyncMock()
            ) as run_reply, patch.object(
                admin_dispatch.admin_commands, "run_close", AsyncMock()
            ) as run_close:
                handled = await admin_dispatch.dispatch_admin_callback(event, payload)
            assert handled is True
            ack.assert_awaited_once()
            run_reply.assert_not_awaited()
            run_close.assert_not_awaited()


# --- prefix-raw (handler сам разбирает payload + сам акает) ------------------

# (payload, owner, attr) — owner всегда вызывается с (event, полный_payload).
_AdminPrefixRawCase = tuple[str, object, str]

_ADMIN_PREFIX_RAW_CASES: tuple[_AdminPrefixRawCase, ...] = (
    ("op:aud:block:5", admin_dispatch.admin_commands, "run_audience_action"),
    ("op:opadd:start", admin_dispatch.admin_commands, "run_operators_action"),
    ("op:opcard:5", admin_dispatch.admin_commands, "run_operators_action"),
    ("op:oprole:5", admin_dispatch.admin_commands, "run_operators_action"),
    ("op:opchrole:5:admin", admin_dispatch.admin_commands, "run_operators_action"),
    ("op:opdeact:5", admin_dispatch.admin_commands, "run_operators_action"),
    ("op:opdeact_ok:5", admin_dispatch.admin_commands, "run_operators_action"),
    ("op:opreact:5", admin_dispatch.admin_commands, "run_operators_action"),
    ("op:setkey:topics", admin_dispatch.admin_commands, "run_settings_action"),
    ("op:set:cat:contacts", admin_dispatch.admin_commands, "run_settings_action"),
)


class TestAdminPrefixRawRouting:
    @pytest.mark.parametrize(
        "payload,owner,attr",
        _ADMIN_PREFIX_RAW_CASES,
        ids=[c[0] for c in _ADMIN_PREFIX_RAW_CASES],
    )
    @pytest.mark.asyncio
    async def test_prefix_raw_passes_full_payload(
        self, payload: str, owner: object, attr: str
    ) -> None:
        event = _event()
        with patch.object(owner, attr, AsyncMock()) as spy:
            handled = await admin_dispatch.dispatch_admin_callback(event, payload)
        assert handled is True
        spy.assert_awaited_once_with(event, payload)

    @pytest.mark.asyncio
    async def test_op_tmpl_routes_to_templates_handler(self) -> None:
        """op:tmpl:* — отдельное семейство (шаблоны рассылок PR H), идёт в
        broadcast_templates_handler.handle_callback с полным payload."""
        event = _event()
        with patch.object(
            admin_dispatch.broadcast_templates_handler, "handle_callback", AsyncMock()
        ) as spy:
            handled = await admin_dispatch.dispatch_admin_callback(
                event, "op:tmpl:open:9"
            )
        assert handled is True
        spy.assert_awaited_once_with(event, "op:tmpl:open:9")


class TestAdminFallthrough:
    @pytest.mark.parametrize(
        "payload",
        [
            "menu:new_appeal",  # citizen exact
            "menu:main",  # menu exact
            "settings:help",  # menu exact
            "broadcast:unsubscribe",  # citizen (menu) — НЕ admin broadcast
            "op:totally_unknown",  # op:-обёртка с неизвестным хвостом
            "broadcast:weird",  # broadcast:-обёртка с неизвестным хвостом
            "appeal:show:5",  # menu prefix
        ],
    )
    @pytest.mark.asyncio
    async def test_non_admin_payload_returns_false(self, payload: str) -> None:
        """Контракт fallthrough: всё не-admin → False, caller идёт в menu.

        Особо важно про broadcast:unsubscribe — это житель, и admin-
        диспетчер обязан его пропустить (иначе перехватит чужой маршрут)."""
        event = _event()
        handled = await admin_dispatch.dispatch_admin_callback(event, payload)
        assert handled is False


# ===========================================================================
# Группа 3 — меню жителя: menu.handle_callback
# ===========================================================================
# Терминальные handler'ы — функции menu.py (open_*, do_*, ask_*). Патчим
# их спаями и проверяем payload → нужная функция. handle_callback сам
# вызывает ack_callback — патчим и его.


# payload → (имя атрибута menu, requires_user). Для requires_user=True
# передаём max_user_id, иначе None достаточно.
_MENU_EXACT_CASES: tuple[tuple[str, str, bool], ...] = (
    ("menu:main", "open_main_menu", False),
    ("menu:my_appeals", "open_my_appeals", True),
    ("menu:useful_info", "open_useful_info", False),
    ("menu:appointment", "open_appointment", False),
    ("menu:security", "open_security_info", False),
    ("menu:settings", "open_settings", False),
    ("settings:help", "open_help", False),
    ("settings:rules", "open_rules", False),
    ("settings:forget_ask", "ask_forget_confirm", False),
    ("settings:forget_yes", "do_forget", True),
    ("settings:consent_status", "show_consent_status", True),
    ("settings:consent_revoke_ask", "ask_consent_revoke_confirm", False),
    ("settings:consent_revoke_yes", "do_consent_revoke", True),
    ("settings:goodbye", "open_goodbye", False),
    ("goodbye:unsub", "do_unsubscribe", True),
    ("goodbye:revoke_ask", "ask_goodbye_revoke_confirm", False),
    ("goodbye:revoke_yes", "do_consent_revoke", True),
    ("goodbye:erase_ask", "ask_goodbye_erase_confirm", False),
    ("goodbye:erase_yes", "do_forget", True),
    ("info:emergency", "open_emergency", False),
    ("info:dispatchers", "open_dispatchers", False),
    ("info:subscribe_on", "do_subscribe", True),
    ("subscribe:confirm", "do_subscribe_confirm", True),
    ("info:subscribe_off", "do_unsubscribe", True),
    ("info:subscribe_toggle", "do_subscribe", True),  # legacy → идемпотентный on
)


class TestMenuExactRouting:
    @pytest.mark.parametrize(
        "payload,attr,requires_user",
        _MENU_EXACT_CASES,
        ids=[c[0] for c in _MENU_EXACT_CASES],
    )
    @pytest.mark.asyncio
    async def test_exact_payload_routes(
        self, payload: str, attr: str, requires_user: bool
    ) -> None:
        event = _event()
        max_user_id = 7
        with patch.object(menu_handler, "ack_callback", AsyncMock()), patch.object(
            menu_handler, attr, AsyncMock()
        ) as spy:
            handled = await menu_handler.handle_callback(event, payload, max_user_id)
        assert handled is True
        spy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_settings_policy_routes_to_lazy_cmd_policy(self) -> None:
        """settings:policy — особый: ленивый импорт start.cmd_policy через
        _lazy_cmd_policy. Характеризуем достижимость обёртки."""
        event = _event()
        with patch.object(menu_handler, "ack_callback", AsyncMock()), patch.object(
            menu_handler, "_lazy_cmd_policy", AsyncMock()
        ) as spy:
            handled = await menu_handler.handle_callback(event, "settings:policy", 7)
        assert handled is True
        # Lambda в _EXACT зовёт _lazy_cmd_policy(e) — только event, без user.
        spy.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_settings_consent_give_routes_to_lazy_funnel(self) -> None:
        event = _event()
        with patch.object(menu_handler, "ack_callback", AsyncMock()), patch.object(
            menu_handler, "_lazy_start_appeal_flow", AsyncMock()
        ) as spy:
            handled = await menu_handler.handle_callback(event, "settings:consent_give", 7)
        assert handled is True
        spy.assert_awaited_once_with(event, 7)

    @pytest.mark.asyncio
    async def test_broadcast_unsubscribe_self_acks(self) -> None:
        """broadcast:unsubscribe — единственный menu-маршрут с ack=False
        (handler акает сам внутри). Характеризуем: ack_callback НЕ зовётся
        диспетчером, но handler вызван."""
        event = _event()
        with patch.object(menu_handler, "ack_callback", AsyncMock()) as ack, patch.object(
            menu_handler, "handle_broadcast_unsubscribe", AsyncMock()
        ) as spy:
            handled = await menu_handler.handle_callback(event, "broadcast:unsubscribe", 7)
        assert handled is True
        spy.assert_awaited_once_with(event, 7)
        ack.assert_not_awaited()


# --- menu prefix: appeal:show:/followup:/repeat:/atts: ----------------------

_MENU_PREFIX_CASES: tuple[tuple[str, str], ...] = (
    ("appeal:show:5", "show_appeal"),
    ("appeal:followup:5", "start_appeal_followup"),
    ("appeal:repeat:5", "start_appeal_repeat"),
    ("appeal:atts:5", "show_appeal_attachments"),
)


class TestMenuPrefixRouting:
    @pytest.mark.parametrize(
        "payload,attr",
        _MENU_PREFIX_CASES,
        ids=[c[0] for c in _MENU_PREFIX_CASES],
    )
    @pytest.mark.asyncio
    async def test_appeal_prefix_routes_with_parsed_id(
        self, payload: str, attr: str
    ) -> None:
        event = _event()
        with patch.object(menu_handler, "ack_callback", AsyncMock()), patch.object(
            menu_handler, attr, AsyncMock()
        ) as spy:
            handled = await menu_handler.handle_callback(event, payload, 7)
        assert handled is True
        spy.assert_awaited_once_with(event, 5, 7)

    @pytest.mark.asyncio
    async def test_appeal_prefix_requires_user(self) -> None:
        """appeal:* prefix-маршруты осмыслены только при max_user_id !=
        None — иначе fallthrough (False)."""
        event = _event()
        handled = await menu_handler.handle_callback(event, "appeal:show:5", None)
        assert handled is False

    @pytest.mark.asyncio
    async def test_appeal_prefix_malformed_id_swallowed(self) -> None:
        """Битый id в appeal:show: → тап «съедается» (True), handler НЕ
        вызван (исторический контракт if-elif)."""
        event = _event()
        with patch.object(menu_handler, "ack_callback", AsyncMock()), patch.object(
            menu_handler, "show_appeal", AsyncMock()
        ) as spy:
            handled = await menu_handler.handle_callback(event, "appeal:show:xx", 7)
        assert handled is True
        spy.assert_not_awaited()


class TestMenuAppealsPagination:
    @pytest.mark.asyncio
    async def test_appeals_page_numeric_opens_page(self) -> None:
        event = _event()
        with patch.object(menu_handler, "ack_callback", AsyncMock()), patch.object(
            menu_handler, "open_my_appeals", AsyncMock()
        ) as spy:
            handled = await menu_handler.handle_callback(event, "appeals:page:3", 7)
        assert handled is True
        spy.assert_awaited_once_with(event, 7, page=3)

    @pytest.mark.asyncio
    async def test_appeals_page_noop_only_acks(self) -> None:
        """appeals:page:noop — текущая страница, только ack, без open."""
        event = _event()
        with patch.object(menu_handler, "ack_callback", AsyncMock()) as ack, patch.object(
            menu_handler, "open_my_appeals", AsyncMock()
        ) as spy:
            handled = await menu_handler.handle_callback(event, "appeals:page:noop", 7)
        assert handled is True
        ack.assert_awaited_once()
        spy.assert_not_awaited()


class TestMenuFallthrough:
    @pytest.mark.asyncio
    async def test_unknown_payload_returns_false(self) -> None:
        """Совсем неизвестный payload → menu тоже отдаёт False (конец
        цепочки, on_callback просто молча завершается)."""
        event = _event()
        handled = await menu_handler.handle_callback(event, "totally:unknown", 7)
        assert handled is False

    @pytest.mark.asyncio
    async def test_requires_user_consume_on_no_user_semantics(self) -> None:
        """menu:my_appeals «съедает» тап при max_user_id=None (True),
        а прочие user-маршруты — проваливаются (False). Историческое
        расхождение зафиксировано в _MenuRoute.consume_on_no_user."""
        # my_appeals: consume_on_no_user=True → True.
        event = _event()
        handled_consume = await menu_handler.handle_callback(event, "menu:my_appeals", None)
        assert handled_consume is True
        # settings:forget_yes: consume_on_no_user=False → False.
        event2 = _event()
        handled_fall = await menu_handler.handle_callback(event2, "settings:forget_yes", None)
        assert handled_fall is False


# ===========================================================================
# Группа 4 — реестр callback_router: route_for классифицирует ВСЕ префиксы
# ===========================================================================
# callback_router — это та таблица, в которую будущее объединение должно
# схлопнуть три диспетчера. Закрепляем, что route_for каждого
# репрезентативного payload даёт ожидаемую группу, и что admin/citizen
# граница (admin_allowed) совпадает с тем, какой диспетчер реально
# обрабатывает payload.


# payload → ожидаемая CallbackGroup.
_ROUTE_GROUP_CASES: tuple[tuple[str, CallbackGroup], ...] = (
    # citizen exact
    ("menu:main", CallbackGroup.CITIZEN_FLOW),
    ("menu:new_appeal", CallbackGroup.CITIZEN_FLOW),
    ("consent:yes", CallbackGroup.CITIZEN_FLOW),
    ("cancel", CallbackGroup.CITIZEN_FLOW),
    ("addr:reuse", CallbackGroup.CITIZEN_FLOW),
    ("subscribe:confirm", CallbackGroup.CITIZEN_FLOW),
    ("settings:goodbye", CallbackGroup.CITIZEN_FLOW),
    ("goodbye:erase_yes", CallbackGroup.CITIZEN_FLOW),
    ("info:emergency", CallbackGroup.CITIZEN_FLOW),
    ("broadcast:unsubscribe", CallbackGroup.CITIZEN_FLOW),
    # citizen prefix
    ("locality:3", CallbackGroup.CITIZEN_FLOW),
    ("topic:7", CallbackGroup.CITIZEN_FLOW),
    ("appeal:show:5", CallbackGroup.CITIZEN_FLOW),
    ("appeal:followup:5", CallbackGroup.CITIZEN_FLOW),
    ("appeal:repeat:5", CallbackGroup.CITIZEN_FLOW),
    ("appeal:atts:5", CallbackGroup.CITIZEN_FLOW),
    ("appeals:page:2", CallbackGroup.CITIZEN_FLOW),
    # geo
    ("geo:confirm", CallbackGroup.GEO_FLOW),
    ("geo:edit_address", CallbackGroup.GEO_FLOW),
    ("geo:other_locality", CallbackGroup.GEO_FLOW),
    # broadcast admin
    ("broadcast:confirm", CallbackGroup.BROADCAST_ADMIN),
    ("broadcast:abort", CallbackGroup.BROADCAST_ADMIN),
    ("broadcast:edit", CallbackGroup.BROADCAST_ADMIN),
    ("broadcast:stop:9", CallbackGroup.BROADCAST_ADMIN),
    ("broadcast:cancel-cooldown:4", CallbackGroup.BROADCAST_ADMIN),
    ("op:tmpl:open:9", CallbackGroup.BROADCAST_ADMIN),
    ("op:bc:open:3", CallbackGroup.BROADCAST_ADMIN),
    # operator admin exact
    ("op:menu", CallbackGroup.OPERATOR_ADMIN),
    ("op:stats_today", CallbackGroup.OPERATOR_ADMIN),
    ("op:diag", CallbackGroup.OPERATOR_ADMIN),
    ("op:backup", CallbackGroup.OPERATOR_ADMIN),
    ("op:operators", CallbackGroup.OPERATOR_ADMIN),
    ("op:settings", CallbackGroup.OPERATOR_ADMIN),
    ("op:audience", CallbackGroup.OPERATOR_ADMIN),
    ("op:reply_cancel", CallbackGroup.OPERATOR_ADMIN),
    ("op:help_full", CallbackGroup.OPERATOR_ADMIN),
    ("op:help_security", CallbackGroup.OPERATOR_ADMIN),
    # operator admin prefix
    ("op:aud:block:5", CallbackGroup.OPERATOR_ADMIN),
    ("op:reply:42", CallbackGroup.OPERATOR_ADMIN),
    ("op:replyint:42", CallbackGroup.OPERATOR_ADMIN),
    ("op:reopen:8", CallbackGroup.OPERATOR_ADMIN),
    ("op:close:5", CallbackGroup.OPERATOR_ADMIN),
    ("op:erase:6", CallbackGroup.OPERATOR_ADMIN),
    ("op:block:7", CallbackGroup.OPERATOR_ADMIN),
    ("op:unblock:7", CallbackGroup.OPERATOR_ADMIN),
    ("op:atts:11", CallbackGroup.OPERATOR_ADMIN),
    ("op:open_card:13", CallbackGroup.OPERATOR_ADMIN),
    ("op:opadd:start", CallbackGroup.OPERATOR_ADMIN),
    ("op:opcard:5", CallbackGroup.OPERATOR_ADMIN),
    ("op:oprole:5", CallbackGroup.OPERATOR_ADMIN),
    ("op:opchrole:5:admin", CallbackGroup.OPERATOR_ADMIN),
    ("op:opdeact:5", CallbackGroup.OPERATOR_ADMIN),
    ("op:opdeact_ok:5", CallbackGroup.OPERATOR_ADMIN),
    ("op:opreact:5", CallbackGroup.OPERATOR_ADMIN),
    ("op:setkey:topics", CallbackGroup.OPERATOR_ADMIN),
    ("op:set:cat:contacts", CallbackGroup.OPERATOR_ADMIN),
    # fallback
    ("totally:unknown", CallbackGroup.MENU_FALLBACK),
    ("op:totally_unknown", CallbackGroup.MENU_FALLBACK),
    ("broadcast:weird", CallbackGroup.MENU_FALLBACK),
)


class TestRouterRouteForGrouping:
    @pytest.mark.parametrize(
        "payload,group",
        _ROUTE_GROUP_CASES,
        ids=[c[0] for c in _ROUTE_GROUP_CASES],
    )
    def test_route_for_returns_expected_group(
        self, payload: str, group: CallbackGroup
    ) -> None:
        assert callback_router.route_for(payload).group is group

    @pytest.mark.parametrize(
        "payload,group",
        _ROUTE_GROUP_CASES,
        ids=[c[0] for c in _ROUTE_GROUP_CASES],
    )
    def test_admin_allowed_matches_group(
        self, payload: str, group: CallbackGroup
    ) -> None:
        """admin_allowed == True ровно для admin-групп. Это и есть гейт
        в on_callback (callback_router.is_admin_callback), решающий, можно
        ли обрабатывать payload в админ-группе."""
        admin_groups = {CallbackGroup.BROADCAST_ADMIN, CallbackGroup.OPERATOR_ADMIN}
        expected = group in admin_groups
        assert callback_router.is_admin_callback(payload) is expected


# ===========================================================================
# Группа 5 — порядок цепочки в on_callback (citizen → admin → menu)
# ===========================================================================
# Самый хрупкий инвариант при объединении: ТРИ диспетчера вызываются в
# фиксированном порядке, и первый, вернувший True, останавливает цепочку.
# Здесь проверяем именно оркестрацию (через реальный on_callback с mock
# dp), а не тело handler'ов.


def _make_callback_event(*, chat_id: int = 555, user_id: int = 7,
                         payload: str = "") -> SimpleNamespace:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return SimpleNamespace(
        bot=bot,
        callback=SimpleNamespace(
            payload=payload,
            callback_id="cb-1",
            user=SimpleNamespace(user_id=user_id, first_name="X"),
        ),
        message=SimpleNamespace(
            answer=AsyncMock(),
            sender=SimpleNamespace(user_id=user_id, first_name="X"),
            recipient=SimpleNamespace(chat_id=chat_id),
            body=SimpleNamespace(text="", attachments=[], mid="m-1"),
        ),
    )


class _CapturingDispatcher:
    def __init__(self) -> None:
        self.callback_handler: Callable[..., Awaitable] | None = None
        self.message_handler: Callable[..., Awaitable] | None = None

    def message_callback(self):
        def deco(fn):
            self.callback_handler = fn
            return fn
        return deco

    def message_created(self):
        def deco(fn):
            self.message_handler = fn
            return fn
        return deco


@pytest.fixture
def on_callback():
    dp = _CapturingDispatcher()
    appeal_handler.register(dp)
    assert dp.callback_handler is not None
    return dp.callback_handler


class TestDispatchChainOrder:
    @pytest.mark.asyncio
    async def test_citizen_payload_stops_before_admin_and_menu(
        self, on_callback
    ) -> None:
        """menu:new_appeal обрабатывается citizen-диспетчером (True) —
        admin-dispatch и menu.handle_callback НЕ должны вызываться."""
        event = _make_callback_event(payload="menu:new_appeal")
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), patch.object(
            appeal_handler, "_dispatch_citizen_callback", AsyncMock(return_value=True)
        ) as citizen, patch.object(
            appeal_handler.admin_callback_dispatch,
            "dispatch_admin_callback",
            AsyncMock(return_value=True),
        ) as admin, patch(
            "aemr_bot.handlers.menu.handle_callback", AsyncMock(return_value=True)
        ) as menu:
            await on_callback(event)
        citizen.assert_awaited_once()
        admin.assert_not_awaited()
        menu.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_admin_payload_routes_to_admin_not_menu(
        self, on_callback
    ) -> None:
        """Admin payload (op:menu) идёт в admin-dispatch; тот вернул True —
        menu НЕ вызывается.

        После объединения через `callback_router.route_for(payload).group`
        admin-payload классифицируется как OPERATOR_ADMIN и попадает СРАЗУ
        в admin-dispatch — citizen-воронка для него больше не опрашивается
        (раньше слепая цепочка дёргала citizen вхолостую). Наблюдаемый
        контракт (admin обрабатывает op:menu, menu не трогается) сохранён.

        P3-1 authz-гейт: admin-payload выполняется ТОЛЬКО из настроенной
        админ-группы, поэтому легитимный кейс — событие ИЗ админ-чата
        (chat_id == admin_group_id). Реверс-гейт для НЕ-админ-чата проверяет
        отдельный тест `test_admin_payload_from_non_admin_chat_is_blocked`.
        """
        event = _make_callback_event(payload="op:menu", chat_id=999)
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), patch.object(
            appeal_handler, "_dispatch_citizen_callback", AsyncMock(return_value=False)
        ) as citizen, patch.object(
            appeal_handler.admin_callback_dispatch,
            "dispatch_admin_callback",
            AsyncMock(return_value=True),
        ) as admin, patch(
            "aemr_bot.handlers.menu.handle_callback", AsyncMock(return_value=True)
        ) as menu:
            await on_callback(event)
        # Router направляет admin-группу мимо citizen-воронки.
        citizen.assert_not_awaited()
        admin.assert_awaited_once()
        menu.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_admin_miss_falls_through_to_menu(
        self, on_callback
    ) -> None:
        """Admin-обёртка с неизвестным хвостом: admin-dispatch вернул False
        → fallthrough в menu.handle_callback (исторический контракт).

        `op:bc:weird` реестр классифицирует как BROADCAST_ADMIN, но
        admin-dispatch не знает такой хвост (есть только op:bc:open/clone/
        failed) и возвращает False. Раньше такой payload проваливался из
        admin-обёртки в меню — сохраняем это поведение.

        P3-1 authz-гейт пропускает admin-payload только ИЗ админ-чата, поэтому
        событие приходит с chat_id == admin_group_id; иначе fallthrough в
        меню не дошёл бы (реверс-гейт отсёк бы payload раньше dispatch)."""
        event = _make_callback_event(payload="op:bc:weird", chat_id=999)
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), patch.object(
            appeal_handler, "_dispatch_citizen_callback", AsyncMock(return_value=False)
        ) as citizen, patch.object(
            appeal_handler.admin_callback_dispatch,
            "dispatch_admin_callback",
            AsyncMock(return_value=False),
        ) as admin, patch(
            "aemr_bot.handlers.menu.handle_callback", AsyncMock(return_value=True)
        ) as menu:
            await on_callback(event)
        citizen.assert_not_awaited()
        admin.assert_awaited_once()
        menu.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_admin_payload_from_non_admin_chat_is_blocked(
        self, on_callback
    ) -> None:
        """P3-1 реверс-гейт: admin-payload (op:menu) из НЕ-админ-чата (личка
        жителя) отсекается в `_route_callback` ДО dispatch — тихий ack,
        ни admin-dispatch, ни fallthrough в меню.

        Это обратная граница к `test_admin_chat_blocks_non_admin_payload`
        (та закрывает прямую: житель-payload в админ-группе). Здесь chat_id
        (личка, 555) != admin_group_id (999): группа реестра OPERATOR_ADMIN,
        но admin-dispatch НЕ зовётся (иначе отмена мастера рассылки / отмена
        черновика ответа дёргала бы in-memory wizard/intent-хранилища из
        лички ДО ролевой проверки — инъекция в опер-группу)."""
        event = _make_callback_event(payload="op:menu", chat_id=555)
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), patch.object(
            appeal_handler, "ack_callback", AsyncMock()
        ) as ack, patch.object(
            appeal_handler, "_dispatch_citizen_callback", AsyncMock(return_value=True)
        ) as citizen, patch.object(
            appeal_handler.admin_callback_dispatch,
            "dispatch_admin_callback",
            AsyncMock(return_value=True),
        ) as admin, patch(
            "aemr_bot.handlers.menu.handle_callback", AsyncMock(return_value=True)
        ) as menu:
            await on_callback(event)
        # Реверс-гейт: тихий ack, дальше ничего не выполняется.
        ack.assert_awaited_once()
        citizen.assert_not_awaited()
        admin.assert_not_awaited()
        menu.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_menu_payload_routes_to_menu_not_admin(
        self, on_callback
    ) -> None:
        """Citizen-группа, не пойманная воронкой (menu:main) → menu.

        `menu:main` реестр относит к CITIZEN_FLOW (security-классификация
        грубая, покрывает и навигацию меню). Воронка его не знает (вернёт
        False), управление спускается в menu.handle_callback. admin-
        dispatch для citizen-группы НЕ вызывается — это и есть устранённая
        слепая цепочка. Раньше admin дёргался вхолостую между citizen и
        menu; наблюдаемый результат (payload доходит до menu) сохранён."""
        event = _make_callback_event(payload="menu:main")
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), patch.object(
            appeal_handler, "_dispatch_citizen_callback", AsyncMock(return_value=False)
        ) as citizen, patch.object(
            appeal_handler.admin_callback_dispatch,
            "dispatch_admin_callback",
            AsyncMock(return_value=False),
        ) as admin, patch(
            "aemr_bot.handlers.menu.handle_callback", AsyncMock(return_value=True)
        ) as menu:
            await on_callback(event)
        citizen.assert_awaited_once()
        # Router не отправляет citizen-группу в admin-dispatch.
        admin.assert_not_awaited()
        menu.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_admin_chat_blocks_non_admin_payload(
        self, on_callback
    ) -> None:
        """Жительский payload, пришедший из админ-группы, отсекается
        is_admin_callback-гейтом ДО всех трёх диспетчеров: тихий ack,
        ни один диспетчер не зван."""
        event = _make_callback_event(payload="menu:new_appeal", chat_id=123)
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 123), patch(
            "aemr_bot.utils.event.ack_callback", AsyncMock()
        ), patch.object(
            appeal_handler, "_dispatch_citizen_callback", AsyncMock(return_value=True)
        ) as citizen, patch.object(
            appeal_handler.admin_callback_dispatch,
            "dispatch_admin_callback",
            AsyncMock(return_value=True),
        ) as admin:
            await on_callback(event)
        citizen.assert_not_awaited()
        admin.assert_not_awaited()
