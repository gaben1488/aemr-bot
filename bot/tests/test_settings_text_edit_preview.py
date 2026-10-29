from unittest.mock import AsyncMock, patch

from aemr_bot.handlers import admin_settings_text as settings_text
from tests._helpers import fake_session_scope, make_event


async def test_edit_shows_the_current_text_without_prior_copy():
    value = 'Здравствуйте, жители Елизовского округа!'
    event = make_event()
    with patch.object(settings_text, 'session_scope', fake_session_scope), \
         patch.object(settings_text.settings_store, 'get_versioned', AsyncMock(return_value=(value, 1))), \
         patch.object(settings_text, 'op_screen', AsyncMock()) as show:
        await settings_text._start_edit_intent(event, 42, 'welcome_text')
    assert value in show.await_args.args[1]
    assert 'новый текст' in show.await_args.args[1].lower()


async def test_long_edit_preview_offers_all_text_in_safe_pages():
    value = '🏠' * 1800 + 'КОНЕЦ ТЕКСТА'
    event = make_event()
    with patch.object(settings_text, 'session_scope', fake_session_scope), \
         patch.object(settings_text.settings_store, 'get_versioned', AsyncMock(return_value=(value, 1))), \
         patch.object(settings_text, 'op_screen', AsyncMock()) as show:
        await settings_text._start_edit_intent(event, 42, 'welcome_text')
        first = show.await_args.args[1]
        await settings_text._show_edit_page(event, 42, 'welcome_text', 1)
        second = show.await_args.args[1]
    assert 'КОНЕЦ ТЕКСТА' in second
    assert len(first.encode('utf-16-le')) // 2 < 4000
    assert len(second.encode('utf-16-le')) // 2 < 4000


async def test_old_navigation_does_not_start_a_new_edit_intent():
    settings_text._edit_intents.clear()
    event = make_event()
    with patch.object(settings_text.settings_store, 'get_versioned', AsyncMock()) as get, \
         patch.object(settings_text, 'op_screen', AsyncMock()) as show:
        await settings_text._show_edit_page(event, 42, 'welcome_text', 1)
    get.assert_not_awaited()
    assert 'заново' in show.await_args.args[1]
    assert settings_text._intent_get(42) is None
