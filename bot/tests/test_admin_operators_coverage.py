"""Покрытие непокрытых веток кластера admin_operators.

Дополняет ``test_admin_operators.py`` и
``test_admin_operators_characterization.py``: те фиксируют основные
сценарии (happy / гарды самомодификации / гард единственного IT / audit-
контракты), а здесь добиваются ветки, которые term-missing показал как
непройденные:

- ``admin_operators`` (фасад): диспетчер ``name_edit``.
- ``admin_operators_list``: счётчик деактивированных в шапке списка.
- ``admin_operators_roles``: невалидный int в payload каждого хендлера
  (``_show_role_change`` / ``_apply_role_change`` / ``_show_deactivate_
  confirm`` / ``_apply_deactivate`` / ``_apply_reactivate``); цель
  None/деактивирована в ``_apply_role_change``; «понижение IT когда есть
  второй IT» (fall-through гарда) для смены роли и для деактивации.
- ``admin_operators_wizard``: реальное тело ``_safe_get_chat_members``
  (None-гард, успех через ``ChatMembersManager.list_all``, проглатывание
  ошибки); fallback ``_full_name_from_member`` (``User <id>``); кандидат
  без ``user_id`` и кандидат-сам-оператор в ``_show_from_group``;
  ``get_chat_member`` вернул None в ``_start_add_with_picked``;
  ``_apply_name_keep`` (wrong-state и пустой suggested→edit);
  ``_start_name_edit`` без wizard; ``_show_add_confirm`` без state;
  ``_confirm_save`` без state и с битым target_id;
  ``handle_operators_wizard_text`` на незнакомом step → False.

Стиль — как в характеризационном файле: SimpleNamespace-фейки,
``tests._helpers.fake_session_scope`` вместо Postgres, точечный ``patch``
сервис-слоя; реальные клавиатуры ``kbds.op_*`` не мокаем. Без maxapi —
skip, в CI идёт.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


# ── фабрики / хелперы (как в характеризационном файле) ────────────────────


def _make_event(*, user_id: int = 7) -> SimpleNamespace:
    return make_event(chat_id=555, user_id=user_id)


@pytest.fixture(autouse=True)
def _clean_wizards():
    from aemr_bot.handlers import admin_operators

    admin_operators._op_wizards.clear()
    yield
    admin_operators._op_wizards.clear()


def _sent_text(event) -> str:
    return event.bot.send_message.call_args.kwargs["text"]


class _patch_session_scope_all:
    _TARGETS = (
        "aemr_bot.handlers.admin_operators_list.session_scope",
        "aemr_bot.handlers.admin_operators_roles.session_scope",
        "aemr_bot.handlers.admin_operators_wizard.session_scope",
    )

    def __enter__(self):
        self._patches = [patch(t, _fake_session_scope) for t in self._TARGETS]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


def _it_ctx():
    return (
        patch(
            "aemr_bot.handlers.admin_operators.ensure_role",
            AsyncMock(return_value=True),
        ),
        _patch_session_scope_all(),
        patch("aemr_bot.utils.event.ack_callback", AsyncMock()),
    )


# ── Фасад: диспетчер name_edit ───────────────────────────────────────────


class TestFacadeNameEditDispatch:
    @pytest.mark.asyncio
    async def test_name_edit_routes_to_start_name_edit(self) -> None:
        """`op:opadd:name_edit` при живом wizard → step переходит в
        awaiting_name и показывается экран ввода ФИО (фасад дёргает
        _wizard._start_name_edit — ветка 174-175 диспетчера)."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        admin_operators._op_wizard_set(
            7, step="picked_role", target_id=42, role="aemr",
            suggested_name="Мария Иванова", source="group",
        )
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack:
            await admin_operators.run_operators_action(event, "op:opadd:name_edit")
        state = admin_operators._op_wizard_get(7)
        assert state["step"] == "awaiting_name"
        text = _sent_text(event)
        assert "ФИО" in text


# ── Фасад: неизвестный op:opadd:* suffix — тихий no-op ───────────────────


