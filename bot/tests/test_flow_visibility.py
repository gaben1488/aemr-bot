from aemr_bot.services import flow_followup_policy as policy


def test_append_allowed_only_for_open_statuses():
    assert policy.can_append("new") is True
    assert policy.can_append("in_progress") is True
    assert policy.can_append("answered") is False
    assert policy.can_append("closed") is False


def test_user_card_keyboard_exists_for_all_statuses():
    for status in ["new", "in_progress", "answered", "closed", "unknown"]:
        assert policy.user_card_keyboard(10, status) is not None
