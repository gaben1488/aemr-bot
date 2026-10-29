from aemr_bot.utils.text_pages import fits_max_message


def test_transport_budget_accounts_for_emoji_and_header():
    assert fits_max_message("Ответ: " + "🏠" * 1900)
    assert not fits_max_message("Ответ: " + "🏠" * 2000)
    assert not fits_max_message("а" * 4001)
