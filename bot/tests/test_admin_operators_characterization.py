"""Характеризационные тесты для handlers/admin_operators.

Фиксируют ТЕКУЩЕЕ поведение god-object'а управления операторами как
страховочную сетку перед декомпозицией. Прод-код НЕ меняем — только
закрепляем наблюдаемые контракты (роль-гарды, успешные пути, гарды
единственного IT, валидацию, запись в audit_log).

Существующий ``test_admin_operators.py`` уже покрывает:
- ``_op_wizard_get/set/drop`` (TTL, обновление);
- ``run_operators_menu`` (not-it / it / edit свежей карточки);
- ``run_operators_action`` для семьи ``op:opadd:*`` (start / list /
  cancel / role:* valid|invalid|wrong-state);
- ``handle_operators_wizard_text`` (id valid|invalid, name short|self|
  upsert|update).

Здесь — ДОПОЛНИТЕЛЬНЫЕ ветки, которых там нет. Они закрывают «вторую
половину» модуля: карточку оператора, смену роли через карточку,
деактивацию (подтверждение + применение), реактивацию, добавление из
участников группы и переход к выбору роли. Особый акцент — на гардах
самомодификации, гарде «единственный активный IT» и на содержимом
записей ``operators_service.write_audit`` (action + details), потому что
именно это — наблюдаемый контракт 152-ФЗ-журналирования, который рефактор
обязан сохранить байт-в-байт.

Стиль — как в ``test_admin_appeal_ops_characterization.py``:
SimpleNamespace-фейки, ``tests._helpers.fake_session_scope`` вместо
реального Postgres, точечный ``patch`` сервис-слоя. Реальные клавиатуры
``kbds.op_*`` НЕ мокаем — они чистые билдеры и отрабатывают на фейках.
Локально без maxapi — skip, в CI идёт.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


# ── фабрики ───────────────────────────────────────────────────────────────


def _make_event(*, user_id: int = 7) -> SimpleNamespace:
    # chat_id=555 — служебная группа в этих тестах (как в соседних файлах).
    # callback не нужен: дispatcher вызывает ack_callback через
    # aemr_bot.utils.event.ack_callback, который на фейке без .ack — no-op.
    return make_event(chat_id=555, user_id=user_id)


@pytest.fixture(autouse=True)
def _clean_wizards():
    """Глобальный _op_wizards мог остаться от предыдущего теста — изолируем."""
    from aemr_bot.handlers import admin_operators

    admin_operators._op_wizards.clear()
    yield
    admin_operators._op_wizards.clear()


def _sent_text(event) -> str:
    """Последний текст, ушедший через send_or_edit_screen → bot.send_message.

    Все error/guard/success-экраны admin_operators идут через
    send_or_edit_screen, который на не-callback событии всегда делает
    bot.send_message(chat_id=…, text=…). Эту функцию НЕ мокаем — она
    реально дёргает bot.send_message, как и в боевом пути.
    """
    return event.bot.send_message.call_args.kwargs["text"]


# Базовый набор патчей для action-handler'ов: пройти роль-гард IT и
# подменить session_scope заглушкой. ack_callback патчим у источника —
# как в существующих характеризационных тестах (на фейке он и так no-op,
# патч страхует от любых будущих изменений ack-формы).
#
# ВАЖНО (декомпозиция god-объекта): ensure_role остался в фасаде
# admin_operators (его дёргает run_operators_action), а session_scope и
# operators_service переехали в подмодули admin_operators_{list,roles,
# wizard}. run_operators_action диспетчеризует по payload в нужный
# подмодуль, поэтому session_scope патчим во ВСЕХ трёх — лишние патчи
# безвредны, а тест-специфичные operators_service.* каждый тест целит уже
# в свой подмодуль (фасадный re-export НЕ покрывает patch — урок PR #139).
class _patch_session_scope_all:
    """Контекст-менеджер: подменяет session_scope во всех подмодулях
    admin_operators сразу (list/roles/wizard)."""

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


# ── Диспетчер run_operators_action: общие гарды ──────────────────────────


class TestActionDispatcherGuards:
    @pytest.mark.asyncio
    async def test_not_it_blocks_everything(self) -> None:
        """Не-IT роль → ensure_role вернул False, ни одна ветка не
        выполняется, в чат ничего не уходит."""
        from aemr_bot.handlers import admin_operators

        event = _make_event()
        with patch(
            "aemr_bot.handlers.admin_operators.ensure_role",
            AsyncMock(return_value=False),
        ):
            await admin_operators.run_operators_action(event, "op:opcard:42")
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_operator_id_acks_and_returns(self) -> None:
        """IT прошёл, но get_user_id вернул None (нет sender id) → тихий
        ack и выход, экранов не шлём."""
        from aemr_bot.handlers import admin_operators

        event = _make_event()
        with patch(
            "aemr_bot.handlers.admin_operators.ensure_role",
            AsyncMock(return_value=True),
        ), patch(
            "aemr_bot.handlers.admin_operators.get_user_id",
            return_value=None,
        ), patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_operators.run_operators_action(event, "op:opcard:42")
        event.bot.send_message.assert_not_called()


# ── Карточка оператора: _show_operator_card ──────────────────────────────


class TestShowOperatorCard:
    @pytest.mark.asyncio
    async def test_invalid_target_id_acks_no_screen(self) -> None:
        """`op:opcard:abc` — нечисловой id → ack и выход, экран не
        рисуется."""
        from aemr_bot.handlers import admin_operators

        event = _make_event()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack:
            await admin_operators.run_operators_action(event, "op:opcard:abc")
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_found_message(self) -> None:
        """Оператора с таким id нет (get_any→None) → текст «не найден»."""
        from aemr_bot.handlers import admin_operators

        event = _make_event()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_list.operators_service.get_any",
            AsyncMock(return_value=None),
        ), patch(
            "aemr_bot.handlers.admin_operators_list.operators_service."
            "count_active_by_role",
            AsyncMock(return_value=0),
        ):
            await admin_operators.run_operators_action(event, "op:opcard:42")
        assert "не найден" in _sent_text(event)

    @pytest.mark.asyncio
    async def test_happy_active_non_self_can_deactivate(self) -> None:
        """Активный coordinator, не вы, есть другие IT → карточка с ФИО,
        ID, ролью, статусом «активен», без warning-блоков."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        op = SimpleNamespace(
            max_user_id=42,
            full_name="Петрова Анна",
            role="coordinator",
            is_active=True,
            created_at=None,
        )
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_list.operators_service.get_any",
            AsyncMock(return_value=op),
        ), patch(
            "aemr_bot.handlers.admin_operators_list.operators_service."
            "count_active_by_role",
            AsyncMock(return_value=2),
        ):
            await admin_operators.run_operators_action(event, "op:opcard:42")
        text = _sent_text(event)
        assert "Петрова Анна" in text
        assert "42" in text
        assert "coordinator" in text
        assert "активен" in text
        # Не вы и не единственный IT — предупреждений нет.
        assert "Это вы" not in text
        assert "единственный активный IT" not in text

    @pytest.mark.asyncio
    async def test_self_card_shows_you_warning(self) -> None:
        """Карточка самого себя (max_user_id == operator_id) → строка
        «⚠️ Это вы. Себя через меню изменить нельзя.»"""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=42)
        op = SimpleNamespace(
            max_user_id=42,
            full_name="Я Сам",
            role="coordinator",
            is_active=True,
            created_at=None,
        )
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_list.operators_service.get_any",
            AsyncMock(return_value=op),
        ), patch(
            "aemr_bot.handlers.admin_operators_list.operators_service."
            "count_active_by_role",
            AsyncMock(return_value=2),
        ):
            await admin_operators.run_operators_action(event, "op:opcard:42")
        assert "Это вы" in _sent_text(event)

    @pytest.mark.asyncio
    async def test_sole_active_it_shows_lock_warning(self) -> None:
        """Активный IT, других активных IT нет (count<=1) → карточка
        содержит предупреждение про «единственный активный IT» и
        деактивация заблокирована."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        op = SimpleNamespace(
            max_user_id=99,
            full_name="Единственный Айтишник",
            role="it",
            is_active=True,
            created_at=None,
        )
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_list.operators_service.get_any",
            AsyncMock(return_value=op),
        ), patch(
            "aemr_bot.handlers.admin_operators_list.operators_service."
            "count_active_by_role",
            AsyncMock(return_value=1),
        ):
            await admin_operators.run_operators_action(event, "op:opcard:99")
        assert "единственный активный IT" in _sent_text(event)


# ── Смена роли — picker: _show_role_change ───────────────────────────────


class TestShowRoleChange:
    @pytest.mark.asyncio
    async def test_self_role_change_blocked(self) -> None:
        """`op:oprole:<self>` → «Изменить свою роль через меню нельзя»;
        в БД не ходим."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        get_any = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.get_any",
            get_any,
        ):
            await admin_operators.run_operators_action(event, "op:oprole:7")
        assert "свою роль" in _sent_text(event)
        get_any.assert_not_called()

    @pytest.mark.asyncio
    async def test_inactive_target_not_found(self) -> None:
        """Цель деактивирована (is_active=False) → «не найден или
        деактивирован», picker не показываем."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        op = SimpleNamespace(
            max_user_id=42, full_name="Спящий", role="aemr", is_active=False
        )
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.get_any",
            AsyncMock(return_value=op),
        ):
            await admin_operators.run_operators_action(event, "op:oprole:42")
        assert "деактивирован" in _sent_text(event)

    @pytest.mark.asyncio
    async def test_happy_opens_role_picker(self) -> None:
        """Активная цель → экран смены роли с текущей ролью и ФИО."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        op = SimpleNamespace(
            max_user_id=42, full_name="Сидоров С.С.", role="aemr", is_active=True
        )
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.get_any",
            AsyncMock(return_value=op),
        ):
            await admin_operators.run_operators_action(event, "op:oprole:42")
        text = _sent_text(event)
        assert "Смена роли" in text
        assert "Сидоров С.С." in text
        assert "aemr" in text