class TestFacadeUnknownSuffix:
    @pytest.mark.asyncio
    async def test_unknown_opadd_suffix_does_nothing(self) -> None:
        """`op:opadd:bogus` не совпал ни с одной веткой → только ack, ни
        одного экрана, wizard не тронут (фасад 179->exit, False-сторона
        последнего if)."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack:
            await admin_operators.run_operators_action(event, "op:opadd:bogus")
        event.bot.send_message.assert_not_called()
        assert admin_operators._op_wizard_get(7) is None


# ── Список: счётчик деактивированных ─────────────────────────────────────


class TestOperatorsListInactiveCounter:
    @pytest.mark.asyncio
    async def test_header_shows_inactive_count(self) -> None:
        """Список с деактивированными → шапка содержит «деактивированных N»
        (ветка admin_operators_list:37)."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        ops = [
            SimpleNamespace(max_user_id=1, role="it",
                            full_name="Активный IT", is_active=True),
            SimpleNamespace(max_user_id=2, role="aemr",
                            full_name="Спящий", is_active=False),
        ]
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_list.operators_service.list_all",
            AsyncMock(return_value=ops),
        ):
            await admin_operators.run_operators_action(event, "op:opadd:list")
        text = _sent_text(event)
        assert "деактивированных 1" in text
        assert "активных 1" in text


# ── Roles: невалидный int в payload каждого хендлера ─────────────────────


class TestRolesBadIntPayload:
    @pytest.mark.asyncio
    async def test_show_role_change_bad_int_acks_no_screen(self) -> None:
        """`op:oprole:abc` → int() падает → ack и выход без экрана
        (admin_operators_roles:37-39)."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        get_any = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.get_any",
            get_any,
        ):
            await admin_operators.run_operators_action(event, "op:oprole:abc")
        event.bot.send_message.assert_not_called()
        get_any.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_role_change_bad_target_int_acks(self) -> None:
        """`op:opchrole:abc:aemr` → parts[0] не int → ack и выход без
        экрана (admin_operators_roles:78-80)."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        change_role = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.change_role",
            change_role,
        ):
            await admin_operators.run_operators_action(
                event, "op:opchrole:abc:aemr"
            )
        event.bot.send_message.assert_not_called()
        change_role.assert_not_called()

    @pytest.mark.asyncio
    async def test_show_deactivate_confirm_bad_int_acks(self) -> None:
        """`op:opdeact:abc` → int() падает → ack без экрана
        (admin_operators_roles:154-156)."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        get = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.get", get
        ):
            await admin_operators.run_operators_action(event, "op:opdeact:abc")
        event.bot.send_message.assert_not_called()
        get.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_deactivate_bad_int_acks(self) -> None:
        """`op:opdeact_ok:abc` → int() падает → ack без экрана
        (admin_operators_roles:205-207)."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        deactivate = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.deactivate",
            deactivate,
        ):
            await admin_operators.run_operators_action(event, "op:opdeact_ok:abc")
        event.bot.send_message.assert_not_called()
        deactivate.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_reactivate_bad_int_acks(self) -> None:
        """`op:opreact:abc` → int() падает → ack без экрана
        (admin_operators_roles:257-259)."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        upsert = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.upsert",
            upsert,
        ):
            await admin_operators.run_operators_action(event, "op:opreact:abc")
        event.bot.send_message.assert_not_called()
        upsert.assert_not_called()


# ── Roles: _apply_role_change — цель None / деактивирована ────────────────


class TestApplyRoleChangeTargetMissing:
    @pytest.mark.asyncio
    async def test_target_none_says_not_found(self) -> None:
        """get_any→None внутри _apply_role_change → «не найден или
        деактивирован», change_role не зовём (admin_operators_roles:
        101-106)."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        change_role = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.get_any",
            AsyncMock(return_value=None),
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.change_role",
            change_role,
        ):
            await admin_operators.run_operators_action(
                event, "op:opchrole:42:aemr"
            )
        assert "не найден" in _sent_text(event)
        change_role.assert_not_called()

    @pytest.mark.asyncio
    async def test_target_inactive_says_not_found(self) -> None:
        """Цель деактивирована (is_active=False) → тот же экран; ветка
        `op is None or not op.is_active` по второй части."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        op = SimpleNamespace(
            max_user_id=42, full_name="Спящий", role="aemr", is_active=False
        )
        change_role = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.get_any",
            AsyncMock(return_value=op),
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.change_role",
            change_role,
        ):
            await admin_operators.run_operators_action(
                event, "op:opchrole:42:coordinator"
            )
        assert "деактивирован" in _sent_text(event)
        change_role.assert_not_called()


# ── Roles: понижение IT когда есть второй IT (fall-through гарда) ─────────


class TestDemoteItWhenSecondItExists:
    @pytest.mark.asyncio
    async def test_it_to_aemr_with_second_it_succeeds(self) -> None:
        """IT→aemr при active_it=2 → гард `<=1` не срабатывает (ветка
        113->124), change_role вызывается с OperatorRole.AEMR, пишется
        audit, экран «Роль изменена»."""
        from aemr_bot.db.models import OperatorRole
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        op = SimpleNamespace(
            max_user_id=42, full_name="Второй IT", role="it", is_active=True
        )
        change_role = AsyncMock()
        write_audit = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.get_any",
            AsyncMock(return_value=op),
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service."
            "count_active_by_role",
            AsyncMock(return_value=2),
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.change_role",
            change_role,
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.write_audit",
            write_audit,
        ):
            await admin_operators.run_operators_action(event, "op:opchrole:42:aemr")
        change_role.assert_awaited_once()
        assert change_role.await_args.args[2] == OperatorRole.AEMR
        write_audit.assert_awaited_once()
        akw = write_audit.await_args.kwargs
        assert akw["details"] == {"old_role": "it", "new_role": "aemr"}
        assert "Роль изменена" in _sent_text(event)


# ── Roles: деактивация IT когда есть второй IT (fall-through гарда) ───────


class TestDeactivateItWhenSecondItExists:
    @pytest.mark.asyncio
    async def test_it_deactivate_with_second_it_succeeds(self) -> None:
        """Деактивация IT при active_it=2 → внутренний гард `<=1` не
        срабатывает (ветка 229->236), deactivate вызывается, пишется
        audit operator_deactivate, экран «Деактивирован»."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        op = SimpleNamespace(max_user_id=42, full_name="Второй IT", role="it")
        deactivate = AsyncMock()
        write_audit = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.get",
            AsyncMock(return_value=op),
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service."
            "count_active_by_role",
            AsyncMock(return_value=2),
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.deactivate",
            deactivate,
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.write_audit",
            write_audit,
        ):
            await admin_operators.run_operators_action(event, "op:opdeact_ok:42")
        deactivate.assert_awaited_once()
        assert deactivate.await_args.args[1] == 42
        write_audit.assert_awaited_once()
        akw = write_audit.await_args.kwargs
        assert akw["action"] == "operator_deactivate"
        assert akw["details"] == {"role": "it", "full_name": "Второй IT"}
        assert "Деактивирован" in _sent_text(event)


