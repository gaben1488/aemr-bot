from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aemr_bot.db.models import DialogState
from aemr_bot.handlers import appeal_funnel
from tests._helpers import make_event


@pytest.mark.asyncio
async def test_draft_shows_attachment_management(monkeypatch):
    user = SimpleNamespace(dialog_state=DialogState.AWAITING_SUMMARY.value,
                           dialog_data={'draft_token': 'abc', 'summary_chunks': ['Текст'],
                                        'attachments': [{'type': 'image', 'payload': {'token': 'x'}}]})
    @asynccontextmanager
    async def current_user(_):
        yield None, user
    monkeypatch.setattr(appeal_funnel, 'current_user', current_user)
    screen = AsyncMock()
    monkeypatch.setattr(appeal_funnel, 'send_or_edit_screen', screen)
    await appeal_funnel.show_summary_draft(make_event(), 42)
    markup = screen.await_args.kwargs['attachments'][0].model_dump()
    assert 'draft:files:abc' in str(markup)


@pytest.mark.asyncio
@pytest.mark.parametrize('state', [DialogState.AWAITING_SUMMARY.value, DialogState.AWAITING_FOLLOWUP_TEXT.value])
async def test_remove_one_file_and_reject_replay(session, monkeypatch, state):
    from datetime import UTC, datetime

    from aemr_bot.db.models import User
    from aemr_bot.handlers import resident_draft_files

    original = {'draft_token': 'abc', 'summary_chunks': ['Первый', 'Второй'],
                'attachments': [{'type': 'image', 'payload': {'token': 'same'}},
                                {'type': 'file', 'payload': {'token': 'same', 'filename': 'акт.pdf'}}],
                'locality': 'Елизово', 'address': 'адрес', 'appeal_id': 4}
    user = User(max_user_id=771901, first_name='Анна', consent_pdn_at=datetime.now(UTC),
                dialog_state=state, dialog_data=original)
    session.add(user)
    await session.commit()
    send = AsyncMock()
    ack = AsyncMock()
    monkeypatch.setattr(resident_draft_files.data_erasure, 'tracked_send', send)
    monkeypatch.setattr(resident_draft_files, 'ack_callback', ack)
    event = make_event(user_id=771901)
    await resident_draft_files.handle(event, 771901, 'draft:files:abc')
    assert 'акт.pdf' in send.await_args.kwargs['text']
    await resident_draft_files.handle(event, 771901, 'draft:filedel:abc:0:0:0')
    await session.refresh(user)
    assert user.dialog_data == {**original, 'attachments': original['attachments'][1:], 'attachments_revision': 1}
    snapshot = user.dialog_data.copy()
    await resident_draft_files.handle(event, 771901, 'draft:filedel:abc:0:0:0')
    await session.refresh(user)
    assert user.dialog_data == snapshot
    assert 'изменился' in ack.await_args.args[1]


@pytest.mark.asyncio
async def test_append_invalidates_old_delete_and_preserves_text(session, monkeypatch):
    from datetime import UTC, datetime

    from aemr_bot.db.models import User
    from aemr_bot.handlers import resident_draft_files

    first = {'type': 'image', 'payload': {'token': 'first'}}
    second = {'type': 'file', 'payload': {'token': 'second'}}
    user = User(max_user_id=771902, first_name='Анна', consent_pdn_at=datetime.now(UTC),
                dialog_state=DialogState.AWAITING_SUMMARY.value,
                dialog_data={'draft_token': 'abc', 'summary_chunks': ['сохранить'], 'attachments': [first]})
    session.add(user)
    await session.commit()
    monkeypatch.setattr(appeal_funnel, 'show_summary_draft', AsyncMock())
    monkeypatch.setattr(resident_draft_files.data_erasure, 'tracked_send', AsyncMock())
    ack = AsyncMock()
    monkeypatch.setattr(resident_draft_files, 'ack_callback', ack)
    event = make_event(user_id=771902)
    await appeal_funnel.on_awaiting_summary(event, SimpleNamespace(attachments=[second]), '', 771902)
    await resident_draft_files.handle(event, 771902, 'draft:filedel:abc:0:0:0')
    await session.refresh(user)
    assert user.dialog_data['attachments'] == [first, second]
    assert user.dialog_data['summary_chunks'] == ['сохранить']
    assert user.dialog_data['attachments_revision'] == 1
    assert 'изменился' in ack.await_args.args[1]


