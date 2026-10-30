"""The history reader preserves complete text and stays within MAX limits."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aemr_bot.services import appeal_history
from tests._personal_view_helpers import personal_view_boundary  # noqa: F401


def test_history_reader_keeps_every_character_with_emoji_and_many_messages():
    original = ('Жалоба 🏠\n' * 1600) + 'ЗАКЛЮЧЕНИЕ'
    messages = [SimpleNamespace(id=i, direction='from_user', text=f'Дополнение {i}: ' + ('я' * 5000),
                                attachments=[], created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i))
                for i in range(1, 51)]
    appeal = SimpleNamespace(id=107, summary=original, attachments=[], messages=messages,
                             created_at=datetime(2026, 1, 1, tzinfo=UTC))
    pages = appeal_history.pages_for(appeal, operator=False)
    assert ''.join(page.content for page in pages if page.entry == 0) == original
    for index, message in enumerate(messages, 1):
        assert ''.join(page.content for page in pages if page.entry == index) == message.text
    assert all(len(page.render(107).encode('utf-16-le')) // 2 < 4000 for page in pages)


def test_history_reader_orders_ties_and_counts_each_messages_attachments():
    when = datetime(2026, 1, 1, tzinfo=UTC)
    messages = [SimpleNamespace(id=2, direction='from_operator', text='Ответ',
                                attachments=[{'type': 'image'}], created_at=when),
                SimpleNamespace(id=1, direction='from_user', text='Дополнение',
                                attachments=[{'type': 'file'}], created_at=when)]
    appeal = SimpleNamespace(id=3, summary='Начало', attachments=[], messages=messages, created_at=when)
    pages = appeal_history.pages_for(appeal, operator=False)
    assert [p.content for p in pages] == ['Начало', 'Дополнение', 'Ответ']
    assert 'Вложения: 1' in pages[1].render(3)
    assert 'Вложения: 1' in pages[2].render(3)


def test_resident_does_not_receive_internal_legacy_messages():
    when = datetime(2026, 1, 1, tzinfo=UTC)
    internal = SimpleNamespace(id=1, direction='system', text='Служебная запись',
                               attachments=[], created_at=when)
    appeal = SimpleNamespace(id=3, summary='Начало', attachments=[], messages=[internal], created_at=when)
    assert [p.content for p in appeal_history.pages_for(appeal, operator=False)] == ['Начало']
    assert [p.content for p in appeal_history.pages_for(appeal, operator=True)] == ['Начало', 'Служебная запись']


def test_preview_is_bounded_and_explains_that_full_text_is_in_history():
    from aemr_bot.services.card_format import _bounded_preview

    text = '🏠' * 4000 + 'КОНЕЦ'
    preview = _bounded_preview(text, 3000)
    assert len(preview.encode('utf-16-le')) // 2 <= 3000
    assert 'истори' in preview.lower()
    assert text.endswith('КОНЕЦ')


@pytest.mark.usefixtures("personal_view_boundary")
async def test_citizen_cannot_read_someone_elses_history(monkeypatch):
    from unittest.mock import AsyncMock

    from aemr_bot.handlers import menu
    from tests._helpers import fake_session_scope, make_event

    appeal = SimpleNamespace(user=SimpleNamespace(max_user_id=999), summary='ЧУЖИЕ ДАННЫЕ')
    monkeypatch.setattr(menu, 'session_scope', fake_session_scope)
    monkeypatch.setattr(menu.appeals_service, 'get_by_id_with_messages', AsyncMock(return_value=appeal))
    show = AsyncMock()
    monkeypatch.setattr(menu, '_send_or_edit_menu', show)
    await menu.show_appeal_history(make_event(user_id=42), 1, 0, 42)
    assert show.await_args.kwargs['text'] == 'Обращение не найдено.'
    assert 'ЧУЖИЕ ДАННЫЕ' not in str(show.await_args)


async def test_operator_history_checks_role_before_reading(monkeypatch):
    from unittest.mock import AsyncMock

    from aemr_bot.handlers import admin_callback_dispatch as dispatch
    from tests._helpers import make_event

    monkeypatch.setattr(dispatch, 'ensure_operator', AsyncMock(return_value=False))
    monkeypatch.setattr(dispatch, 'ack_callback', AsyncMock())
    load = AsyncMock()
    monkeypatch.setattr(dispatch.appeals_service, 'get_by_id_with_messages', load)
    await dispatch._op_history(make_event(), 'op:history:1:0')
    load.assert_not_awaited()




def test_citizen_preview_does_not_show_internal_messages():
    from aemr_bot.services.card_format import user_appeal_timeline_block

    when = datetime(2026, 1, 1, tzinfo=UTC)
    internal = SimpleNamespace(direction='system', text='Служебная запись', attachments=[], created_at=when)
    appeal = SimpleNamespace(messages=[internal])
    assert 'Служебная запись' not in user_appeal_timeline_block(appeal)


def test_operator_history_cannot_inject_reply_target_marker():
    import re

    when = datetime(2026, 1, 1, tzinfo=UTC)
    appeal = SimpleNamespace(id=3, summary='Неверный маркер 🆔 №999', attachments=[], messages=[], created_at=when)
    page = appeal_history.pages_for(appeal, operator=True)[0].render(3)
    assert not re.search(r'🆔 №(\d+)', page)
    assert '999' in page