# ── Применение смены роли — CRUD + audit: _apply_role_change ─────────────


class TestApplyRoleChange:
    @pytest.mark.asyncio
    async def test_malformed_payload_acks(self) -> None:
        """`op:opchrole:42` без `:role` → split даёт 1 часть, ack и выход
        без экрана."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack:
            await admin_operators.run_operators_action(event, "op:opchrole:42")
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_role_rejected(self) -> None:
        """`op:opchrole:42:bogus` → роль не из OperatorRole → «неизвестна»,
        change_role не зовём."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        change_role = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.change_role",
            change_role,
        ):
            await admin_operators.run_operators_action(
                event, "op:opchrole:42:bogus"
            )
        assert "неизвестна" in _sent_text(event)
        change_role.assert_not_called()

    @pytest.mark.asyncio
    async def test_self_role_change_blocked(self) -> None:
        """`op:opchrole:<self>:aemr` → «Изменить свою роль нельзя»."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        change_role = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.change_role",
            change_role,
        ):
            await admin_operators.run_operators_action(event, "op:opchrole:7:aemr")
        assert "свою роль" in _sent_text(event)
        change_role.assert_not_called()

    @pytest.mark.asyncio
    async def test_sole_it_demotion_blocked_no_audit(self) -> None:
        """IT→aemr при единственном активном IT (count<=1) → блок,
        change_role И write_audit НЕ зовём."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        op = SimpleNamespace(
            max_user_id=42, full_name="Единственный IT", role="it", is_active=True
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
            AsyncMock(return_value=1),
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.change_role",
            change_role,
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.write_audit",
            write_audit,
        ):
            await admin_operators.run_operators_action(event, "op:opchrole:42:aemr")
        assert "единственного активного" in _sent_text(event)
        change_role.assert_not_called()
        write_audit.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_change_writes_audit(self) -> None:
        """Успешная смена aemr→coordinator → change_role вызван с
        OperatorRole(new); write_audit с action='operator_role_change' и
        details={'old_role','new_role'}; экран «Роль изменена»."""
        from aemr_bot.db.models import OperatorRole
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        op = SimpleNamespace(
            max_user_id=42, full_name="Сидоров С.С.", role="aemr", is_active=True
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
            await admin_operators.run_operators_action(
                event, "op:opchrole:42:coordinator"
            )
        # change_role(session, target_id, OperatorRole.COORDINATOR)
        change_role.assert_awaited_once()
        assert change_role.await_args.args[1] == 42
        assert change_role.await_args.args[2] == OperatorRole.COORDINATOR
        # audit-контракт
        write_audit.assert_awaited_once()
        akw = write_audit.await_args.kwargs
        assert akw["action"] == "operator_role_change"
        assert akw["operator_max_user_id"] == 7
        assert akw["details"] == {"old_role": "aemr", "new_role": "coordinator"}
        # экран успеха
        text = _sent_text(event)
        assert "Роль изменена" in text
        assert "aemr" in text and "coordinator" in text


# ── Деактивация: подтверждение — _show_deactivate_confirm ────────────────


class TestShowDeactivateConfirm:
    @pytest.mark.asyncio
    async def test_self_blocked(self) -> None:
        """`op:opdeact:<self>` → «Себя деактивировать нельзя», в БД не
        идём."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        get = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.get", get
        ):
            await admin_operators.run_operators_action(event, "op:opdeact:7")
        assert "Себя деактивировать нельзя" in _sent_text(event)
        get.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_found_active(self) -> None:
        """Активного оператора нет (get→None) → «Активный оператор не
        найден»."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.get",
            AsyncMock(return_value=None),
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service."
            "count_active_by_role",
            AsyncMock(return_value=0),
        ):
            await admin_operators.run_operators_action(event, "op:opdeact:42")
        assert "Активный оператор не найден" in _sent_text(event)

    @pytest.mark.asyncio
    async def test_sole_it_blocked(self) -> None:
        """IT-цель при единственном активном IT → блок-экран, кнопки
        подтверждения не показываем."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        op = SimpleNamespace(max_user_id=42, full_name="IT One", role="it")
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.get",
            AsyncMock(return_value=op),
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service."
            "count_active_by_role",
            AsyncMock(return_value=1),
        ):
            await admin_operators.run_operators_action(event, "op:opdeact:42")
        assert "единственного активного IT" in _sent_text(event)

    @pytest.mark.asyncio
    async def test_happy_shows_confirm(self) -> None:
        """Не-IT активная цель → экран подтверждения деактивации с ФИО и
        ролью."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        op = SimpleNamespace(max_user_id=42, full_name="Петров П.", role="aemr")
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.get",
            AsyncMock(return_value=op),
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service."
            "count_active_by_role",
            AsyncMock(return_value=2),
        ):
            await admin_operators.run_operators_action(event, "op:opdeact:42")
        text = _sent_text(event)
        assert "Деактивировать оператора" in text
        assert "Петров П." in text