# ── Roles: _apply_deactivate — активного оператора нет ───────────────────


class TestApplyDeactivateNotFound:
    @pytest.mark.asyncio
    async def test_op_none_says_not_found(self) -> None:
        """get→None внутри _apply_deactivate → «Активный оператор не
        найден», deactivate не зовём (admin_operators_roles:219-224)."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        deactivate = AsyncMock()
        write_audit = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.get",
            AsyncMock(return_value=None),
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.deactivate",
            deactivate,
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.write_audit",
            write_audit,
        ):
            await admin_operators.run_operators_action(event, "op:opdeact_ok:42")
        assert "Активный оператор не найден" in _sent_text(event)
        deactivate.assert_not_called()
        write_audit.assert_not_called()


# ── Wizard: _show_add_confirm — нет state (защитный return) ───────────────


class TestShowAddConfirmNoState:
    @pytest.mark.asyncio
    async def test_no_state_returns_silently(self) -> None:
        """_show_add_confirm без активного wizard → ранний bare return,
        экран не рисуется (wizard:387)."""
        from aemr_bot.handlers import admin_operators_wizard as w

        event = _make_event(user_id=7)
        # wizard пуст (autouse-фикстура очистила) → state None.
        await w._show_add_confirm(event, 7)
        event.bot.send_message.assert_not_called()


# ── Wizard: _safe_get_chat_members — реальное тело ───────────────────────


class TestSafeGetChatMembers:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_admin_group(self) -> None:
        """admin_group_id=None → ранний return [] (wizard:95-98)."""
        from aemr_bot.handlers import admin_operators_wizard as w

        with patch.object(w.cfg, "admin_group_id", None):
            result = await w._safe_get_chat_members(MagicMock())
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_members_via_manager_list_all(self) -> None:
        """admin_group_id задан → ChatMembersManager.list_all() даёт
        список, который и возвращается (wizard:99-102)."""
        from aemr_bot.handlers import admin_operators_wizard as w

        members = [SimpleNamespace(user_id=1), SimpleNamespace(user_id=2)]

        class _FakeManager:
            def __init__(self, *, bot, chat_id):
                self.chat_id = chat_id

            async def list_all(self, *, count: int = 100):
                return members

        with patch.object(w.cfg, "admin_group_id", 555), patch(
            "maxapi.types.chats.ChatMembersManager", _FakeManager
        ):
            result = await w._safe_get_chat_members(MagicMock())
        assert result == members

    @pytest.mark.asyncio
    async def test_swallows_exception_returns_empty(self) -> None:
        """list_all() бросает → исключение проглатывается, возвращается []
        (wizard:103-105)."""
        from aemr_bot.handlers import admin_operators_wizard as w

        class _BoomManager:
            def __init__(self, *, bot, chat_id):
                pass

            async def list_all(self, *, count: int = 100):
                raise RuntimeError("MAX down")

        with patch.object(w.cfg, "admin_group_id", 555), patch(
            "maxapi.types.chats.ChatMembersManager", _BoomManager
        ):
            result = await w._safe_get_chat_members(MagicMock())
        assert result == []


# ── Wizard: _full_name_from_member — fallback ────────────────────────────


class TestFullNameFromMember:
    def test_first_and_last(self) -> None:
        from aemr_bot.handlers import admin_operators_wizard as w

        m = SimpleNamespace(first_name="Анна", last_name="Иванова", user_id=5)
        assert w._full_name_from_member(m) == "Анна Иванова"

    def test_only_first(self) -> None:
        from aemr_bot.handlers import admin_operators_wizard as w

        m = SimpleNamespace(first_name="Анна", last_name=None, user_id=5)
        assert w._full_name_from_member(m) == "Анна"

    def test_no_names_falls_back_to_user_id(self) -> None:
        """Ни first, ни last → «User <id>» (wizard:115)."""
        from aemr_bot.handlers import admin_operators_wizard as w

        m = SimpleNamespace(first_name=None, last_name=None, user_id=777)
        assert w._full_name_from_member(m) == "User 777"


# ── Wizard: _show_from_group — кандидат без user_id и сам-оператор ────────


class TestShowFromGroupEdgeCandidates:
    @pytest.mark.asyncio
    async def test_member_without_user_id_skipped_and_self_listed(self) -> None:
        """Участник без user_id пропускается (continue, wizard:147); сам
        оператор (user_id == operator_id) попадает кандидатом с пометкой
        «(вы) — уже оператор» (wizard:156-157). В итоге доступных к
        добавлению 0 (есть только сам IT)."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        event.bot.me = SimpleNamespace(user_id=999)
        members = [
            SimpleNamespace(user_id=None, first_name="Безымянный",
                            last_name="", is_bot=False),  # → continue
            SimpleNamespace(user_id=7, first_name="Сам", last_name="IT",
                            is_bot=False),  # → self-кандидат
        ]
        existing: list = []  # operator_id=7 нет в БД-списке → ветка self
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_wizard._safe_get_chat_members",
            AsyncMock(return_value=members),
        ), patch(
            "aemr_bot.handlers.admin_operators_wizard.operators_service.list_all",
            AsyncMock(return_value=existing),
        ):
            await admin_operators.run_operators_action(event, "op:opadd:from_group")
        text = _sent_text(event)
        # Один валидный кандидат (сам IT), безымянный отброшен.
        assert "Участников группы: 1" in text
        assert "Доступно для добавления: 0" in text


