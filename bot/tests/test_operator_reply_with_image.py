"""TDD-тесты image-relay в `_deliver_operator_reply`.

Контракт: когда оператор отвечает на карточку обращения сообщением,
содержащим картинку (`event.message.body.attachments` содержит
type=image), `bot.send_message` к жителю должен включать эту картинку
в outbound `attachments` (рядом с inline-клавиатурой
`keyboards.back_to_menu_keyboard`).

Без image-relay этот тест должен падать на текущем коде: в
`_send_reply_to_citizen:312` сейчас передаётся только клавиатура,
картинки оператора игнорируются.

Также regression-guard: текстовый ответ без картинки продолжает
работать как раньше — никакого extra-вложения не добавляется.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


def _event_with_image(*, chat_id: int = 100, user_id: int = 7) -> SimpleNamespace:
    event = make_event(chat_id=chat_id, user_id=user_id, with_edit_message=True)
    event.message.link = None
    event.message.body.attachments = [
        {"type": "image", "payload": {"url": "https://cdn.max/img.jpg"}},
    ]
    return event


def _event_without_image(*, chat_id: int = 100, user_id: int = 7) -> SimpleNamespace:
    event = make_event(chat_id=chat_id, user_id=user_id, with_edit_message=True)
    event.message.link = None
    return event


def _fresh_appeal() -> SimpleNamespace:
    user = SimpleNamespace(
        is_blocked=False,
        first_name="Иван",
        consent_pdn_at=datetime(2026, 1, 1, tzinfo=UTC),
        consent_revoked_at=None,
        max_user_id=42,
    )
    return SimpleNamespace(
        id=1, user=user,
        created_at=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        topic="Дороги", locality="Елизово",
        address="ул. Ленина, д. 1", status="new", summary="яма",
        messages=[], answered_at=None,
    )


def _patches_for_delivery():
    """Стандартный набор патчей для прохождения guard'ов в
    `_deliver_operator_reply` до точки доставки и записи."""
    live_op = SimpleNamespace(id=7, max_user_id=42, is_active=True)
    return [
        patch.object(__import__("aemr_bot.handlers.operator_reply",
                                fromlist=["cfg"]).cfg,
                     "answer_max_chars", 1000),
        patch("aemr_bot.handlers.operator_reply.session_scope",
              _fake_session_scope),
        # SEC #6: re-check operator activity. Tests должны вернуть live_op.
        patch("aemr_bot.handlers.operator_reply.operators_service.get",
              AsyncMock(return_value=live_op)),
        patch("aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
              AsyncMock(return_value=_fresh_appeal())),
        patch("aemr_bot.handlers.operator_reply.appeals_service.add_operator_message",
              AsyncMock(return_value=MagicMock(id=99))),
        patch("aemr_bot.handlers.operator_reply.operators_service.write_audit",
              AsyncMock()),
        patch("aemr_bot.handlers.operator_reply._is_reply_success_recorded",
              AsyncMock(return_value=False)),
        patch("aemr_bot.handlers.operator_reply._mark_reply_success_recorded",
              AsyncMock()),
    ]


class TestImageRelay:
    @pytest.mark.asyncio
    async def test_operator_image_attached_to_citizen_message(self) -> None:
        """Контракт: картинка оператора пробрасывается жителю в
        outbound `attachments`. Без relay тест падает (текущий код
        передаёт только клавиатуру)."""
        from aemr_bot.handlers import operator_reply as opr

        event = _event_with_image()
        event.bot.send_message = AsyncMock(
            side_effect=[
                # 1-й вызов: житель
                SimpleNamespace(body=SimpleNamespace(mid="out-1")),
                # 2-й вызов: подтверждение оператору
                None,
            ]
        )
        # фейк pydantic-объекта от deserialize_for_relay — чтобы не
        # зависеть от maxapi
        fake_image_obj = SimpleNamespace(
            type="image", payload={"url": "https://cdn.max/img.jpg"}
        )
        appeal = MagicMock(id=1)
        operator = MagicMock(id=7, max_user_id=42)

        opr._recent_replies.clear()
        with patch("aemr_bot.utils.image_attachments.deserialize_for_relay",
                   return_value=[fake_image_obj]):
            stack = _patches_for_delivery()
            for p in stack:
                p.start()
            try:
                handled = await opr._deliver_operator_reply(
                    event, appeal=appeal, operator=operator,
                    text="ответ с картинкой", audit_action="reply",
                )
            finally:
                for p in stack:
                    p.stop()

        assert handled is True
        # 1-й вызов send_message — к жителю
        first = event.bot.send_message.call_args_list[0]
        assert first.kwargs.get("user_id") == 42
        attachments = first.kwargs.get("attachments", [])
        # картинка должна быть в attachments
        assert fake_image_obj in attachments, (
            f"картинка оператора не пробросилась к жителю; "
            f"attachments={attachments}"
        )

    @pytest.mark.asyncio
    async def test_text_only_reply_regression(self) -> None:
        """Regression-guard: текстовый ответ без картинки — никаких
        лишних вложений сверх клавиатуры."""
        from aemr_bot.handlers import operator_reply as opr

        event = _event_without_image()
        event.bot.send_message = AsyncMock(
            side_effect=[
                SimpleNamespace(body=SimpleNamespace(mid="out-1")),
                None,
            ]
        )
        appeal = MagicMock(id=1)
        operator = MagicMock(id=7, max_user_id=42)

        opr._recent_replies.clear()
        stack = _patches_for_delivery()
        for p in stack:
            p.start()
        try:
            handled = await opr._deliver_operator_reply(
                event, appeal=appeal, operator=operator,
                text="только текст", audit_action="reply",
            )
        finally:
            for p in stack:
                p.stop()

        assert handled is True
        first = event.bot.send_message.call_args_list[0]
        attachments = first.kwargs.get("attachments", [])
        # ровно одна клавиатура, без image-объектов
        assert len(attachments) == 1, (
            f"text-only ответ не должен иметь лишних вложений; "
            f"attachments={attachments}"
        )


@pytest.mark.asyncio
async def test_sent_images_are_saved_with_corresponding_reply():
    from aemr_bot.handlers import operator_reply as opr

    event = _event_with_image()
    event.bot.send_message = AsyncMock(return_value=SimpleNamespace(body=SimpleNamespace(mid='out-1')))
    save = AsyncMock()
    stack = _patches_for_delivery()
    for p in stack:
        p.start()
    try:
        with patch.object(opr.appeals_service, 'add_operator_message', save), \
             patch('aemr_bot.utils.image_attachments.deserialize_for_relay',
                   return_value=[SimpleNamespace(type='image')]):
            opr._recent_replies.clear()
            await opr._deliver_operator_reply(event, appeal=SimpleNamespace(id=1),
                                               operator=SimpleNamespace(id=7, max_user_id=42),
                                               text='Фото результата', audit_action='reply')
    finally:
        for p in stack:
            p.stop()
    assert save.await_args.kwargs['attachments'] == event.message.body.attachments


@pytest.mark.asyncio
@pytest.mark.parametrize('attachments', [
    [{'type': 'image', 'payload': {'url': 'https://cdn.max/img.jpg'}}] * 11,
    [{'type': 'file', 'payload': {'token': 'document'}}],
])
async def test_unsupported_or_too_many_operator_attachments_do_not_send_partial_answer(attachments):
    from aemr_bot.handlers import operator_reply as opr

    event = _event_without_image()
    event.message.body.attachments = attachments
    event.bot.send_message = AsyncMock()
    fresh = _fresh_appeal()
    result = await opr._send_reply_to_citizen(event, fresh_appeal=fresh, appeal_id=1, text='Ответ')
    assert not result[0]
    assert not any(call.kwargs.get('user_id') == fresh.user.max_user_id
                   for call in event.bot.send_message.await_args_list)


def test_identical_text_with_different_photos_is_not_duplicate():
    from aemr_bot.handlers import operator_reply as opr

    first = _event_with_image()
    second = _event_with_image()
    second.message.body.attachments[0]['payload']['url'] = 'https://cdn.max/other.jpg'
    opr._recent_replies.clear()
    opr._remember_successful_reply(7, 1, opr._reply_dedupe_text(first, 'Готово'))
    assert not opr._has_recent_successful_reply(7, 1, opr._reply_dedupe_text(second, 'Готово'))
    assert opr._has_recent_successful_reply(7, 1, opr._reply_dedupe_text(first, 'Готово'))


@pytest.mark.asyncio
async def test_reply_timeout_reports_unknown_delivery_without_false_failure():
    from aemr_bot.handlers import operator_reply as opr

    event = _event_without_image()
    event.bot.send_message = AsyncMock(side_effect=[TimeoutError(), None])
    result = await opr._send_reply_to_citizen(event, fresh_appeal=_fresh_appeal(), appeal_id=1, text='Ответ')
    assert result == (False, None, [])
    notice = event.bot.send_message.await_args_list[1].kwargs['text']
    assert 'неизвестен' in notice
    assert 'мог быть доставлен' in notice


@pytest.mark.asyncio
async def test_reply_without_message_id_requires_manual_delivery_check():
    from aemr_bot.handlers import operator_reply as opr

    event = _event_without_image()
    event.bot.send_message = AsyncMock(side_effect=[None, None])
    result = await opr._send_reply_to_citizen(event, fresh_appeal=_fresh_appeal(), appeal_id=1, text='Ответ')
    assert result == (False, None, [])
    assert 'неизвестен' in event.bot.send_message.await_args_list[1].kwargs['text']
