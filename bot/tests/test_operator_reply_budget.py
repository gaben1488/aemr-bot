from aemr_bot.utils.text_pages import fits_max_message

import pytest


def test_application_default_stays_below_pinned_sdk_strict_boundary():
    # The official API says up to 4000 characters; maxapi 1.2.3 rejects
    # Python len >= 4000. The application's UTF-16 accounting is conservative.
    from maxapi.methods.edit_message import EditMessage
    from maxapi.methods.send_message import SendMessage

    for constructor, kwargs in ((SendMessage, {"chat_id": 1}),
                                (EditMessage, {"message_id": "synthetic"})):
        constructor(bot=object(), text="а" * 3999, **kwargs)
        with pytest.raises(ValueError, match="4000"):
            constructor(bot=object(), text="а" * 4000, **kwargs)
    assert fits_max_message("а" * 3999)
    assert not fits_max_message("а" * 4000)


def test_conservative_emoji_budget_is_distinct_from_sdk_codepoint_count():
    # 2000 non-BMP characters use 4000 UTF-16 units. This SDK accepts
    # their Python length, while the application deliberately rejects them.
    from maxapi.methods.send_message import SendMessage
    SendMessage(bot=object(), chat_id=1, text="🏠" * 2000)
    assert fits_max_message("🏠" * 1999 + "а")
    assert not fits_max_message("🏠" * 2000)


def test_transport_budget_accounts_for_emoji_and_header():
    assert fits_max_message("Ответ: " + "🏠" * 1900)
    assert not fits_max_message("Ответ: " + "🏠" * 2000)
    assert not fits_max_message("а" * 4001)
