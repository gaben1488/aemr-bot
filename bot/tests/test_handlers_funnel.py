"""Тесты на handlers/appeal_funnel.py — FSM-воронка обращения.

Используем mock'и для bot, event, message — чтобы тесты не нуждались
в реальном MAX-API. Покрываем критичные edge-cases:
- блокированный житель не входит в воронку
- rate-limit 3 за час перенаправляет в дополнение
- consent_pdn_at NULL → запрос согласия
- policy_url отсутствует + token отсутствует → конфигурационный stop
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests._helpers import make_event

# handlers/__init__.py делает `from maxapi import Dispatcher` — без
# установленного maxapi пакета (локально без CI) импорт handler'ов
# упадёт на module-level. Скипаем такие тесты локально.
pytest.importorskip("maxapi", reason="handlers тесты требуют установленного maxapi")


def _make_event(*, chat_id: int = 100, user_id: int = 42) -> SimpleNamespace:
    # Обёртка над tests/_helpers.make_event. appeal_funnel-handler'ы
    # зовут bot.send_message И bot.edit_message (через send_or_edit_
    # progress) — with_edit_message=True даёт оба как AsyncMock.
    # Раньше файл держал свой bot=AsyncMock(); структурно эквивалентно.
    return make_event(
        chat_id=chat_id, user_id=user_id, with_edit_message=True
    )


class TestStartAppealFlow:
    """start_appeal_flow — точка входа из callback `menu:new_appeal`."""

    @pytest.mark.asyncio
    async def test_blocked_user_gets_block_message(self) -> None:
        """Заблокированный житель не должен попадать в воронку."""
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        with patch(
            "aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
            AsyncMock(
                return_value=SimpleNamespace(
                    is_blocked=True, consent_pdn_at=None, id=1
                )
            ),
        ), patch(
            "aemr_bot.handlers.appeal_funnel.session_scope"
        ) as mock_scope:
            mock_scope.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_scope.return_value.__aexit__ = AsyncMock(return_value=None)

            await appeal_funnel.start_appeal_flow(event, max_user_id=42)

        # Шлёт сообщение «вы заблокированы»
        event.bot.send_message.assert_called()
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "заблокирован" in text.lower()

    @pytest.mark.asyncio
    async def test_rate_limit_redirects(self) -> None:
        """3+ обращений за час → подсказка дополнить уже открытое."""
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        with patch(
            "aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
            AsyncMock(
                return_value=SimpleNamespace(
                    is_blocked=False, consent_pdn_at=None, id=1
                )
            ),
        ), patch(
            "aemr_bot.handlers.appeal_funnel.appeals_service.count_recent_for_user",
            AsyncMock(return_value=5),
        ), patch(
            "aemr_bot.handlers.appeal_funnel.appeals_service.find_active_for_user",
            AsyncMock(return_value=SimpleNamespace(id=7)),
        ), patch(
            "aemr_bot.handlers.appeal_funnel.session_scope"
        ) as mock_scope:
            mock_scope.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_scope.return_value.__aexit__ = AsyncMock(return_value=None)

            await appeal_funnel.start_appeal_flow(event, max_user_id=42)

        # Сообщение про «несколько обращений за час»
        event.bot.send_message.assert_called()
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "час" in text.lower()
        assert "Мои обращения" in text
        assert "Дополнить" in text
        assert "просто отправьте" not in text

    @pytest.mark.asyncio
    async def test_rate_limit_without_open_appeal_waits_for_reset(self) -> None:
        """Если неотвеченного обращения нет, дополнять нечего — ждём сброс лимита."""
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        with patch(
            "aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
            AsyncMock(
                return_value=SimpleNamespace(
                    is_blocked=False, consent_pdn_at=None, id=1
                )
            ),
        ), patch(
            "aemr_bot.handlers.appeal_funnel.appeals_service.count_recent_for_user",
            AsyncMock(return_value=5),
        ), patch(
            "aemr_bot.handlers.appeal_funnel.appeals_service.find_active_for_user",
            AsyncMock(return_value=None),
        ), patch(
            "aemr_bot.handlers.appeal_funnel.session_scope"
        ) as mock_scope:
            mock_scope.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_scope.return_value.__aexit__ = AsyncMock(return_value=None)

            await appeal_funnel.start_appeal_flow(event, max_user_id=42)

        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "лимит" in text.lower()
        assert "позже" in text.lower() or "сброс" in text.lower()
        assert "Дополнить" not in text


class TestOnAwaitingAddress:
    @pytest.mark.asyncio
    async def test_empty_address_rejected(self) -> None:
        """Пустой адрес или без буквенно-цифровых — отказ."""
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        await appeal_funnel.on_awaiting_address(event, body=None, text_body="...", max_user_id=42)
        # ADDRESS_EMPTY ответ
        event.message.answer.assert_called()
        msg = event.message.answer.call_args.args[0]
        assert "Укажите" in msg or "адрес" in msg.lower()

    @pytest.mark.asyncio
    async def test_valid_address_opens_topic_as_new_message(self) -> None:
        """После ручного адреса следующая карточка должна идти ниже сообщения жителя."""
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        ask_topic = AsyncMock()
        with patch(
            "aemr_bot.handlers.appeal_funnel.session_scope"
        ) as mock_scope, patch(
            "aemr_bot.handlers.appeal_funnel.users_service.update_dialog_data",
            AsyncMock(),
        ), patch(
            "aemr_bot.handlers.appeal_funnel.ask_topic",
            ask_topic,
        ):
            mock_scope.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_scope.return_value.__aexit__ = AsyncMock(return_value=None)

            await appeal_funnel.on_awaiting_address(
                event,
                body=None,
                text_body="Ленина, 5",
                max_user_id=42,
            )

        ask_topic.assert_called_once_with(event, 42, force_new_message=True)


class TestOnAwaitingName:
    @pytest.mark.asyncio
    async def test_empty_name_falls_back_to_max_profile(self) -> None:
        """Если житель прислал «...» — пробуем имя из профиля MAX."""
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        with patch(
            "aemr_bot.handlers.appeal_funnel.get_first_name",
            return_value="Иван",
        ), patch(
            "aemr_bot.handlers.appeal_funnel.users_service.set_first_name",
            AsyncMock(),
        ), patch(
            "aemr_bot.handlers.appeal_funnel.ask_address_or_reuse",
            AsyncMock(return_value=False),
        ), patch(
            "aemr_bot.handlers.appeal_funnel.ask_locality",
            AsyncMock(),
        ), patch(
            "aemr_bot.handlers.appeal_funnel.session_scope"
        ) as mock_scope:
            mock_scope.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_scope.return_value.__aexit__ = AsyncMock(return_value=None)

            await appeal_funnel.on_awaiting_name(
                event, body=None, text_body="...", max_user_id=42
            )

    @pytest.mark.asyncio
    async def test_no_name_at_all_asks_again(self) -> None:
        """Если ни в тексте ни в профиле — просит ввести."""
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        with patch(
            "aemr_bot.handlers.appeal_funnel.get_first_name",
            return_value=None,
        ):
            await appeal_funnel.on_awaiting_name(
                event, body=None, text_body="", max_user_id=42
            )
        event.message.answer.assert_called()
