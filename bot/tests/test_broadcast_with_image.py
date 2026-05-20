"""TDD-тесты image-attachments в рассылках.

Контракт:
- `_send_one(bot, max_user_id, body_text, *, outbound_images=())` —
  per-user отправка с картинками рассылки рядом с unsubscribe-keyboard.
- `_run_broadcast_impl` должна разадеть `broadcast.attachments` ОДИН
  РАЗ через `image_attachments.build_outbound_image_attachments`
  (а не на каждого подписчика — деривалидация pydantic стоит ресурсов),
  передать результат в send-loop.

RED → GREEN: текущий `_send_one` принимает только три позиционных
параметра без attachments; тесты должны падать до правки.

Regression-guard: text-only рассылка (broadcast.attachments=[])
продолжает работать ровно как раньше — никаких лишних объектов в
attachments сверх клавиатуры.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("maxapi", reason="broadcast тянет maxapi")


# ---- _send_one с outbound_images -------------------------------------------


class TestSendOneWithImages:
    @pytest.mark.asyncio
    async def test_image_attached_to_send_message(self) -> None:
        """Контракт: картинка из outbound_images попадает в
        attachments отправки рядом с unsubscribe-клавиатурой."""
        from aemr_bot.handlers import broadcast as bc

        bot = MagicMock()
        bot.send_message = AsyncMock()
        fake_image = SimpleNamespace(type="image", payload={"url": "https://..."})

        err = await bc._send_one(
            bot, max_user_id=42, body_text="Объявление",
            outbound_images=[fake_image],
        )

        assert err is None
        call = bot.send_message.call_args
        attachments = call.kwargs.get("attachments", [])
        assert fake_image in attachments, (
            f"картинка не прицепилась к рассылке; attachments={attachments}"
        )
        # клавиатура отписки тоже должна быть
        assert len(attachments) == 2

    @pytest.mark.asyncio
    async def test_no_images_regression_text_only(self) -> None:
        """Regression: text-only рассылка — ровно одна attachment
        (клавиатура отписки)."""
        from aemr_bot.handlers import broadcast as bc

        bot = MagicMock()
        bot.send_message = AsyncMock()

        err = await bc._send_one(
            bot, max_user_id=42, body_text="Объявление",
        )

        assert err is None
        call = bot.send_message.call_args
        attachments = call.kwargs.get("attachments", [])
        assert len(attachments) == 1, (
            f"text-only рассылка не должна иметь лишних вложений; "
            f"attachments={attachments}"
        )

    @pytest.mark.asyncio
    async def test_send_failure_reported(self) -> None:
        """Regression: при ошибке send_message _send_one возвращает
        строку с ошибкой (контракт не сломался)."""
        from aemr_bot.handlers import broadcast as bc

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("max down"))

        err = await bc._send_one(
            bot, max_user_id=42, body_text="Объявление",
            outbound_images=[],
        )

        assert err is not None
        assert "max down" in err


# ---- _run_send_loop с outbound_images --------------------------------------


from tests._helpers import fake_session_scope as _fake_session_scope  # noqa: E402
from tests._helpers import make_event  # noqa: E402


class TestRunSendLoopWithImages:
    @pytest.mark.asyncio
    async def test_images_propagated_to_send_one(self) -> None:
        """Контракт: _run_send_loop передаёт outbound_images каждому
        вызову _send_one. Тест ловит цепочку, не текстовые детали."""
        from aemr_bot.handlers import broadcast as bc

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.edit_message = AsyncMock()

        fake_image = SimpleNamespace(type="image", payload={})
        targets = [(1, 42), (2, 43)]  # (user_db_id, max_user_id)

        # Ловим аргументы каждого _send_one
        captured_kwargs: list[dict] = []

        async def _stub_send_one(bot_arg, max_user_id, body_text, **kw):
            captured_kwargs.append(kw)
            return None

        with patch.object(bc, "_send_one", side_effect=_stub_send_one), \
             patch("aemr_bot.handlers.broadcast.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.record_deliveries",
                   AsyncMock()), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.get_status",
                   AsyncMock(return_value="running")), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.update_progress",
                   AsyncMock()):
            delivered, failed, cancelled = await bc._run_send_loop(
                bot,
                broadcast_id=7,
                body="text",
                total=2,
                targets=targets,
                admin_mid=None,
                rate_delay=0.0,
                progress_step_sec=999.0,  # не флашим по таймеру
                outbound_images=[fake_image],
            )

        assert delivered == 2
        assert failed == 0
        assert cancelled is False
        # каждый из 2 вызовов получил картинку
        assert len(captured_kwargs) == 2
        for kw in captured_kwargs:
            assert kw.get("outbound_images") == [fake_image]


# ---- wizard: _handle_wizard_text захватывает картинку ----------------------


class TestWizardCapturesImage:
    @pytest.mark.asyncio
    async def test_image_in_event_stored_on_wizard_state(self) -> None:
        """Контракт: когда оператор шлёт текст рассылки с приложенной
        картинкой в одном сообщении, мастер сохраняет картинку в
        state.attachments для следующего шага (confirm → create)."""
        from aemr_bot.handlers import broadcast as bc

        event = make_event(chat_id=100, user_id=7)
        event.message.body.attachments = [
            {"type": "image", "payload": {"url": "https://cdn/img.jpg"}}
        ]
        bc._wizards.clear()
        bc._wizards[7] = bc._WizardState(step="awaiting_text")

        with patch("aemr_bot.handlers.broadcast.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.count_subscribers",
                   AsyncMock(return_value=5)):
            handled = await bc._handle_wizard_text(event, "текст рассылки")

        assert handled is True
        state = bc._wizards.get(7)
        assert state is not None
        assert state.text == "текст рассылки"
        # картинка сохранена для confirm-шага
        assert state.attachments, "state.attachments пуст — картинка не захвачена"
        assert state.attachments[0]["type"] == "image"

    @pytest.mark.asyncio
    async def test_text_only_keeps_attachments_empty(self) -> None:
        """Regression: текст без картинки — state.attachments=[]."""
        from aemr_bot.handlers import broadcast as bc

        event = make_event(chat_id=100, user_id=7)
        # никаких attachments в body
        bc._wizards.clear()
        bc._wizards[7] = bc._WizardState(step="awaiting_text")

        with patch("aemr_bot.handlers.broadcast.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.count_subscribers",
                   AsyncMock(return_value=5)):
            handled = await bc._handle_wizard_text(event, "только текст")

        assert handled is True
        state = bc._wizards.get(7)
        assert state is not None
        assert list(state.attachments) == []


# ---- _handle_confirm передаёт attachments в create_broadcast ---------------


class TestConfirmPassesAttachmentsToCreate:
    @pytest.mark.asyncio
    async def test_attachments_passed_to_create_broadcast(self) -> None:
        """Контракт: при confirm мастера, картинки state.attachments
        передаются в broadcasts_service.create_broadcast как kwarg
        attachments. Без этого фоновая рассылка не найдёт картинку
        в Broadcast row."""
        from aemr_bot.handlers import broadcast as bc

        event = make_event(
            chat_id=100, user_id=7, with_callback=True, with_edit_message=True,
        )
        op = SimpleNamespace(id=10)
        broadcast_obj = SimpleNamespace(id=99)

        # State уже на confirm-шаге, с картинкой
        bc._wizards.clear()
        bc._wizards[7] = bc._WizardState(step="awaiting_confirm")
        bc._wizards[7].text = "ВАЖНО"
        bc._wizards[7].attachments = [
            {"type": "image", "payload": {"url": "https://cdn/img.jpg"}}
        ]

        def _consume(coro, **kwargs):
            coro.close()

        create_mock = AsyncMock(return_value=broadcast_obj)
        with patch("aemr_bot.handlers.broadcast.ack_callback", AsyncMock()), \
             patch("aemr_bot.handlers.broadcast._get_operator",
                   AsyncMock(return_value=op)), \
             patch("aemr_bot.handlers.broadcast.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.count_subscribers",
                   AsyncMock(return_value=5)), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.create_broadcast",
                   create_mock), \
             patch("aemr_bot.handlers.broadcast.operators_service.write_audit",
                   AsyncMock()), \
             patch("aemr_bot.handlers.broadcast.send_or_edit_screen",
                   AsyncMock()), \
             patch("aemr_bot.handlers.broadcast.spawn_background_task",
                   MagicMock(side_effect=_consume)):
            await bc._handle_confirm(event)

        create_mock.assert_awaited_once()
        kwargs = create_mock.await_args.kwargs
        assert kwargs.get("attachments") == [
            {"type": "image", "payload": {"url": "https://cdn/img.jpg"}}
        ], f"attachments не пробросились в create_broadcast: {kwargs}"


# ---- _run_broadcast_impl: deserialize attachments and pass to send_loop ----


class TestRunBroadcastImplPassesImages:
    @pytest.mark.asyncio
    async def test_broadcast_attachments_deserialized_and_passed_to_send_loop(self) -> None:
        """Контракт: _run_broadcast_impl читает broadcast.attachments
        из БД, десериализует через build_outbound_image_attachments
        (один раз, не на каждого подписчика), и передаёт результат
        в _run_send_loop как outbound_images."""
        from aemr_bot.handlers import broadcast as bc

        bot = MagicMock()
        bot.edit_message = AsyncMock()
        bot.send_message = AsyncMock()

        stored_attachments = [{"type": "image", "payload": {"url": "x"}}]
        broadcast_row = SimpleNamespace(
            id=99, attachments=stored_attachments, text="t",
        )
        fake_image_obj = SimpleNamespace(type="image", payload={})

        run_send_loop_mock = AsyncMock(return_value=(0, 0, False))
        deserialize_mock = MagicMock(return_value=[fake_image_obj])

        with patch("aemr_bot.handlers.broadcast.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.mark_started",
                   AsyncMock()), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.list_subscriber_targets",
                   AsyncMock(return_value=[(1, 42)])), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.get_by_id",
                   AsyncMock(return_value=broadcast_row)), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.mark_finished",
                   AsyncMock()), \
             patch.object(bc, "_run_send_loop", run_send_loop_mock), \
             patch.object(bc, "_send_final_summary", AsyncMock()), \
             patch.object(bc._image_attachments, "build_outbound_image_attachments",
                          deserialize_mock):
            await bc._run_broadcast_impl(
                bot, broadcast_id=99, text="t", total=1,
                admin_mid="m-progress",
            )

        # десериализация вызвана ровно один раз с сохранёнными dict'ами
        deserialize_mock.assert_called_once_with(stored_attachments)
        # результат пробросился в _run_send_loop
        run_send_loop_mock.assert_awaited_once()
        kwargs = run_send_loop_mock.await_args.kwargs
        assert kwargs.get("outbound_images") == [fake_image_obj], (
            f"outbound_images не пробросились в _run_send_loop: {kwargs}"
        )
