"""Необратимые операторские действия: гарды «последнего IT», самомодификации
и журнал 152-ФЗ.

Спасено из удалённых `test_admin_operators_characterization.py` и
`test_admin_appeal_ops_characterization.py`: там это лежало под видом
«характеризации», хотя проверяет последствия, а не текущую форму кода.
Гарды больше нигде в корпусе не проверяются — `test_admin_operators_
coverage.py` покрывает только зеркальные УСПЕШНЫЕ случаи (второй IT
существует → операция проходит).

Формат докстрингов: «если <поломка>, то <что случится с оператором/
данными>».
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


def _make_event(*, user_id: int = 7) -> SimpleNamespace:
    # chat_id=555 — служебная группа (как в соседних admin-тестах).
    return make_event(chat_id=555, user_id=user_id)


def _sent_text(event) -> str:
    """Последний текст, ушедший через send_or_edit_screen → bot.send_message.

    send_or_edit_screen НЕ мокаем — на не-callback событии он реально
    зовёт bot.send_message, как и в боевом пути.
    """
    return event.bot.send_message.call_args.kwargs["text"]


class _patch_session_scope_all:
    """session_scope подменяется во всех подмодулях admin_operators сразу.

    run_operators_action — фасад, диспетчеризующий в list/roles/wizard;
    патч по месту резолва обязателен (урок PR #139: фасадный re-export
    патч не перехватывает).
    """

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
    """Контексты «за рулём IT»: роль пройдена, БД заглушена, ack тихий."""
    return (
        patch(
            "aemr_bot.handlers.admin_operators.ensure_role",
            AsyncMock(return_value=True),
        ),
        _patch_session_scope_all(),
        patch("aemr_bot.utils.event.ack_callback", AsyncMock()),
    )


@pytest.fixture(autouse=True)
def _clean_wizards():
    from aemr_bot.handlers import admin_operators

    admin_operators._op_wizards.clear()
    yield
    admin_operators._op_wizards.clear()


class TestLastItGuard:
    """Единственный активный IT — точка невозврата: сняв его, никто
    больше не сможет управлять операторами и настройками бота."""

    @pytest.mark.asyncio
    async def test_sole_active_it_cannot_be_demoted(self) -> None:
        """Если гард снять, понижение последнего IT до оператора запрёт
        администрацию снаружи собственного бота: вернуть роль будет
        некому, останется только правка БД руками на сервере."""
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
        # Отказ не должен оставлять следа «роль изменена» в журнале.
        write_audit.assert_not_called()

    @pytest.mark.asyncio
    async def test_sole_active_it_cannot_be_deactivated(self) -> None:
        """То же самое другой кнопкой: деактивация последнего IT оставит
        бота без администратора, а журнал — с записью о действии,
        которого не было."""
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


class TestSelfModificationGuard:
    @pytest.mark.asyncio
    async def test_operator_cannot_change_own_role(self) -> None:
        """Если гард снять, IT сможет тихо переписать себе роль (или
        случайно понизить себя одной кнопкой), и разграничение доступа
        перестанет что-либо значить."""
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
    async def test_operator_cannot_deactivate_self(self) -> None:
        """Если гард снять, оператор одним промахом по своей же карточке
        отключит себе доступ и не сможет его вернуть."""
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


class TestOperatorChangesAreJournalled:
    @pytest.mark.asyncio
    async def test_deactivation_writes_audit_record(self) -> None:
        """Если запись в журнал потеряется, отзыв доступа к ПДн жителей
        станет недоказуемым: по 152-ФЗ администрация обязана показать,
        кто и когда снял оператора."""
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


class TestOperatorAlwaysSeesResult:
    @pytest.mark.asyncio
    async def test_card_render_failure_still_reports_result_to_operator(self) -> None:
        """Если падение рендера карточки не глушить, оператор нажмёт
        «Закрыть»/«Вернуть в работу», операция в БД пройдёт, а в чате не
        появится ничего — и он нажмёт ещё раз, уже вслепую."""
        from aemr_bot.handlers import admin_appeal_ops

        event = make_event(chat_id=555, user_id=7, with_callback=True)
        appeal = SimpleNamespace(id=5, user=SimpleNamespace(max_user_id=42))
        send_screen = AsyncMock()
        with patch(
            "aemr_bot.handlers.admin_appeal_ops.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service."
            "get_by_id_with_messages",
            AsyncMock(return_value=appeal),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.admin_card_service.render",
            AsyncMock(side_effect=RuntimeError("MAX down")),
        ), patch(
            "aemr_bot.handlers._common.send_or_edit_screen",
            send_screen,
        ):
            await admin_appeal_ops._show_appeal_card_or_result(
                event, 5, "fallback-render-упал"
            )

        send_screen.assert_awaited_once()
        assert send_screen.await_args.kwargs.get("text") == "fallback-render-упал"
