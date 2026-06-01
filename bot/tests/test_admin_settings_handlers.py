"""Characterization-тесты handlers/admin_settings.py — Cluster E
(coverage wave per Codex PR 5 + продолжение паттерна PR #126).

Подсистема «⚙️ Настройки бота» — иерархическое меню op:set:*,
intent-flow редактирования (TTL 5 мин), wizard тихого режима,
PR-flow к репо. До этого PR'а покрытие было точечным
(`test_admin_settings_audit.py` про audit-trail). Здесь — широкий
characterization-набор:

1. **Pure helpers**: `_intent_set`/`_intent_get`/`_intent_drop`,
   `_clip_audit_value`, `_render_value`, TTL семантика, GC.
2. **`run_settings_menu` role-check**: non-IT → no-op.
3. **`run_settings_action` dispatch** для всех веток `op:set:*` —
   ack + правильный sub-handler вызывается.
4. **Quiet hours wizard**: `_show_quiet_card`, `_toggle_quiet`,
   `_start_quiet_hour_intent(start/end)`, `_apply_quiet_hour_edit`
   с валидацией 0–23.
5. **`handle_settings_edit_text`**: dispatch по kind в intent +
   intent-drop при successful apply.

Тесты не запускают handler-цепочку до session_scope/DB. Используем
тонкие mocks для `session_scope`, `settings_store.*`, `quiet_hours.*`,
`ops_svc.write_audit`, `ensure_role`, `send_or_edit_screen`. Это
именно характеризационный слой — фиксирует **поведенческий
контракт** handler'а перед будущей декомпозицией (Cluster B/C/B1).
"""
from __future__ import annotations

import time as _time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


pytest.importorskip("maxapi", reason="нужен maxapi для admin_settings импортов")

from tests._helpers import make_callback_event, make_event


# ──────────────────────────────────────────────────────────────────────
# Pure helpers — не требуют моков handler-инфраструктуры
# ──────────────────────────────────────────────────────────────────────


