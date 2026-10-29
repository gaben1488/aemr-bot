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
