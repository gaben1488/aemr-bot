"""The history reader preserves complete text and stays within MAX limits."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from aemr_bot.services import appeal_history


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


def test_preview_is_bounded_and_explains_that_full_text_is_in_history():
    from aemr_bot.services.card_format import _bounded_preview

    text = '🏠' * 4000 + 'КОНЕЦ'
    preview = _bounded_preview(text, 3000)
    assert len(preview.encode('utf-16-le')) // 2 <= 3000
    assert 'истори' in preview.lower()
    assert text.endswith('КОНЕЦ')


async def test_citizen_cannot_read_someone_elses_history(monkeypatch):
    from unittest.mock import AsyncMock

    from aemr_bot.handlers import menu
    from tests._helpers import fake_session_scope, make_event

    appeal = SimpleNamespace(user=SimpleNamespace(max_user_id=999), summary='ЧУЖИЕ ДАННЫЕ')
    monkeypatch.setattr(menu, 'session_scope', fake_session_scope)
    monkeypatch.setattr(menu.appeals_service, 'get_by_id_with_messages', AsyncMock(return_value=appeal))
    show = AsyncMock()
    monkeypatch.setattr(menu, '_send_or_edit_menu', show)
    await menu.show_appeal_history(make_event(), 1, 0, 42)
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


async def test_opening_card_is_a_view_not_a_new_event(monkeypatch):
    from unittest.mock import AsyncMock

    from aemr_bot.handlers import admin_callback_dispatch as dispatch
    from tests._helpers import fake_session_scope, make_event

    user = SimpleNamespace(is_blocked=False)
    appeal = SimpleNamespace(id=7, user=user, status='new', closed_due_to_revoke=False)
    monkeypatch.setattr(dispatch, 'session_scope', fake_session_scope)
    monkeypatch.setattr(dispatch, 'ensure_operator', AsyncMock(return_value=True))
    monkeypatch.setattr(dispatch, 'ack_callback', AsyncMock())
    monkeypatch.setattr(dispatch.appeals_service, 'get_by_id_with_messages', AsyncMock(return_value=appeal))
    monkeypatch.setattr(dispatch.card_format, 'admin_card', lambda a, u: 'Карточка')
    monkeypatch.setattr(dispatch.admin_card_service, '_count_attachments', lambda a: 0)
    event_card = AsyncMock()
    monkeypatch.setattr(dispatch.admin_card_service, 'render', event_card)
    show = AsyncMock()
    monkeypatch.setattr(dispatch, 'send_or_edit_screen', show)
    await dispatch._op_open_card(make_event(), 7)
    event_card.assert_not_awaited()
    assert show.await_args.kwargs['text'] == 'Карточка'
