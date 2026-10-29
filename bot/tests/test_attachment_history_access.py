"""Attachments sent by either side remain available from the appeal history."""
from types import SimpleNamespace

from aemr_bot.services import admin_relay


def test_attachment_collection_includes_resident_and_operator_without_internal_records():
    appeal = SimpleNamespace(attachments=[{'type': 'image', 'payload': {'token': 'initial'}}], messages=[
        SimpleNamespace(direction='from_user', attachments=[{'type': 'file', 'payload': {'token': 'citizen'}}]),
        SimpleNamespace(direction='from_operator', attachments=[{'type': 'image', 'payload': {'token': 'operator'}}]),
        SimpleNamespace(direction='system', attachments=[{'type': 'file', 'payload': {'token': 'internal'}}]),
    ])
    assert [a['payload']['token'] for a in admin_relay.collect_appeal_attachments(appeal)] == [
        'initial', 'citizen', 'operator',
    ]


def test_transport_groups_documents_separately_from_photo_and_video():
    media1 = SimpleNamespace(type='image')
    file1 = SimpleNamespace(type='file')
    media2 = SimpleNamespace(type='video')
    file2 = SimpleNamespace(type='file')
    assert admin_relay._chunk_relayable([media1, file1, media2, file2]) == [
        [media1], [file1], [media2], [file2],
    ]


async def test_unavailable_attachments_are_reported_instead_of_silently_ignored(monkeypatch):
    from unittest.mock import AsyncMock

    bot = SimpleNamespace(send_message=AsyncMock())
    appeal = SimpleNamespace(id=3, attachments=[{'type': 'file', 'payload': {'token': 'expired'}}], messages=[])
    monkeypatch.setattr(admin_relay, 'deserialize_for_relay', lambda _: [])
    await admin_relay.render_appeal_attachments(bot, chat_id=None, user_id=42, appeal=appeal)
    assert bot.send_message.await_count == 1
    assert 'не удалось' in bot.send_message.await_args.kwargs['text'].lower()


async def test_uncertain_attachment_delivery_is_not_retried(monkeypatch):
    from unittest.mock import AsyncMock

    from aemr_bot.services import admin_bus

    monkeypatch.setattr(admin_relay.cfg, 'admin_group_id', 555)
    monkeypatch.setattr(admin_relay, 'deserialize_for_relay', lambda a: a)
    alert = AsyncMock()
    monkeypatch.setattr(admin_bus, 'send', alert)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError()))
    result = await admin_relay.relay_attachments_to_admin(
        bot, appeal_id=3, admin_mid=None, stored_attachments=[{'type': 'image'}],
    )
    assert result is False
    assert bot.send_message.await_count == 1
    assert 'неизвест' in alert.await_args.kwargs['text'].lower()


async def test_operator_response_persists_the_delivered_attachments():
    from unittest.mock import AsyncMock

    from aemr_bot.services import appeals

    saved = []
    session = SimpleNamespace(add=saved.append, flush=AsyncMock())
    appeal = SimpleNamespace(id=3, status='new', assigned_operator_id=None)
    photos = [{'type': 'image', 'payload': {'token': 'photo'}}]
    message = await appeals.add_operator_message(session, appeal, 'Готово', operator_id=7,
                                                  max_message_id='delivered', attachments=photos)
    assert message.attachments == photos
    assert saved == [message]
    assert appeal.status == 'answered'


def test_history_pages_point_to_the_specific_record_attachments():
    from datetime import UTC, datetime

    from aemr_bot.services import appeal_history

    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = {'type': 'image', 'payload': {'token': 'initial'}}
    second = {'type': 'image', 'payload': {'token': 'operator'}}
    reply = SimpleNamespace(id=92, direction='from_operator', text='Готово', attachments=[second], created_at=now)
    appeal = SimpleNamespace(id=3, summary='Проблема', attachments=[first], messages=[reply], created_at=now)
    pages = appeal_history.pages_for(appeal, operator=False)
    assert pages[0].message_id == 0
    assert pages[1].message_id == 92
    assert appeal_history.attachments_for(appeal, 0, operator=False) == [first]
    assert appeal_history.attachments_for(appeal, 92, operator=False) == [second]
    assert appeal_history.attachments_for(appeal, 999, operator=False) is None


async def test_resident_cannot_get_another_residents_record_attachments(monkeypatch):
    from unittest.mock import AsyncMock

    from aemr_bot.handlers import menu
    from tests._helpers import fake_session_scope, make_event

    appeal = SimpleNamespace(id=5, user=SimpleNamespace(max_user_id=900), attachments=[{'type': 'image'}], messages=[])
    monkeypatch.setattr(menu, 'session_scope', fake_session_scope)
    monkeypatch.setattr(menu.appeals_service, 'get_by_id_with_messages', AsyncMock(return_value=appeal))
    relay = AsyncMock()
    monkeypatch.setattr(menu.admin_relay, 'render_appeal_attachments', relay)
    monkeypatch.setattr(menu, '_send_or_edit_menu', AsyncMock())
    await menu.show_record_attachments(make_event(user_id=42), 5, 0, 42)
    relay.assert_not_awaited()


async def test_resident_gets_only_selected_record(monkeypatch):
    from unittest.mock import AsyncMock

    from aemr_bot.handlers import menu
    from tests._helpers import fake_session_scope, make_event

    selected = [{'type': 'image', 'payload': {'token': 'sent-photo'}}]
    msg = SimpleNamespace(id=8, direction='from_operator', attachments=selected)
    appeal = SimpleNamespace(id=5, user=SimpleNamespace(max_user_id=42), attachments=[{'type': 'file'}], messages=[msg])
    monkeypatch.setattr(menu, 'session_scope', fake_session_scope)
    monkeypatch.setattr(menu.appeals_service, 'get_by_id_with_messages', AsyncMock(return_value=appeal))
    relay = AsyncMock()
    monkeypatch.setattr(menu.admin_relay, 'render_appeal_attachments', relay)
    await menu.show_record_attachments(make_event(user_id=42), 5, 8, 42)
    assert relay.await_args.kwargs['attachments_override'] == selected