# ── Деактивация: применение — CRUD + audit: _apply_deactivate ────────────


class TestApplyDeactivate:
    @pytest.mark.asyncio
    async def test_self_blocked(self) -> None:
        """`op:opdeact_ok:<self>` → «Себя деактивировать нельзя»."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        deactivate = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.deactivate",
            deactivate,
        ):
            await admin_operators.run_operators_action(event, "op:opdeact_ok:7")
        assert "Себя деактивировать нельзя" in _sent_text(event)
        deactivate.assert_not_called()

    @pytest.mark.asyncio
    async def test_sole_it_blocked_no_audit(self) -> None:
        """IT-цель при единственном активном IT → блок; deactivate и
        write_audit НЕ зовём."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        op = SimpleNamespace(max_user_id=42, full_name="IT One", role="it")
        deactivate = AsyncMock()
        write_audit = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.get",
            AsyncMock(return_value=op),
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service."
            "count_active_by_role",
            AsyncMock(return_value=1),
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.deactivate",
            deactivate,
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.write_audit",
            write_audit,
        ):
            await admin_operators.run_operators_action(event, "op:opdeact_ok:42")
        assert "единственного активного IT" in _sent_text(event)
        deactivate.assert_not_called()
        write_audit.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_deactivate_writes_audit(self) -> None:
        """Успешная деактивация не-IT → deactivate(session, id);
        write_audit action='operator_deactivate' с details role+full_name;
        экран «Деактивирован: <имя> (<роль>)»."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        op = SimpleNamespace(max_user_id=42, full_name="Петров П.", role="aemr")
        deactivate = AsyncMock()
        write_audit = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.get",
            AsyncMock(return_value=op),
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
        assert akw["details"] == {"role": "aemr", "full_name": "Петров П."}
        text = _sent_text(event)
        assert "Деактивирован" in text
        assert "Петров П." in text


# ── Реактивация — CRUD + audit: _apply_reactivate ────────────────────────


class TestApplyReactivate:
    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        """Записи нет (get_any→None) → «Оператор не найден», upsert не
        зовём."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        upsert = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.get_any",
            AsyncMock(return_value=None),
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.upsert", upsert
        ):
            await admin_operators.run_operators_action(event, "op:opreact:42")
        assert "не найден" in _sent_text(event)
        upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_already_active(self) -> None:
        """Оператор уже активен → «уже активен», upsert не зовём."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        op = SimpleNamespace(
            max_user_id=42, full_name="Активный", role="aemr", is_active=True
        )
        upsert = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.get_any",
            AsyncMock(return_value=op),
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.upsert", upsert
        ):
            await admin_operators.run_operators_action(event, "op:opreact:42")
        assert "уже активен" in _sent_text(event)
        upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_reactivate_writes_audit(self) -> None:
        """Реактивация деактивированного → upsert(max_user_id, full_name,
        OperatorRole(role)); write_audit action='operator_reactivate';
        экран «Реактивирован: <имя> (<роль>)»."""
        from aemr_bot.db.models import OperatorRole
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        op = SimpleNamespace(
            max_user_id=42, full_name="Возвращенец", role="aemr", is_active=False
        )
        upsert = AsyncMock()
        write_audit = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.get_any",
            AsyncMock(return_value=op),
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.upsert", upsert
        ), patch(
            "aemr_bot.handlers.admin_operators_roles.operators_service.write_audit",
            write_audit,
        ):
            await admin_operators.run_operators_action(event, "op:opreact:42")
        upsert.assert_awaited_once()
        ukw = upsert.await_args.kwargs
        assert ukw["max_user_id"] == 42
        assert ukw["full_name"] == "Возвращенец"
        assert ukw["role"] == OperatorRole.AEMR
        write_audit.assert_awaited_once()
        akw = write_audit.await_args.kwargs
        assert akw["action"] == "operator_reactivate"
        text = _sent_text(event)
        assert "Реактивирован" in text
        assert "Возвращенец" in text


# ── Добавление из участников группы: _show_from_group ────────────────────


class TestShowFromGroup:
    @pytest.mark.asyncio
    async def test_no_members_fallback(self) -> None:
        """_safe_get_chat_members вернул пусто → подсказка «Используйте
        По ID вручную», в БД за списком операторов не ходим."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        list_all = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_wizard._safe_get_chat_members",
            AsyncMock(return_value=[]),
        ), patch(
            "aemr_bot.handlers.admin_operators_wizard.operators_service.list_all",
            list_all,
        ):
            await admin_operators.run_operators_action(event, "op:opadd:from_group")
        assert "вручную" in _sent_text(event)
        list_all.assert_not_called()

    @pytest.mark.asyncio
    async def test_candidates_mix_addable_existing_self(self) -> None:
        """Смешанный состав группы: новый кандидат, уже-активный оператор
        и сам IT. Текст фиксирует «Участников группы: 3» и «Доступно для
        добавления: 1» (только новый — addable). Бот и is_bot отфильтрованы.
        """
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        # bot.me.user_id управляет фильтром «не бот сам».
        event.bot.me = SimpleNamespace(user_id=999)
        members = [
            SimpleNamespace(user_id=100, first_name="Новый", last_name="Кандидат",
                            is_bot=False),
            SimpleNamespace(user_id=42, first_name="Уже", last_name="Оператор",
                            is_bot=False),
            SimpleNamespace(user_id=7, first_name="Сам", last_name="IT",
                            is_bot=False),
            SimpleNamespace(user_id=999, first_name="Бот", last_name="",
                            is_bot=True),  # сам бот → отфильтрован
        ]
        existing = [
            SimpleNamespace(max_user_id=42, full_name="Уже Оператор",
                            role="aemr", is_active=True),
            SimpleNamespace(max_user_id=7, full_name="Сам IT",
                            role="it", is_active=True),
        ]
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
        assert "Участников группы: 3" in text
        assert "Доступно для добавления: 1" in text

    @pytest.mark.asyncio
    async def test_only_bot_in_group(self) -> None:
        """В группе один лишь бот (всё отфильтровано) → «нет участников,
        кроме бота»."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        event.bot.me = SimpleNamespace(user_id=999)
        members = [
            SimpleNamespace(user_id=999, first_name="Бот", last_name="",
                            is_bot=True),
        ]
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_wizard._safe_get_chat_members",
            AsyncMock(return_value=members),
        ), patch(
            "aemr_bot.handlers.admin_operators_wizard.operators_service.list_all",
            AsyncMock(return_value=[]),
        ):
            await admin_operators.run_operators_action(event, "op:opadd:from_group")
        assert "кроме бота" in _sent_text(event)


# ── Выбор участника → шаг роли: _start_add_with_picked ───────────────────


class TestStartAddWithPicked:
    @pytest.mark.asyncio
    async def test_pick_self_blocked(self) -> None:
        """`op:opadd:pick:<self>` → «Себя через меню добавить/изменить
        нельзя», wizard НЕ создаётся."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack:
            await admin_operators.run_operators_action(event, "op:opadd:pick:7")
        assert "Себя через меню" in _sent_text(event)
        assert admin_operators._op_wizard_get(7) is None

    @pytest.mark.asyncio
    async def test_pick_bad_int_message(self) -> None:
        """`op:opadd:pick:xx` — нечисловой → «Некорректный выбор.»"""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack:
            await admin_operators.run_operators_action(event, "op:opadd:pick:xx")
        assert "Некорректный выбор" in _sent_text(event)

    @pytest.mark.asyncio
    async def test_pick_happy_sets_wizard_awaiting_role(self) -> None:
        """Выбор валидного участника → подтянули имя из MAX
        (get_chat_member), wizard в step='awaiting_role' с target_id и
        source='group'; экран «Шаг 2 — выбор роли» с именем."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        # get_chat_member возвращает профиль → suggested_name из имени.
        event.bot.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(
                user_id=42, first_name="Мария", last_name="Иванова"
            )
        )
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack:
            await admin_operators.run_operators_action(event, "op:opadd:pick:42")
        state = admin_operators._op_wizard_get(7)
        assert state is not None
        assert state["step"] == "awaiting_role"
        assert state["target_id"] == 42
        assert state["source"] == "group"
        assert state["suggested_name"] == "Мария Иванова"
        text = _sent_text(event)
        assert "Шаг 2" in text
        assert "Мария Иванова" in text

    @pytest.mark.asyncio
    async def test_pick_get_member_fails_name_none(self) -> None:
        """get_chat_member бросает → исключение проглочено, suggested_name
        остаётся None, wizard всё равно ставится и экран обещает ввод
        имени вручную."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        event.bot.get_chat_member = AsyncMock(side_effect=RuntimeError("MAX 500"))
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack:
            await admin_operators.run_operators_action(event, "op:opadd:pick:42")
        state = admin_operators._op_wizard_get(7)
        assert state is not None
        assert state["step"] == "awaiting_role"
        assert state.get("suggested_name") is None
        assert "вручную" in _sent_text(event)


