"""Гарды доставки ответа оператора жителю: право отвечать и свайп-резолв.

Спасено из удалённого `test_operator_reply_characterization.py` — это
единственное место в корпусе, где проверялись СЛЕДСТВИЯ, а не строки
покрытия: деактивированный оператор не может ответить, ссылка на
сторонний сайт не уходит жителю, свайп по переопубликованной карточке
находит нужное обращение.

Формат докстрингов: «если <поломка>, то <что случится с жителем/
оператором/данными>».
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


def _make_event(*, chat_id: int = 100, user_id: int = 7) -> SimpleNamespace:
    """Событие оператора в служебной группе.

    handle_operator_reply читает event.message.link (свайп-ответ) и
    редактирует сообщения, поэтому добавляем link=None и edit_message.
    """
    event = make_event(chat_id=chat_id, user_id=user_id, with_edit_message=True)
    event.message.link = None
    return event


def _fresh_appeal(*, appeal_id: int = 1) -> SimpleNamespace:
    """Здоровое обращение: активный житель с действующим согласием."""
    user = SimpleNamespace(
        is_blocked=False,
        first_name="Иван",
        phone="+79991234567",
        subscribed_broadcast=False,
        consent_pdn_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        consent_revoked_at=None,
        max_user_id=42,
    )
    appeal = SimpleNamespace(
        id=appeal_id,
        user=user,
        created_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        topic="Дороги",
        locality="Елизово",
        address="ул. Ленина, д. 1",
        status="new",
        summary="яма",
        attachments=[],
    )
    appeal.__dict__["messages"] = []
    return appeal


@pytest.fixture(autouse=True)
def _clean_reply_state():
    """Module-level дедуп и registry intent'ов протекают между тестами."""
    from aemr_bot.handlers import operator_reply as opr
    from aemr_bot.services import wizard_registry as _wr

    opr._recent_replies.clear()
    _wr._reply_intent.clear()
    yield
    opr._recent_replies.clear()
    _wr._reply_intent.clear()


class TestRightToAnswerCheckedAtDelivery:
    """SEC #6: право отвечать перечитывается в момент доставки, а не в
    момент нажатия кнопки."""

    @pytest.mark.asyncio
    async def test_deactivated_operator_reply_never_reaches_resident(self) -> None:
        """Если проверку «оператор ещё активен» убрать, уволенный
        сотрудник, у которого в чате остался старый intent, продолжит
        отвечать жителям от имени администрации."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        appeal = MagicMock(id=5)
        operator = MagicMock(id=7, max_user_id=42)
        dead_op = SimpleNamespace(id=7, max_user_id=42, is_active=False)

        with patch.object(opr.cfg, "answer_max_chars", 1000), patch(
            "aemr_bot.handlers.operator_reply.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.operator_reply.operators_service.get",
            AsyncMock(return_value=dead_op),
        ), patch(
            "aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
            AsyncMock(return_value=_fresh_appeal()),
        ) as get_appeal:
            handled = await opr._deliver_operator_reply(
                event, appeal=appeal, operator=operator,
                text="ответ", audit_action="reply",
            )

        assert handled is True
        # До перечитки обращения не дошли — отказ раньше любых записей.
        get_appeal.assert_not_called()
        text = event.bot.send_message.await_args.kwargs.get("text", "")
        assert "деактивирована" in text.lower()
        assert "#5" in text

    @pytest.mark.asyncio
    async def test_vanished_operator_reply_never_reaches_resident(self) -> None:
        """Если None из operators_service.get трактовать как «оператор
        есть», удалённая из таблицы учётка сохранит право писать жителю."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        appeal = MagicMock(id=5)
        operator = MagicMock(id=7, max_user_id=42)

        with patch.object(opr.cfg, "answer_max_chars", 1000), patch(
            "aemr_bot.handlers.operator_reply.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.operator_reply.operators_service.get",
            AsyncMock(return_value=None),
        ):
            handled = await opr._deliver_operator_reply(
                event, appeal=appeal, operator=operator,
                text="ответ", audit_action="reply",
            )

        assert handled is True
        text = event.bot.send_message.await_args.kwargs.get("text", "")
        assert "деактивирована" in text.lower()