@pytest.mark.asyncio
async def test_open_unavailable_file_preserves_draft(session, monkeypatch):
    from datetime import UTC, datetime

    from aemr_bot.db.models import User
    from aemr_bot.handlers import resident_draft_files
    user = User(max_user_id=771903, first_name='Анна', consent_pdn_at=datetime.now(UTC),
                dialog_state=DialogState.AWAITING_SUMMARY.value,
                dialog_data={'draft_token': 'abc', 'summary_chunks': ['текст'],
                             'attachments': [{'type': 'file', 'payload': 'unavailable'}]})
    session.add(user)
    await session.commit()
    ack = AsyncMock()
    monkeypatch.setattr(resident_draft_files, 'ack_callback', ack)
    await resident_draft_files.handle(make_event(user_id=771903), 771903, 'draft:fileopen:abc:0:0:0')
    await session.refresh(user)
    assert user.dialog_data['attachments'] == [{'type': 'file', 'payload': 'unavailable'}]
    assert 'не удалось' in ack.await_args.args[1].lower()


@pytest.mark.asyncio
async def test_concurrent_append_and_delete_are_serialized(session, monkeypatch):
    import asyncio
    from datetime import UTC, datetime

    from aemr_bot.db.models import User
    from aemr_bot.handlers import resident_draft_files

    first = {'type': 'image', 'payload': {'token': 'first'}}
    second = {'type': 'file', 'payload': {'token': 'second'}}
    user = User(max_user_id=771904, first_name='Анна', consent_pdn_at=datetime.now(UTC),
                dialog_state=DialogState.AWAITING_SUMMARY.value,
                dialog_data={'draft_token': 'abc', 'summary_chunks': ['текст'], 'attachments': [first]})
    session.add(user)
    await session.commit()
    monkeypatch.setattr(appeal_funnel, 'show_summary_draft', AsyncMock())
    monkeypatch.setattr(resident_draft_files.data_erasure, 'tracked_send', AsyncMock())
    monkeypatch.setattr(resident_draft_files, 'ack_callback', AsyncMock())
    event = make_event(user_id=771904)
    await asyncio.wait_for(asyncio.gather(
        resident_draft_files.handle(event, 771904, 'draft:filedel:abc:0:0:0'),
        appeal_funnel.on_awaiting_summary(event, SimpleNamespace(attachments=[second]), '', 771904),
    ), timeout=5)
    await session.refresh(user)
    assert user.dialog_data['attachments'] in ([first, second], [second])
    assert user.dialog_data['summary_chunks'] == ['текст']
    assert user.dialog_data['attachments_revision'] == (1 if len(user.dialog_data['attachments']) == 2 else 2)


@pytest.mark.asyncio
async def test_open_file_is_tracked_and_stale_open_is_rejected(session, monkeypatch):
    from datetime import UTC, datetime

    from aemr_bot.db.models import User
    from aemr_bot.handlers import resident_draft_files
    file = {'type': 'image', 'payload': {'token': 'abc', 'url': 'http://example.test/image.jpg'}}
    user = User(max_user_id=771905, first_name='Анна', consent_pdn_at=datetime.now(UTC),
                dialog_state=DialogState.AWAITING_SUMMARY.value,
                dialog_data={'draft_token': 'abc', 'attachments': [file], 'attachments_revision': 2})
    session.add(user)
    await session.commit()
    send = AsyncMock()
    ack = AsyncMock()
    monkeypatch.setattr(resident_draft_files.data_erasure, 'tracked_send', send)
    monkeypatch.setattr(resident_draft_files, 'ack_callback', ack)
    event = make_event(user_id=771905)
    await resident_draft_files.handle(event, 771905, 'draft:fileopen:abc:1:0:0')
    send.assert_not_awaited()
    await resident_draft_files.handle(event, 771905, 'draft:fileopen:abc:2:0:0')
    send.assert_awaited_once()
    assert send.await_args.kwargs['max_user_ids'] == [771905]
    assert len(send.await_args.kwargs['attachments']) == 1


