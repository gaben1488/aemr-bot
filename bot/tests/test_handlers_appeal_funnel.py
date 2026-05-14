"""Расширенные тесты handlers/appeal_funnel — состояния воронки и
дополнения. Дополняет существующий test_handlers_funnel.py.

Локально skip без maxapi; в CI работает.

Покрываем:
- on_awaiting_topic: пустой список тем → reset
- on_awaiting_consent: с policy_url и без
- on_awaiting_summary: пусто → отказ
- on_awaiting_followup_text: appeal not found, consent revoked, closed
- ask_address_or_reuse: prev address found / not found
- ask_locality / ask_address / ask_topic / ask_summary — pure-flow
- on_idle: с активным обращением и без
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


def _make_event(*, chat_id: int = 100, user_id: int = 42, text: str = "") -> SimpleNamespace:
    bot = MagicMock()
    # send_message возвращает SendedMessage-like — нужно для
    # services/progress.send_or_edit_progress → extract_message_id.
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(
            message=SimpleNamespace(body=SimpleNamespace(mid="m-progress"))
        )
    )
    bot.edit_message = AsyncMock()
    return SimpleNamespace(
        bot=bot,
        message=SimpleNamespace(
            answer=AsyncMock(),
            sender=SimpleNamespace(user_id=user_id, first_name="Иван"),
            recipient=SimpleNamespace(chat_id=chat_id),
            body=SimpleNamespace(text=text, attachments=[], mid="m-1"),
        ),
        user=SimpleNamespace(user_id=user_id, first_name="Иван"),
    )


def _make_callback_event(
    *, chat_id: int = 100, user_id: int = 42, text: str = "", payload: str = "menu:settings"
) -> SimpleNamespace:
    event = _make_event(chat_id=chat_id, user_id=user_id, text=text)
    event.callback = SimpleNamespace(
        payload=payload,
        callback_id="cb-1",
        user=SimpleNamespace(user_id=user_id, first_name="Иван"),
    )
    event.ack = AsyncMock()
    return event


@asynccontextmanager
async def _fake_session_scope():
    yield MagicMock()


class TestAskAddressOrReuse:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_prev_address(self) -> None:
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=SimpleNamespace(id=1))), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.find_last_address_for_user",
                   AsyncMock(return_value=None)):
            result = await appeal_funnel.ask_address_or_reuse(event, max_user_id=42)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_with_prev_address(self) -> None:
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=SimpleNamespace(id=1))), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.find_last_address_for_user",
                   AsyncMock(return_value=("Елизовское ГП", "Ленина 1"))):
            result = await appeal_funnel.ask_address_or_reuse(event, max_user_id=42)
        assert result is True
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "Ленина 1" in text


class TestAskFunnelSteps:
    @pytest.mark.asyncio
    async def test_button_step_edits_current_progress_card(self) -> None:
        """Кнопочный переход должен менять текущую карточку, а не плодить новую."""
        from aemr_bot import keyboards
        from aemr_bot.db.models import DialogState
        from aemr_bot.handlers import appeal_funnel

        event = _make_callback_event(payload="locality:0")
        user = SimpleNamespace(
            first_name="Иван",
            dialog_data={"locality": "Елизовское ГП"},
        )
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.set_state",
                   AsyncMock()), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.update_dialog_data",
                   AsyncMock()) as update_data:
            await appeal_funnel._show_progress_step(
                event,
                max_user_id=42,
                stage="address",
                next_state=DialogState.AWAITING_ADDRESS,
                keyboard=keyboards.cancel_keyboard(),
            )

        event.bot.edit_message.assert_called_once()
        assert event.bot.edit_message.call_args.kwargs["message_id"] == "m-1"
        event.bot.send_message.assert_not_called()
        update_data.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_new_progress_card_after_visible_input(self) -> None:
        """После текста/гео/файла следующий шаг должен появиться ниже сообщения жителя."""
        from aemr_bot import keyboards
        from aemr_bot.db.models import DialogState
        from aemr_bot.handlers import appeal_funnel

        event = _make_callback_event(payload="topic:0")
        user = SimpleNamespace(
            first_name="Иван",
            dialog_data={"locality": "Елизовское ГП", "address": "Ленина, 5"},
        )
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.set_state",
                   AsyncMock()), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.update_dialog_data",
                   AsyncMock()) as update_data:
            await appeal_funnel._show_progress_step(
                event,
                max_user_id=42,
                stage="topic",
                next_state=DialogState.AWAITING_TOPIC,
                keyboard=keyboards.cancel_keyboard(),
                force_new_message=True,
            )

        event.bot.edit_message.assert_not_called()
        event.bot.send_message.assert_called_once()
        update_data.assert_called_once()
        assert update_data.call_args.args[2] == {"progress_message_id": "m-progress"}

    @pytest.mark.asyncio
    async def test_ask_locality_sends_localities_keyboard(self) -> None:
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        user = SimpleNamespace(first_name="Иван", dialog_data={})
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.set_state",
                   AsyncMock()), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.update_dialog_data",
                   AsyncMock()), \
             patch("aemr_bot.handlers.appeal_funnel.settings_store.get",
                   AsyncMock(return_value=["Елизовское ГП", "Паратунское СП"])):
            await appeal_funnel.ask_locality(event, max_user_id=42)
        event.bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_ask_address_uses_locality_from_dialog_data(self) -> None:
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        user = SimpleNamespace(first_name="Иван", dialog_data={"locality": "Корякское СП"})
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.set_state",
                   AsyncMock()), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.update_dialog_data",
                   AsyncMock()):
            await appeal_funnel.ask_address(event, max_user_id=42)
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "Корякское СП" in text

    @pytest.mark.asyncio
    async def test_ask_topic_uses_address_and_topics(self) -> None:
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        user = SimpleNamespace(first_name="Иван", dialog_data={"address": "Ленина 1"})
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.set_state",
                   AsyncMock()), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.update_dialog_data",
                   AsyncMock()), \
             patch("aemr_bot.handlers.appeal_funnel.settings_store.get",
                   AsyncMock(return_value=["Дороги", "ЖКХ"])):
            await appeal_funnel.ask_topic(event, max_user_id=42)
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "Ленина 1" in text

    @pytest.mark.asyncio
    async def test_ask_summary_uses_topic(self) -> None:
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        user = SimpleNamespace(first_name="Иван", dialog_data={"topic": "Дороги"})
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.set_state",
                   AsyncMock()), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.update_dialog_data",
                   AsyncMock()):
            await appeal_funnel.ask_summary(event, max_user_id=42)
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "Дороги" in text


class TestOnAwaitingTopic:
    @pytest.mark.asyncio
    async def test_empty_topics_resets_state(self) -> None:
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        reset = AsyncMock()
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.settings_store.get",
                   AsyncMock(return_value=[])), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.reset_state",
                   reset):
            await appeal_funnel.on_awaiting_topic(event, body=None, text_body="hi", max_user_id=42)
        reset.assert_called_once()
        event.message.answer.assert_called_once()

    @pytest.mark.asyncio
    async def test_with_topics_prompts_to_use_buttons(self) -> None:
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.settings_store.get",
                   AsyncMock(return_value=["Дороги", "ЖКХ"])):
            await appeal_funnel.on_awaiting_topic(event, body=None, text_body="hi", max_user_id=42)
        event.message.answer.assert_called_once()
        text = event.message.answer.call_args.args[0]
        assert "тематик" in text.lower() or "Выберите" in text


class TestOnAwaitingConsent:
    @pytest.mark.asyncio
    async def test_with_policy_url(self) -> None:
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.settings_store.get",
                   AsyncMock(return_value="https://policy.example/p.pdf")):
            await appeal_funnel.on_awaiting_consent(event, body=None, text_body="x", max_user_id=42)
        event.message.answer.assert_called_once()

    @pytest.mark.asyncio
    async def test_without_policy_url_falls_back(self) -> None:
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.settings_store.get",
                   AsyncMock(return_value=None)):
            await appeal_funnel.on_awaiting_consent(event, body=None, text_body="x", max_user_id=42)
        event.message.answer.assert_called_once()
        text = event.message.answer.call_args.args[0]
        assert "согласи" in text.lower()


class TestOnAwaitingSummary:
    @pytest.mark.asyncio
    async def test_empty_text_and_no_attachments_rejected(self) -> None:
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        with patch("aemr_bot.handlers.appeal_funnel.collect_attachments",
                   return_value=[]):
            await appeal_funnel.on_awaiting_summary(event, body=None, text_body="", max_user_id=42)
        # Сообщение «обращение пустое» должно уйти.
        event.message.answer.assert_called_once()
        text = event.message.answer.call_args.args[0]
        from aemr_bot import texts
        assert text == texts.APPEAL_EMPTY_REJECTED


class TestOnAwaitingFollowupText:
    @pytest.mark.asyncio
    async def test_appeal_not_found_resets(self) -> None:
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        user = SimpleNamespace(id=1, dialog_data={"appeal_id": 999}, consent_pdn_at=datetime.now(timezone.utc))
        reset = AsyncMock()
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.reset_state",
                   reset), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.get_by_id",
                   AsyncMock(return_value=None)):
            await appeal_funnel.on_awaiting_followup_text(
                event, body=None, text_body="дополнение", max_user_id=42
            )
        reset.assert_called_once()
        event.message.answer.assert_called_once()

    @pytest.mark.asyncio
    async def test_revoked_consent_blocks(self) -> None:
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        user = SimpleNamespace(
            id=1,
            dialog_data={"appeal_id": 5},
            consent_pdn_at=None,  # отозвано
        )
        appeal = SimpleNamespace(id=5, user_id=1, status="new")
        reset = AsyncMock()
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.reset_state",
                   reset), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.get_by_id",
                   AsyncMock(return_value=appeal)):
            await appeal_funnel.on_awaiting_followup_text(
                event, body=None, text_body="x", max_user_id=42
            )
        reset.assert_called_once()
        text = event.message.answer.call_args.args[0]
        assert "отозвано" in text.lower() or "согласие" in text.lower()

    @pytest.mark.asyncio
    async def test_closed_appeal_blocks(self) -> None:
        from aemr_bot.db.models import AppealStatus
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        user = SimpleNamespace(
            id=1,
            dialog_data={"appeal_id": 5},
            consent_pdn_at=datetime.now(timezone.utc),
        )
        appeal = SimpleNamespace(
            id=5, user_id=1, status=AppealStatus.CLOSED.value
        )
        reset = AsyncMock()
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.reset_state",
                   reset), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.get_by_id",
                   AsyncMock(return_value=appeal)):
            await appeal_funnel.on_awaiting_followup_text(
                event, body=None, text_body="x", max_user_id=42
            )
        reset.assert_called_once()
        text = event.message.answer.call_args.args[0]
        assert "закрыто" in text.lower() or "Подать похожее" in text

    @pytest.mark.asyncio
    async def test_success_updates_original_admin_card(self) -> None:
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        user = SimpleNamespace(
            id=1,
            max_user_id=42,
            first_name="Сергей",
            phone="+79991234567",
            is_blocked=False,
            dialog_data={"appeal_id": 5},
            consent_pdn_at=datetime.now(timezone.utc),
        )
        appeal = SimpleNamespace(
            id=5,
            user_id=1,
            user=user,
            status="new",
            locality="Елизовское ГП",
            address="ул. Ленина, 5",
            topic="Дороги",
            summary="Яма во дворе.",
            attachments=[],
            messages=[],
            admin_message_id="admin-mid-5",
        )
        updated_appeal = SimpleNamespace(
            **{
                **appeal.__dict__,
                "messages": [
                    SimpleNamespace(
                        direction="from_user",
                        text="Уточнение: яма у второго подъезда.",
                        attachments=[],
                    )
                ],
            }
        )
        reset = AsyncMock()
        with patch(
            "aemr_bot.handlers.appeal_funnel.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
            AsyncMock(return_value=user),
        ), patch(
            "aemr_bot.handlers.appeal_funnel.users_service.reset_state",
            reset,
        ), patch(
            "aemr_bot.handlers.appeal_funnel.appeals_service.get_by_id",
            AsyncMock(return_value=appeal),
        ), patch(
            "aemr_bot.handlers.appeal_funnel.appeals_service.get_by_id_with_messages",
            AsyncMock(return_value=updated_appeal),
        ), patch(
            "aemr_bot.handlers.appeal_funnel.appeals_service.add_user_message",
            AsyncMock(),
        ), patch(
            "aemr_bot.config.settings.admin_group_id",
            555,
        ), patch(
            "aemr_bot.handlers.menu.open_main_menu",
            AsyncMock(),
        ):
            await appeal_funnel.on_awaiting_followup_text(
                event,
                body=SimpleNamespace(attachments=[]),
                text_body="Уточнение: яма у второго подъезда.",
                max_user_id=42,
            )

        reset.assert_called_once()
        event.bot.edit_message.assert_called_once()
        assert event.bot.edit_message.call_args.kwargs["message_id"] == "admin-mid-5"
        edited_text = event.bot.edit_message.call_args.kwargs["text"]
        assert "Дополнение к обращению:" in edited_text
        assert "второго подъезда" in edited_text


class TestOnIdle:
    @pytest.mark.asyncio
    async def test_with_active_appeal_offers_followup_path(self) -> None:
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        user = SimpleNamespace(id=1)
        appeal = SimpleNamespace(id=5)
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.find_active_for_user",
                   AsyncMock(return_value=appeal)):
            await appeal_funnel.on_idle(event, body=None, text_body="x", max_user_id=42)
        # Подсказка про «Мои обращения / Дополнить».
        event.message.answer.assert_called_once()
        text = event.message.answer.call_args.args[0]
        assert "Дополнить" in text or "Мои обращения" in text

    @pytest.mark.asyncio
    async def test_without_active_falls_back_to_main_menu(self) -> None:
        from aemr_bot.handlers import appeal_funnel

        event = _make_event()
        user = SimpleNamespace(id=1)
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.find_active_for_user",
                   AsyncMock(return_value=None)):
            await appeal_funnel.on_idle(event, body=None, text_body="x", max_user_id=42)
        event.message.answer.assert_called_once()
        assert event.message.answer.call_args.kwargs["attachments"]