class TestOutgoingLinkGate:
    @pytest.mark.asyncio
    async def test_reply_with_foreign_link_is_not_delivered(self) -> None:
        """Если снять проверку исходящих ссылок, взломанная или ошибшаяся
        учётка оператора разошлёт жителям фишинговый адрес от имени
        администрации — и он же ляжет в переписку обращения."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        appeal = MagicMock(id=7)
        operator = MagicMock(id=7, max_user_id=42)
        live_op = SimpleNamespace(id=7, max_user_id=42, is_active=True)

        with patch.object(opr.cfg, "answer_max_chars", 1000), patch(
            "aemr_bot.handlers.operator_reply.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.operator_reply.operators_service.get",
            AsyncMock(return_value=live_op),
        ), patch(
            "aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
            AsyncMock(return_value=_fresh_appeal()),
        ), patch(
            "aemr_bot.handlers.operator_reply._is_reply_success_recorded",
            AsyncMock(return_value=False),
        ), patch(
            "aemr_bot.services.settings_store.find_non_whitelisted_urls",
            return_value=["http://evil.example"],
        ), patch(
            "aemr_bot.handlers.operator_reply.appeals_service.add_operator_message",
            AsyncMock(),
        ) as add_message:
            handled = await opr._deliver_operator_reply(
                event, appeal=appeal, operator=operator,
                text="смотри http://evil.example", audit_action="reply",
            )

        assert handled is True
        add_message.assert_not_called()
        # Ни одного send с user_id (жителю) — только уведомление оператору.
        for call in event.bot.send_message.await_args_list:
            assert "user_id" not in call.kwargs
        text = event.bot.send_message.await_args.kwargs.get("text", "")
        assert "сторонн" in text.lower()
        assert "evil.example" in text


class TestSwipeResolvesAppeal:
    @pytest.mark.asyncio
    async def test_swipe_by_admin_message_id_delivers(self) -> None:
        """Если резолв обращения по mid карточки сломается, свайп-ответ —
        основной рабочий жест оператора — перестанет доходить до жителя
        (оператор увидит «обращение не найдено» на живой карточке)."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event(user_id=7)
        event.message.link = SimpleNamespace(
            type="reply", message=SimpleNamespace(mid="MID-1")
        )
        operator = SimpleNamespace(id=7, max_user_id=42)
        appeal = _fresh_appeal(appeal_id=8)

        deliver = AsyncMock(return_value=True)
        with patch(
            "aemr_bot.handlers.operator_reply.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.operator_reply.operators_service.get",
            AsyncMock(return_value=operator),
        ), patch(
            "aemr_bot.handlers.operator_reply.appeals_service.get_by_admin_message_id",
            AsyncMock(return_value=appeal),
        ) as by_admin, patch(
            "aemr_bot.handlers.operator_reply._deliver_operator_reply", deliver
        ):
            result = await opr.handle_operator_reply(
                event, body=None, text="ответ свайпом"
            )

        assert result is True
        by_admin.assert_awaited_once()
        deliver.assert_awaited_once()
        assert deliver.await_args.kwargs["appeal"] is appeal
        assert deliver.await_args.kwargs["audit_action"] == "reply"

    @pytest.mark.asyncio
    async def test_swipe_on_republished_card_resolves_by_last_mid(self) -> None:
        """Свайп по ПЕРЕОПУБЛИКОВАННОЙ карточке: admin_message_id (mid первой
        публикации) не находит обращение, но mid совпадает с
        last_admin_card_mid последней карточки → обращение резолвится и
        ответ доставляется (раньше падало в ADMIN_REPLY_NO_APPEAL)."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event(user_id=7)
        event.message.link = SimpleNamespace(
            type="reply", message=SimpleNamespace(mid="MID-REPUBLISHED")
        )
        operator = SimpleNamespace(id=7, max_user_id=42)
        appeal = _fresh_appeal(appeal_id=9)

        deliver = AsyncMock(return_value=True)
        with patch(
            "aemr_bot.handlers.operator_reply.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.operator_reply.operators_service.get",
            AsyncMock(return_value=operator),
        ), patch(
            "aemr_bot.handlers.operator_reply.appeals_service.get_by_admin_message_id",
            AsyncMock(return_value=None),
        ), patch(
            "aemr_bot.handlers.operator_reply.appeals_service."
            "get_by_last_admin_card_mid",
            AsyncMock(return_value=appeal),
        ) as by_last, patch(
            "aemr_bot.handlers.operator_reply._deliver_operator_reply", deliver
        ):
            result = await opr.handle_operator_reply(
                event, body=None, text="ответ свайпом по свежей карточке"
            )

        assert result is True
        by_last.assert_awaited_once()
        deliver.assert_awaited_once()
        assert deliver.await_args.kwargs["appeal"] is appeal