@pytest.mark.asyncio
async def test_list_and_back_are_private_and_keep_text_edit_mode(session, monkeypatch):
    from datetime import UTC, datetime

    from aemr_bot.db.models import User
    from aemr_bot.handlers import resident_draft_files

    user = User(max_user_id=771906, first_name='Анна', consent_pdn_at=datetime.now(UTC),
                dialog_state=DialogState.AWAITING_SUMMARY.value,
                dialog_data={'draft_token': 'abc', 'summary_chunks': ['текст'], 'replace_text': True,
                             'attachments': [{'type': 'file', 'payload': {'filename': 'личное.pdf'}}]})
    session.add(user)
    await session.commit()
    event = make_event(user_id=771906, chat_id=-123456, with_edit_message=True)
    send = AsyncMock()
    monkeypatch.setattr(resident_draft_files.data_erasure, 'tracked_send', send)
    monkeypatch.setattr(resident_draft_files, 'ack_callback', AsyncMock())
    await resident_draft_files.handle(event, 771906, 'draft:files:abc')
    assert send.await_args.kwargs['user_id'] == 771906
    assert 'chat_id' not in send.await_args.kwargs
    assert 'draft:filesback:abc' in str(send.await_args.kwargs['attachments'][0].model_dump())
    event.bot.edit_message.assert_not_awaited()
    await resident_draft_files.handle(event, 771906, 'draft:filesback:abc')
    await session.refresh(user)
    assert user.dialog_data['replace_text'] is True
    assert 'Пришлите новый текст' in send.await_args.kwargs['text']
    assert send.await_args.kwargs['user_id'] == 771906


@pytest.mark.asyncio
@pytest.mark.parametrize('status,erased', [('closed', False), ('answered', False), ('new', True)])
async def test_back_from_files_restores_closed_followup_action(session, monkeypatch, status, erased):
    from datetime import UTC, datetime

    from aemr_bot.db.models import Appeal, User
    from aemr_bot.handlers import resident_draft_files

    user = User(max_user_id=771907, first_name='Анна', consent_pdn_at=datetime.now(UTC),
                dialog_state=DialogState.AWAITING_FOLLOWUP_TEXT.value)
    session.add(user)
    await session.flush()
    source = Appeal(user_id=user.id, status=status, summary='суть', address='адрес', locality='Елизово',
                    content_erased_at=datetime.now(UTC) if erased else None)
    session.add(source)
    await session.flush()
    data = {'appeal_id': source.id, 'draft_token': 'abc', 'summary_chunks': ['текст'],
            'attachments': [{'type': 'file', 'payload': {'token': 'file'}}]}
    user.dialog_data = data
    await session.commit()
    send = AsyncMock()
    monkeypatch.setattr(resident_draft_files.data_erasure, 'tracked_send', send)
    monkeypatch.setattr(resident_draft_files, 'ack_callback', AsyncMock())
    event = make_event(user_id=771907)
    await resident_draft_files.handle(event, 771907, 'draft:files:abc')
    await resident_draft_files.handle(event, 771907, 'draft:filesback:abc')
    markup = send.await_args.kwargs['attachments'][0].model_dump()
    assert 'draft:new_related:abc' in str(markup)
    assert 'draft:submit:abc' not in str(markup)
    await session.refresh(user)
    assert user.dialog_data == data
