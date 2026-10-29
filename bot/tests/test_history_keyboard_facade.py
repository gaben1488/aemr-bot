def test_history_keyboard_is_available_to_both_handlers():
    from aemr_bot import keyboards

    assert keyboards.appeal_history_keyboard(123, 0, 1, operator=False) is not None
    assert keyboards.appeal_history_keyboard(123, 0, 1, operator=True) is not None
