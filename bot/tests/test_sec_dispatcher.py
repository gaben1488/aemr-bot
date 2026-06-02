"""Структурные authz-гейты диспетчера callback'ов (security-кластер).

Покрывает две границы, которые раньше зияли:

P3-1 (корень P2-4 / P2-5) — обратная граница admin-чата в
`handlers.appeal._route_callback`. on_callback закрывал только прямую
сторону (жительский payload в админ-группе). Admin-payload
(broadcast:* / op:*) из НЕ-админ-чата (личка жителя) раньше доходил до
`dispatch_admin_callback` → инъекция в опер-группу из лички
(broadcast:abort гасит мастер рассылки, op:reply_cancel — черновик
ответа оператора). Тест доказывает: admin-payload из chat_id !=
admin_group_id НЕ диспетчеризуется, НЕ проваливается в меню и ничего
не шлёт в admin_group_id; из admin_group_id — работает как раньше.

P3-2 — `handlers.admin_panel.show_op_menu` без авторской проверки.
Раньше функция полагалась на гейты вызывающих, но callback op:menu
ролевого фильтра не имел вовсе, а /op_help проверял лишь сам факт
админ-чата (участник группы без записи Operator получал опер-меню).
Тест доказывает: не-оператор опер-меню не получает (нейтральное
сообщение, send_or_edit_screen не вызван); активный оператор — получает.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_callback_event, make_event

ADMIN_GROUP = 123  # совпадает с ADMIN_GROUP_ID в tests/conftest.py
CITIZEN_CHAT = 999  # личка жителя — НЕ админ-группа


# ---------------------------------------------------------------------------
# P3-1: обратная граница admin-чата в _route_callback
# ---------------------------------------------------------------------------


class TestAdminCallbackChatGate:
    """admin-payload (BROADCAST_ADMIN / OPERATOR_ADMIN) выполняется только
    из настроенной админ-группы."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", ["broadcast:abort", "op:reply_cancel"])
    async def test_admin_payload_from_non_admin_chat_blocked(
        self, payload: str
    ) -> None:
        """Из лички жителя admin-payload НЕ диспетчеризуется, НЕ
        проваливается в меню и ничего не шлёт в admin_group_id; только ack."""
        pytest.importorskip("maxapi")
        from aemr_bot.handlers import appeal

        event = make_callback_event(
            chat_id=CITIZEN_CHAT, user_id=42, payload=payload
        )

        dispatch = AsyncMock(return_value=True)
        menu_cb = AsyncMock(return_value=True)
        ack = AsyncMock()
        with patch.object(
            appeal.admin_callback_dispatch,
            "dispatch_admin_callback",
            dispatch,
        ), patch("aemr_bot.handlers.menu.handle_callback", menu_cb), patch.object(
            appeal, "ack_callback", ack
        ), patch.object(appeal.cfg, "admin_group_id", ADMIN_GROUP):
            await appeal._route_callback(event, 42, payload)

        # Действие НЕ выполнено: admin-dispatch не вызван.
        dispatch.assert_not_called()
        # Нет fallthrough в меню (иначе payload всё равно мог бы что-то
        # сделать обходным путём).
        menu_cb.assert_not_called()
        # Спиннер на кнопке гасим, чтобы у атакующего не висел клиент.
        ack.assert_awaited_once()
        # Ничего не ушло в опер-группу (ядро инъекции P2-4/P2-5).
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", ["broadcast:abort", "op:reply_cancel"])
    async def test_admin_payload_from_admin_chat_dispatched(
        self, payload: str
    ) -> None:
        """Позитивный контроль: тот же admin-payload из admin_group_id
        по-прежнему доходит до dispatch_admin_callback (фикс не ломает
        легитимный путь оператора)."""
        pytest.importorskip("maxapi")
        from aemr_bot.handlers import appeal

        event = make_callback_event(
            chat_id=ADMIN_GROUP, user_id=42, payload=payload
        )

        dispatch = AsyncMock(return_value=True)
        menu_cb = AsyncMock(return_value=True)
        ack = AsyncMock()
        with patch.object(
            appeal.admin_callback_dispatch,
            "dispatch_admin_callback",
            dispatch,
        ), patch("aemr_bot.handlers.menu.handle_callback", menu_cb), patch.object(
            appeal, "ack_callback", ack
        ), patch.object(appeal.cfg, "admin_group_id", ADMIN_GROUP):
            await appeal._route_callback(event, 42, payload)

        dispatch.assert_awaited_once_with(event, payload)
        # dispatch вернул True → fallthrough в меню не нужен.
        menu_cb.assert_not_called()

    @pytest.mark.asyncio
    async def test_admin_payload_blocked_when_admin_group_unset(self) -> None:
        """admin_group_id не сконфигурирован (None) → fail-closed: admin-payload
        не выполняется ниоткуда (нельзя «угадать» отсутствующую группу)."""
        pytest.importorskip("maxapi")
        from aemr_bot.handlers import appeal

        event = make_callback_event(
            chat_id=CITIZEN_CHAT, user_id=42, payload="broadcast:abort"
        )

        dispatch = AsyncMock(return_value=True)
        menu_cb = AsyncMock(return_value=True)
        ack = AsyncMock()
        with patch.object(
            appeal.admin_callback_dispatch,
            "dispatch_admin_callback",
            dispatch,
        ), patch("aemr_bot.handlers.menu.handle_callback", menu_cb), patch.object(
            appeal, "ack_callback", ack
        ), patch.object(appeal.cfg, "admin_group_id", None):
            await appeal._route_callback(event, 42, "broadcast:abort")

        dispatch.assert_not_called()
        menu_cb.assert_not_called()
        ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_citizen_payload_unaffected_in_private_chat(self) -> None:
        """Регрессия: жительский payload (CITIZEN_FLOW) из лички работает
        как раньше — гейт admin-границы его не трогает."""
        pytest.importorskip("maxapi")
        from aemr_bot.handlers import appeal

        event = make_callback_event(
            chat_id=CITIZEN_CHAT, user_id=42, payload="menu:new_appeal"
        )

        # citizen-dispatch вернёт True → меню не зовём.
        with patch.object(
            appeal, "_dispatch_citizen_callback", AsyncMock(return_value=True)
        ) as citizen_dispatch, patch(
            "aemr_bot.handlers.menu.handle_callback", AsyncMock()
        ) as menu_cb, patch.object(
            appeal.admin_callback_dispatch,
            "dispatch_admin_callback",
            AsyncMock(return_value=True),
        ) as admin_dispatch, patch.object(
            appeal.cfg, "admin_group_id", ADMIN_GROUP
        ):
            await appeal._route_callback(event, 42, "menu:new_appeal")

        citizen_dispatch.assert_awaited_once()
        # Жительский payload НИКОГДА не идёт в admin-dispatch.
        admin_dispatch.assert_not_called()
        menu_cb.assert_not_called()