# ── Wizard: _start_add_with_picked — get_chat_member вернул None ─────────


class TestStartAddWithPickedMemberNone:
    @pytest.mark.asyncio
    async def test_member_none_keeps_suggested_none(self) -> None:
        """get_chat_member вернул None (не исключение) → ветка `if member
        is not None` ложна (wizard:204->209), suggested_name=None, wizard
        ставится, экран обещает ручной ввод имени."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        event.bot.get_chat_member = AsyncMock(return_value=None)
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack:
            await admin_operators.run_operators_action(event, "op:opadd:pick:42")
        state = admin_operators._op_wizard_get(7)
        assert state is not None
        assert state["step"] == "awaiting_role"
        assert state.get("suggested_name") is None
        assert "вручную" in _sent_text(event)


# ── Wizard: _apply_name_keep — wrong-state и пустой suggested ─────────────


class TestApplyNameKeep:
    @pytest.mark.asyncio
    async def test_wrong_state_says_closed(self) -> None:
        """name_keep когда step != picked_role → «Мастер закрыт»
        (wizard:322-327)."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        admin_operators._op_wizard_set(7, step="awaiting_role", target_id=42)
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack:
            await admin_operators.run_operators_action(event, "op:opadd:name_keep")
        assert "Мастер закрыт" in _sent_text(event)

    @pytest.mark.asyncio
    async def test_picked_role_without_suggested_starts_name_edit(self) -> None:
        """name_keep в picked_role, но suggested_name пуст → делегирует в
        _start_name_edit (wizard:330-331): step→awaiting_name, экран
        ввода ФИО."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        admin_operators._op_wizard_set(
            7, step="picked_role", target_id=42, role="aemr",
            suggested_name=None, source="manual",
        )
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack:
            await admin_operators.run_operators_action(event, "op:opadd:name_keep")
        state = admin_operators._op_wizard_get(7)
        assert state["step"] == "awaiting_name"
        assert "ФИО" in _sent_text(event)


# ── Wizard: _start_name_edit без wizard ──────────────────────────────────


class TestStartNameEditNoWizard:
    @pytest.mark.asyncio
    async def test_name_edit_no_wizard_says_closed(self) -> None:
        """`op:opadd:name_edit` без активного wizard → «Мастер закрыт»
        (wizard:338-347, ветка state is None)."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack:
            await admin_operators.run_operators_action(event, "op:opadd:name_edit")
        assert "Мастер закрыт" in _sent_text(event)