class TestIntentLifecycle:
    """`_intent_set` / `_intent_get` / `_intent_drop` — TTL-кэш для
    «следующее текстовое сообщение оператора = новое значение ключа»."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import admin_settings as mod
        mod._edit_intents.clear()
        yield
        mod._edit_intents.clear()

    def test_set_get_round_trip(self) -> None:
        from aemr_bot.handlers import admin_settings as mod
        mod._intent_set(42, key="welcome_text", kind="single")
        intent = mod._intent_get(42)
        assert intent is not None
        assert intent["key"] == "welcome_text"
        assert intent["kind"] == "single"
        assert "expires_at" in intent

    def test_drop_removes(self) -> None:
        from aemr_bot.handlers import admin_settings as mod
        mod._intent_set(42, key="k", kind="single")
        mod._intent_drop(42)
        assert mod._intent_get(42) is None

    def test_get_unknown_returns_none(self) -> None:
        from aemr_bot.handlers import admin_settings as mod
        assert mod._intent_get(999) is None

    def test_expired_intent_returns_none_and_drops(self) -> None:
        from aemr_bot.handlers import admin_settings as mod
        mod._intent_set(42, key="k", kind="single")
        # Принудительно истекаем — ставим expires_at в прошлое.
        mod._edit_intents[42]["expires_at"] = _time.monotonic() - 1
        assert mod._intent_get(42) is None
        # И тут же должно быть удалено из кэша.
        assert 42 not in mod._edit_intents

    def test_set_extra_fields_preserved(self) -> None:
        from aemr_bot.handlers import admin_settings as mod
        mod._intent_set(42, key="admin_quiet_hours_start", kind="quiet_hour",
                        which="start")
        intent = mod._intent_get(42)
        assert intent is not None
        assert intent["which"] == "start"

    def test_set_overwrites_previous(self) -> None:
        from aemr_bot.handlers import admin_settings as mod
        mod._intent_set(42, key="welcome_text", kind="single")
        mod._intent_set(42, key="consent_text", kind="single")
        intent = mod._intent_get(42)
        assert intent["key"] == "consent_text"

    def test_gc_evicts_expired_when_pool_large(self) -> None:
        """opportunistic GC: при set'е и >16 записях чистим истёкшие.

        Заполняем dict напрямую (минуя `_intent_set`), чтобы не
        триггерить GC на стадии setup'а — иначе раннее срабатывание
        очистит ещё не достроенную лестницу истёкших записей и тест
        потеряет смысл.
        """
        from aemr_bot.handlers import admin_settings as mod
        # 17 истёкших intent'ов напрямую — GC не запускается.
        for op_id in range(100, 117):
            mod._edit_intents[op_id] = {
                "key": f"k{op_id}",
                "kind": "single",
                "expires_at": _time.monotonic() - 1,
            }
        # 17 записей, все истёкшие. _intent_set(200) → len = 18 > 16 →
        # GC.
        mod._intent_set(200, key="fresh", kind="single")
        for op_id in range(100, 117):
            assert op_id not in mod._edit_intents
        assert 200 in mod._edit_intents


class TestClipAuditValue:
    """`_clip_audit_value` — подготовка значения настройки для записи
    в audit_log.details. Длинные значения усекаются."""

    def test_none_returns_dash(self) -> None:
        from aemr_bot.handlers.admin_settings import _clip_audit_value
        assert _clip_audit_value(None) == "—"

    def test_short_string_passthrough(self) -> None:
        from aemr_bot.handlers.admin_settings import _clip_audit_value
        assert _clip_audit_value("hello") == "hello"

    def test_long_string_truncated_with_ellipsis(self) -> None:
        from aemr_bot.handlers.admin_settings import (
            _AUDIT_VALUE_CLIP_LEN,
            _clip_audit_value,
        )
        long_text = "x" * (_AUDIT_VALUE_CLIP_LEN + 50)
        result = _clip_audit_value(long_text)
        assert len(result) == _AUDIT_VALUE_CLIP_LEN
        assert result.endswith("…")

    def test_list_serialised_via_repr(self) -> None:
        from aemr_bot.handlers.admin_settings import _clip_audit_value
        assert _clip_audit_value(["a", "b"]) == "['a', 'b']"

    def test_dict_serialised_via_repr(self) -> None:
        from aemr_bot.handlers.admin_settings import _clip_audit_value
        result = _clip_audit_value({"x": 1})
        assert "'x'" in result and "1" in result


class TestRenderValue:
    """`_render_value` — рендер значения для UI карточки настройки."""

    def test_none_returns_dash(self) -> None:
        from aemr_bot.handlers.admin_settings import _render_value
        assert _render_value(None) == "—"

    def test_short_string_passthrough(self) -> None:
        from aemr_bot.handlers.admin_settings import _render_value
        assert _render_value("hello") == "hello"

    def test_long_string_truncated(self) -> None:
        from aemr_bot.handlers.admin_settings import _render_value
        long_text = "y" * 2000
        result = _render_value(long_text, limit=1500)
        assert "…(обрезано)" in result
        assert len(result) <= 1500 + len("\n…(обрезано)")

    def test_dict_serialised_to_json(self) -> None:
        from aemr_bot.handlers.admin_settings import _render_value
        result = _render_value({"key": "значение"})
        assert "key" in result and "значение" in result

    def test_list_serialised_to_json(self) -> None:
        from aemr_bot.handlers.admin_settings import _render_value
        result = _render_value(["alpha", "beta"])
        assert "alpha" in result and "beta" in result


# ──────────────────────────────────────────────────────────────────────
# run_settings_menu — главная точка входа, role-check
# ──────────────────────────────────────────────────────────────────────


class TestRunSettingsMenu:
    """`run_settings_menu` — точка входа в иерархическое меню."""

    @pytest.mark.asyncio
    async def test_non_it_role_returns_silently(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "ensure_role", AsyncMock(return_value=False)):
            await mod.run_settings_menu(event)
        # Никакая клавиатура не отправлена — гард сработал.
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_it_role_renders_menu_with_dirty_count(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "ensure_role", AsyncMock(return_value=True)), \
             patch.object(mod, "session_scope") as scope, \
             patch.object(mod.settings_store, "get_dirty_keys",
                          AsyncMock(return_value=["welcome_text", "policy_url"])), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            await mod.run_settings_menu(event)
        send.assert_awaited_once()
        # Текст содержит counter «не выгружено».
        kwargs = send.await_args.kwargs
        assert "Не выгружено в репо: 2" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_it_role_no_dirty_no_extra_line(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "ensure_role", AsyncMock(return_value=True)), \
             patch.object(mod, "session_scope") as scope, \
             patch.object(mod.settings_store, "get_dirty_keys",
                          AsyncMock(return_value=[])), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            await mod.run_settings_menu(event)
        kwargs = send.await_args.kwargs
        assert "Не выгружено" not in kwargs["text"]


# ──────────────────────────────────────────────────────────────────────
# run_settings_action — dispatch op:set:*
# ──────────────────────────────────────────────────────────────────────


class TestRunSettingsActionDispatch:
    """`run_settings_action` — главный диспетчер callback'ов meню
    настроек. Проверяем что каждая ветка `op:set:*` доходит до
    соответствующего sub-handler'а."""

    @pytest.fixture(autouse=True)
    def _clean_intents(self):
        from aemr_bot.handlers import admin_settings as mod
        mod._edit_intents.clear()
        yield
        mod._edit_intents.clear()

    @pytest.mark.asyncio
    async def test_non_it_returns_without_ack(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_callback_event(payload="op:set:expert")
        with patch.object(mod, "ensure_role", AsyncMock(return_value=False)), \
             patch.object(mod, "ack_callback", AsyncMock()) as ack:
            await mod.run_settings_action(event, "op:set:expert")
        ack.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_payload_no_op(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_callback_event(payload="op:nope:whatever")
        with patch.object(mod, "ensure_role", AsyncMock(return_value=True)), \
             patch.object(mod, "ack_callback", AsyncMock()) as ack:
            await mod.run_settings_action(event, "op:nope:whatever")
        # Не op:set: и не op:setkey: → ничего не делаем, даже ack нет.
        ack.assert_not_called()

    @pytest.mark.asyncio
    async def test_op_set_dispatches_to_route_set_action(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_callback_event(user_id=42, payload="op:set:author")
        with patch.object(mod, "ensure_role", AsyncMock(return_value=True)), \
             patch.object(mod, "ack_callback", AsyncMock()) as ack, \
             patch.object(mod, "_route_set_action", AsyncMock()) as route:
            await mod.run_settings_action(event, "op:set:author")
        ack.assert_awaited_once()
        route.assert_awaited_once()
        args = route.await_args.args
        assert args[1] == 42  # operator_id
        assert args[2] == "author"  # rest

    @pytest.mark.asyncio
    async def test_op_setkey_legacy_path(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_callback_event(payload="op:setkey:welcome_text")
        with patch.object(mod, "ensure_role", AsyncMock(return_value=True)), \
             patch.object(mod, "_show_expert_key", AsyncMock()) as show:
            await mod.run_settings_action(event, "op:setkey:welcome_text")
        show.assert_awaited_once()


class TestRouteSetActionBranches:
    """`_route_set_action` — внутренний диспетчер по `rest` после
    `op:set:` prefix removal. Главные ветки."""

    @pytest.fixture(autouse=True)
    def _clean_intents(self):
        from aemr_bot.handlers import admin_settings as mod
        mod._edit_intents.clear()
        yield
        mod._edit_intents.clear()

    @pytest.mark.asyncio
    async def test_text_branch_shows_text_card(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "_show_text_card", AsyncMock()) as show:
            await mod._route_set_action(event, 1, "text:welcome_text")
        show.assert_awaited_once_with(event, "welcome_text")

    @pytest.mark.asyncio
    async def test_url_branch_shows_text_card(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "_show_text_card", AsyncMock()) as show:
            await mod._route_set_action(event, 1, "url:policy_url")
        show.assert_awaited_once_with(event, "policy_url")

    @pytest.mark.asyncio
    async def test_edit_branch_starts_intent(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "_start_edit_intent", AsyncMock()) as start:
            await mod._route_set_action(event, 42, "edit:welcome_text")
        start.assert_awaited_once_with(event, 42, "welcome_text")

    @pytest.mark.asyncio
    async def test_cancel_branch_drops_intent_and_shows_text_card(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        mod._intent_set(42, key="welcome_text", kind="single")
        with patch.object(mod, "_show_text_card", AsyncMock()) as show:
            await mod._route_set_action(event, 42, "cancel:welcome_text")
        # Intent сброшен.
        assert mod._intent_get(42) is None
        show.assert_awaited_once_with(event, "welcome_text")

    @pytest.mark.asyncio
    async def test_cancel_branch_for_author_key_shows_author_card(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        mod._intent_set(42, key="commit_author_name", kind="single")
        with patch.object(mod, "_show_author_card", AsyncMock()) as show:
            await mod._route_set_action(event, 42, "cancel:commit_author_name")
        assert mod._intent_get(42) is None
        show.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_list_branch_shows_list_card(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "_show_list_card", AsyncMock()) as show:
            await mod._route_set_action(event, 1, "list:topics")
        show.assert_awaited_once_with(event, "topics")

    @pytest.mark.asyncio
    async def test_list_add_branch_sets_intent(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "send_or_edit_screen", AsyncMock()):
            await mod._route_set_action(event, 42, "list_add:topics")
        intent = mod._intent_get(42)
        assert intent is not None
        assert intent["key"] == "topics"
        assert intent["kind"] == "list_add"

    @pytest.mark.asyncio
    async def test_obj_branch_shows_obj_card(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "_show_obj_card", AsyncMock()) as show:
            await mod._route_set_action(event, 1, "obj:emergency_contacts")
        show.assert_awaited_once_with(event, "emergency_contacts")

    @pytest.mark.asyncio
    async def test_author_branch_shows_author_card(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "_show_author_card", AsyncMock()) as show:
            await mod._route_set_action(event, 1, "author")
        show.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_quiet_branch_shows_quiet_card(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "_show_quiet_card", AsyncMock()) as show:
            await mod._route_set_action(event, 1, "quiet")
        show.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_quiet_toggle_branch(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "_toggle_quiet", AsyncMock()) as toggle:
            await mod._route_set_action(event, 1, "quiet:toggle")
        toggle.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_quiet_edit_start_intent(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "_start_quiet_hour_intent",
                          AsyncMock()) as start:
            await mod._route_set_action(event, 42, "quiet:edit:start")
        start.assert_awaited_once_with(event, 42, which="start")

    @pytest.mark.asyncio
    async def test_quiet_edit_end_intent(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "_start_quiet_hour_intent",
                          AsyncMock()) as start:
            await mod._route_set_action(event, 42, "quiet:edit:end")
        start.assert_awaited_once_with(event, 42, which="end")

    @pytest.mark.asyncio
    async def test_pr_start_branch(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "_show_pr_confirm", AsyncMock()) as show:
            await mod._route_set_action(event, 1, "pr:start")
        show.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_pr_confirm_branch(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "_create_pr", AsyncMock()) as create:
            await mod._route_set_action(event, 42, "pr:confirm")
        create.assert_awaited_once_with(event, 42)


# ──────────────────────────────────────────────────────────────────────
# Quiet hours wizard
# ──────────────────────────────────────────────────────────────────────


class TestQuietHoursWizard:
    """`_show_quiet_card`, `_toggle_quiet`, `_start_quiet_hour_intent`,
    `_apply_quiet_hour_edit` — настройка тихого режима через UI."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import admin_settings as mod
        from aemr_bot.services import quiet_hours
        mod._edit_intents.clear()
        quiet_hours.reset_cache_for_tests()
        yield
        mod._edit_intents.clear()
        quiet_hours.reset_cache_for_tests()

    @pytest.mark.asyncio
    async def test_show_quiet_card_renders_window(self) -> None:
        # `_show_quiet_card` живёт в подмодуле admin_settings_quiet —
        # патчим session_scope/settings_store/send_or_edit_screen на нём
        # (урок PR #139: фасадный re-export не перехватывается patch'ем
        # на старом namespace).
        from aemr_bot.handlers import admin_settings_quiet as mod

        event = make_event()

        async def fake_get(_session, key):
            return {
                "admin_quiet_hours_enabled": True,
                "admin_quiet_hours_start": 22,
                "admin_quiet_hours_end": 8,
            }.get(key)

        with patch.object(mod, "session_scope") as scope, \
             patch("aemr_bot.services.quiet_hours.refresh_cache_from_db",
                   AsyncMock()), \
             patch.object(mod.settings_store, "get", side_effect=fake_get), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            await mod._show_quiet_card(event)
        send.assert_awaited_once()
        kwargs = send.await_args.kwargs
        # 22:00–08:00 в карточке.
        assert "22:00" in kwargs["text"]
        assert "08:00" in kwargs["text"]
        assert "включён" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_show_quiet_card_disabled_status(self) -> None:
        # См. test_show_quiet_card_renders_window — патчим на подмодуле
        # admin_settings_quiet, где живёт `_show_quiet_card`.
        from aemr_bot.handlers import admin_settings_quiet as mod

        event = make_event()

        async def fake_get(_session, key):
            return {
                "admin_quiet_hours_enabled": False,
                "admin_quiet_hours_start": 18,
                "admin_quiet_hours_end": 9,
            }.get(key)

        with patch.object(mod, "session_scope") as scope, \
             patch("aemr_bot.services.quiet_hours.refresh_cache_from_db",
                   AsyncMock()), \
             patch.object(mod.settings_store, "get", side_effect=fake_get), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            await mod._show_quiet_card(event)
        kwargs = send.await_args.kwargs
        assert "выключен" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_toggle_quiet_flips_value(self) -> None:
        # `_toggle_quiet` живёт в admin_settings_quiet — патчим
        # session_scope/settings_store и внутрисмодульный `_show_quiet_card`
        # на нём (урок PR #139), иначе real session_scope утечёт в БД.
        from aemr_bot.handlers import admin_settings_quiet as mod

        event = make_event()

        with patch.object(mod, "session_scope") as scope, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=False)), \
             patch.object(mod.settings_store, "set_value",
                          AsyncMock()) as set_value, \
             patch("aemr_bot.services.quiet_hours.refresh_cache_from_db",
                   AsyncMock()), \
             patch.object(mod, "_show_quiet_card", AsyncMock()):
            sess = MagicMock()
            sess.commit = AsyncMock()
            scope.return_value.__aenter__ = AsyncMock(return_value=sess)
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            await mod._toggle_quiet(event)
        # Flip False → True.
        set_value.assert_awaited_once()
        args = set_value.await_args.args
        assert args[1] == "admin_quiet_hours_enabled"
        assert args[2] is True

    @pytest.mark.asyncio
    async def test_start_quiet_hour_intent_start(self) -> None:
        # `_start_quiet_hour_intent` и его `send_or_edit_screen` живут в
        # admin_settings_quiet — патчим на нём (урок PR #139). Сам intent
        # пишется в общий `_edit_intents` (shared), читаем через фасад.
        from aemr_bot.handlers import admin_settings as mod
        from aemr_bot.handlers import admin_settings_quiet as qmod

        event = make_event()
        with patch.object(qmod, "send_or_edit_screen", AsyncMock()) as send:
            await qmod._start_quiet_hour_intent(event, 42, which="start")
        intent = mod._intent_get(42)
        assert intent is not None
        assert intent["key"] == "admin_quiet_hours_start"
        assert intent["kind"] == "quiet_hour"
        assert intent["which"] == "start"
        # В подсказке упомянуто «начала».
        kwargs = send.await_args.kwargs
        assert "начала" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_start_quiet_hour_intent_end(self) -> None:
        from aemr_bot.handlers import admin_settings as mod
        from aemr_bot.handlers import admin_settings_quiet as qmod

        event = make_event()
        with patch.object(qmod, "send_or_edit_screen", AsyncMock()) as send:
            await qmod._start_quiet_hour_intent(event, 42, which="end")
        intent = mod._intent_get(42)
        assert intent["which"] == "end"
        assert intent["key"] == "admin_quiet_hours_end"
        kwargs = send.await_args.kwargs
        assert "конца" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_start_quiet_hour_intent_invalid_which_asserts(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with pytest.raises(AssertionError):
            await mod._start_quiet_hour_intent(event, 42, which="middle")

    @pytest.mark.asyncio
    async def test_apply_quiet_hour_edit_non_int_rejected(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        await mod._apply_quiet_hour_edit(event, 42, "start", "abc")
        # Bot.send_message вызван с ошибкой «не число».
        event.bot.send_message.assert_awaited()
        kwargs = event.bot.send_message.await_args.kwargs
        assert "не число" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_apply_quiet_hour_edit_out_of_range_rejected(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        await mod._apply_quiet_hour_edit(event, 42, "start", "24")
        event.bot.send_message.assert_awaited()
        kwargs = event.bot.send_message.await_args.kwargs
        assert "вне диапазона" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_apply_quiet_hour_edit_negative_rejected(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        await mod._apply_quiet_hour_edit(event, 42, "end", "-1")
        kwargs = event.bot.send_message.await_args.kwargs
        assert "вне диапазона" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_apply_quiet_hour_edit_valid_value_persists(self) -> None:
        # `_apply_quiet_hour_edit` живёт в admin_settings_quiet; патчим
        # session_scope/settings_store/ops_svc и внутрисмодульный вызов
        # `_show_quiet_card` на нём (урок PR #139 — фасадный re-export не
        # перехватывается patch'ем по старому namespace).
        from aemr_bot.handlers import admin_settings_quiet as mod

        event = make_event()
        with patch.object(mod, "session_scope") as scope, \
             patch.object(mod.settings_store, "set_value",
                          AsyncMock()) as set_value, \
             patch.object(mod.ops_svc, "write_audit", AsyncMock()) as audit, \
             patch("aemr_bot.services.quiet_hours.refresh_cache_from_db",
                   AsyncMock()), \
             patch.object(mod, "_show_quiet_card", AsyncMock()) as show:
            sess = MagicMock()
            sess.commit = AsyncMock()
            scope.return_value.__aenter__ = AsyncMock(return_value=sess)
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            await mod._apply_quiet_hour_edit(event, 42, "start", "23")
        set_value.assert_awaited_once()
        args = set_value.await_args.args
        assert args[1] == "admin_quiet_hours_start"
        assert args[2] == 23
        audit.assert_awaited_once()
        show.assert_awaited_once()


# ──────────────────────────────────────────────────────────────────────
# handle_settings_edit_text — перехват текста для применения intent
# ──────────────────────────────────────────────────────────────────────


class TestHandleSettingsEditText:
    """`handle_settings_edit_text` — перехватывает следующее текстовое
    сообщение оператора если у него есть активный intent."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import admin_settings as mod
        mod._edit_intents.clear()
        yield
        mod._edit_intents.clear()

    @pytest.mark.asyncio
    async def test_no_intent_returns_false(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event(user_id=42)
        result = await mod.handle_settings_edit_text(event, "hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_no_user_id_returns_false(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "get_user_id", return_value=None):
            result = await mod.handle_settings_edit_text(event, "hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_intent_present_but_lost_role_drops_intent(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event(user_id=42)
        mod._intent_set(42, key="welcome_text", kind="single")
        with patch.object(mod, "ensure_role", AsyncMock(return_value=False)):
            result = await mod.handle_settings_edit_text(event, "new text")
        assert result is False
        # Intent сброшен — оператор потерял роль, повторно не должен
        # ничего применить даже если её вернут.
        assert mod._intent_get(42) is None

    @pytest.mark.asyncio
    async def test_kind_single_dispatches_to_apply_single_edit(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event(user_id=42)
        mod._intent_set(42, key="welcome_text", kind="single")
        with patch.object(mod, "ensure_role", AsyncMock(return_value=True)), \
             patch.object(mod, "_apply_single_edit",
                          AsyncMock()) as apply:
            result = await mod.handle_settings_edit_text(event, "new value")
        assert result is True
        apply.assert_awaited_once()
        # Intent сброшен после apply.
        assert mod._intent_get(42) is None

    @pytest.mark.asyncio
    async def test_kind_list_add_dispatches(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event(user_id=42)
        mod._intent_set(42, key="topics", kind="list_add")
        with patch.object(mod, "ensure_role", AsyncMock(return_value=True)), \
             patch.object(mod, "_apply_list_add", AsyncMock()) as apply:
            result = await mod.handle_settings_edit_text(event, "Новая тема")
        assert result is True
        apply.assert_awaited_once()
        assert mod._intent_get(42) is None

    @pytest.mark.asyncio
    async def test_kind_obj_add_dispatches(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event(user_id=42)
        mod._intent_set(42, key="emergency_contacts", kind="obj_add")
        with patch.object(mod, "ensure_role", AsyncMock(return_value=True)), \
             patch.object(mod, "_apply_obj_add", AsyncMock()) as apply:
            result = await mod.handle_settings_edit_text(event,
                                                        "Полиция\n02")
        assert result is True
        apply.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_kind_quiet_hour_dispatches_with_which(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event(user_id=42)
        mod._intent_set(42, key="admin_quiet_hours_start",
                        kind="quiet_hour", which="start")
        with patch.object(mod, "ensure_role", AsyncMock(return_value=True)), \
             patch.object(mod, "_apply_quiet_hour_edit",
                          AsyncMock()) as apply:
            result = await mod.handle_settings_edit_text(event, "23")
        assert result is True
        # which прокидывается в apply.
        apply.assert_awaited_once_with(event, 42, "start", "23")

    @pytest.mark.asyncio
    async def test_unknown_kind_returns_false(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event(user_id=42)
        mod._intent_set(42, key="x", kind="exotic_unknown")
        with patch.object(mod, "ensure_role", AsyncMock(return_value=True)):
            result = await mod.handle_settings_edit_text(event, "data")
        # Неизвестный kind — не падаем, но и не поглощаем.
        assert result is False

    @pytest.mark.asyncio
    async def test_text_stripped_before_dispatch(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event(user_id=42)
        mod._intent_set(42, key="welcome_text", kind="single")
        with patch.object(mod, "ensure_role", AsyncMock(return_value=True)), \
             patch.object(mod, "_apply_single_edit",
                          AsyncMock()) as apply:
            await mod.handle_settings_edit_text(event,
                                                "  trimmed value  \n")
        args = apply.await_args.args
        # Текст stripped до передачи в apply (тест зависит от
        # внутренней семантики handle_settings_edit_text).
        assert args[3] == "trimmed value"


# ──────────────────────────────────────────────────────────────────────
# Race conditions — два оператора, оба с intent'ами
# ──────────────────────────────────────────────────────────────────────


class TestRaceConditions:
    """Базовая проверка изоляции intent'ов между операторами:
    intent оператора-1 не виден оператору-2 и наоборот."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import admin_settings as mod
        mod._edit_intents.clear()
        yield
        mod._edit_intents.clear()

    def test_two_operators_independent_intents(self) -> None:
        from aemr_bot.handlers import admin_settings as mod
        mod._intent_set(10, key="welcome_text", kind="single")
        mod._intent_set(20, key="policy_url", kind="single")
        assert mod._intent_get(10)["key"] == "welcome_text"
        assert mod._intent_get(20)["key"] == "policy_url"
        mod._intent_drop(10)
        assert mod._intent_get(10) is None
        # Второй оператор не задет.
        assert mod._intent_get(20) is not None

    @pytest.mark.asyncio
    async def test_concurrent_edit_one_loses_role(self) -> None:
        """Оператор-A и оператор-B оба с intent'ами. Если у A отозвали
        роль во время edit'а — intent A сбрасывается, B продолжает
        работать. Сейчас контракт: handle_settings_edit_text для A
        вернёт False + drop, для B — True (apply)."""
        from aemr_bot.handlers import admin_settings as mod

        event_a = make_event(user_id=10)
        event_b = make_event(user_id=20)
        mod._intent_set(10, key="welcome_text", kind="single")
        mod._intent_set(20, key="consent_text", kind="single")

        # Оператор A: роль отозвана.
        with patch.object(mod, "ensure_role", AsyncMock(return_value=False)):
            result_a = await mod.handle_settings_edit_text(event_a, "txt-a")
        assert result_a is False
        assert mod._intent_get(10) is None
        # Оператор B: роль ещё есть — apply проходит.
        with patch.object(mod, "ensure_role", AsyncMock(return_value=True)), \
             patch.object(mod, "_apply_single_edit", AsyncMock()):
            result_b = await mod.handle_settings_edit_text(event_b, "txt-b")
        assert result_b is True