# ---------------------------------------------------------------------------
# P3-2: ensure_operator-гейт в show_op_menu
# ---------------------------------------------------------------------------


class TestShowOpMenuOperatorGate:
    @pytest.mark.asyncio
    async def test_non_operator_gets_no_op_menu(self) -> None:
        """Не-оператор (get_operator → None) опер-меню не получает:
        send_or_edit_screen НЕ вызван, вместо него — нейтральное сообщение."""
        pytest.importorskip("maxapi")
        from aemr_bot.handlers import admin_panel

        event = make_event(chat_id=CITIZEN_CHAT, user_id=7)
        event.message.answer = AsyncMock()

        send_screen = AsyncMock()
        with patch("aemr_bot.handlers.admin_panel.get_operator",
                   AsyncMock(return_value=None)), \
             patch("aemr_bot.handlers.admin_panel.send_or_edit_screen",
                   send_screen), \
             patch("aemr_bot.handlers.admin_panel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.services.appeals.count_open",
                   AsyncMock(return_value=5)) as count_open:
            await admin_panel.show_op_menu(event, pin=False)

        # Опер-меню не отрисовано.
        send_screen.assert_not_called()
        # До запроса счётчика открытых обращений тоже не дошли (ранний выход).
        count_open.assert_not_called()
        # Нейтральный ответ выдан.
        event.message.answer.assert_awaited_once()
        msg = event.message.answer.await_args.args[0]
        assert "оператор" in msg.lower()

    @pytest.mark.asyncio
    async def test_non_operator_pin_path_also_blocked(self) -> None:
        """Даже с pin=True (путь /op_help) не-оператор не получает меню и
        ничего не закрепляется."""
        pytest.importorskip("maxapi")
        from aemr_bot.handlers import admin_panel

        event = make_event(chat_id=CITIZEN_CHAT, user_id=7)
        event.message.answer = AsyncMock()
        event.bot.pin_message = AsyncMock()

        send_screen = AsyncMock()
        with patch("aemr_bot.handlers.admin_panel.get_operator",
                   AsyncMock(return_value=None)), \
             patch("aemr_bot.handlers.admin_panel.send_or_edit_screen",
                   send_screen), \
             patch("aemr_bot.handlers.admin_panel.session_scope",
                   _fake_session_scope):
            await admin_panel.show_op_menu(event, pin=True)

        send_screen.assert_not_called()
        event.bot.pin_message.assert_not_called()
        event.message.answer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_operator_still_gets_menu(self) -> None:
        """Позитивный контроль: активный оператор по-прежнему получает
        опер-меню (фикс поведение-сохраняющий для легитимного оператора)."""
        pytest.importorskip("maxapi")
        from aemr_bot.db.models import OperatorRole
        from aemr_bot.handlers import admin_panel

        event = make_event(chat_id=ADMIN_GROUP, user_id=7)
        event.message.answer = AsyncMock()

        op = SimpleNamespace(role=OperatorRole.IT.value)
        send_screen = AsyncMock(return_value=None)
        with patch("aemr_bot.handlers.admin_panel.get_operator",
                   AsyncMock(return_value=op)), \
             patch("aemr_bot.handlers.admin_panel.send_or_edit_screen",
                   send_screen), \
             patch("aemr_bot.handlers.admin_panel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.services.appeals.count_open",
                   AsyncMock(return_value=2)):
            await admin_panel.show_op_menu(event, pin=False)

        # Меню отрисовано, отказного сообщения нет.
        send_screen.assert_awaited_once()
        event.message.answer.assert_not_called()