# ── Wizard: _confirm_save — нет state / битый target_id ──────────────────


class TestConfirmSaveGuards:
    @pytest.mark.asyncio
    async def test_no_state_says_closed(self) -> None:
        """confirm без активного wizard → «Мастер закрыт» (wizard:
        409-414), upsert не зовём."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        upsert = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_wizard.operators_service.upsert",
            upsert,
        ):
            await admin_operators.run_operators_action(event, "op:opadd:confirm")
        assert "Мастер закрыт" in _sent_text(event)
        upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_target_id_not_int_aborts(self) -> None:
        """ready_to_confirm с нечисловым target_id → «ID не задан, начните
        заново» (wizard:417-423), upsert не зовём."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        admin_operators._op_wizard_set(
            7, step="ready_to_confirm", target_id="не-число",
            role="aemr", full_name="Кто-то", source="manual",
        )
        upsert = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_wizard.operators_service.upsert",
            upsert,
        ):
            await admin_operators.run_operators_action(event, "op:opadd:confirm")
        assert "ID не задан" in _sent_text(event)
        upsert.assert_not_called()


# ── Wizard: handle_operators_wizard_text — незнакомый step ───────────────


class TestHandleWizardTextUnknownStep:
    @pytest.mark.asyncio
    async def test_unknown_step_returns_false(self) -> None:
        """Текст при step, который не awaiting_id и не awaiting_name (напр.
        picked_role) → перехватчик не поглощает сообщение, return False
        (wizard:517)."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        admin_operators._op_wizard_set(
            7, step="picked_role", target_id=42, role="aemr",
            suggested_name="Имя", source="group",
        )
        result = await admin_operators.handle_operators_wizard_text(
            event, "любой текст"
        )
        assert result is False
        event.bot.send_message.assert_not_called()