# ── name_keep / confirm-save через picked (group source) ─────────────────


class TestNameKeepAndConfirmGroupSource:
    @pytest.mark.asyncio
    async def test_role_choice_with_suggested_offers_name_choice(self) -> None:
        """В group-флоу после выбора роли при наличии suggested_name
        показывается выбор «как есть / ввести», step→picked_role."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        admin_operators._op_wizard_set(
            7, step="awaiting_role", target_id=42,
            suggested_name="Мария Иванова", source="group",
        )
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack:
            await admin_operators.run_operators_action(event, "op:opadd:role:aemr")
        state = admin_operators._op_wizard_get(7)
        assert state["step"] == "picked_role"
        assert state["role"] == "aemr"
        text = _sent_text(event)
        assert "Шаг 3" in text
        assert "Мария Иванова" in text

    @pytest.mark.asyncio
    async def test_name_keep_advances_to_confirm(self) -> None:
        """`op:opadd:name_keep` при picked_role с suggested_name →
        full_name берётся из MAX, step→ready_to_confirm, экран
        подтверждения."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        admin_operators._op_wizard_set(
            7, step="picked_role", target_id=42, role="aemr",
            suggested_name="Мария Иванова", source="group",
        )
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack:
            await admin_operators.run_operators_action(event, "op:opadd:name_keep")
        state = admin_operators._op_wizard_get(7)
        assert state["step"] == "ready_to_confirm"
        assert state["full_name"] == "Мария Иванова"
        assert "Подтверждение" in _sent_text(event)

    @pytest.mark.asyncio
    async def test_confirm_save_group_source_writes_audit_source(self) -> None:
        """Финальное сохранение из group-флоу → upsert + write_audit
        action='operator_upsert' с details.source='group'; экран
        «Добавлено» (existed=False); wizard сброшен."""
        from aemr_bot.db.models import OperatorRole
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        admin_operators._op_wizard_set(
            7, step="ready_to_confirm", target_id=42, role="aemr",
            full_name="Мария Иванова", source="group",
        )
        upsert = AsyncMock()
        write_audit = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_wizard.operators_service.get",
            AsyncMock(return_value=None),
        ), patch(
            "aemr_bot.handlers.admin_operators_wizard.operators_service.upsert", upsert
        ), patch(
            "aemr_bot.handlers.admin_operators_wizard.operators_service.write_audit",
            write_audit,
        ):
            await admin_operators.run_operators_action(event, "op:opadd:confirm")
        upsert.assert_awaited_once()
        ukw = upsert.await_args.kwargs
        assert ukw["max_user_id"] == 42
        assert ukw["full_name"] == "Мария Иванова"
        assert ukw["role"] == OperatorRole.AEMR
        write_audit.assert_awaited_once()
        akw = write_audit.await_args.kwargs
        assert akw["action"] == "operator_upsert"
        assert akw["details"]["source"] == "group"
        assert "Добавлено" in _sent_text(event)
        assert admin_operators._op_wizard_get(7) is None

    @pytest.mark.asyncio
    async def test_confirm_save_missing_role_aborts(self) -> None:
        """ready_to_confirm без role → «Не хватает данных, начните
        заново», upsert не зовём."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        admin_operators._op_wizard_set(
            7, step="ready_to_confirm", target_id=42, full_name="Без Роли",
        )
        upsert = AsyncMock()
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack, patch(
            "aemr_bot.handlers.admin_operators_wizard.operators_service.upsert", upsert
        ):
            await admin_operators.run_operators_action(event, "op:opadd:confirm")
        assert "Не хватает данных" in _sent_text(event)
        upsert.assert_not_called()


# ── edit_role откат: _back_to_role_pick ──────────────────────────────────


class TestBackToRolePick:
    @pytest.mark.asyncio
    async def test_edit_role_returns_to_role_picker(self) -> None:
        """`op:opadd:edit_role` при живом wizard → step→awaiting_role,
        экран «Шаг 2 — выбор роли» с прежним target_id."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        admin_operators._op_wizard_set(
            7, step="ready_to_confirm", target_id=42, role="aemr",
            full_name="Кто-то", source="group",
        )
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack:
            await admin_operators.run_operators_action(event, "op:opadd:edit_role")
        state = admin_operators._op_wizard_get(7)
        assert state["step"] == "awaiting_role"
        text = _sent_text(event)
        assert "Шаг 2" in text
        assert "42" in text

    @pytest.mark.asyncio
    async def test_edit_role_no_wizard_says_closed(self) -> None:
        """`op:opadd:edit_role` без активного wizard → «Мастер закрыт.»"""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        ensure, scope, ack = _it_ctx()
        with ensure, scope, ack:
            await admin_operators.run_operators_action(event, "op:opadd:edit_role")
        assert "Мастер закрыт" in _sent_text(event)
