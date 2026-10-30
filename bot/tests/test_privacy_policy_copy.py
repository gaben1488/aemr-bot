"""Resident decisions must describe the implemented consent and copy policy."""

import pytest

from aemr_bot.copy import citizen_funnel as texts


@pytest.mark.parametrize("name", [
    "GOODBYE_PROMPT", "CONSENT_STATUS_REVOKED",
    "CONSENT_REVOKE_CONFIRM", "CONSENT_REVOKED_OK",
])
def test_revoke_screens_explain_that_bot_replies_stop(name):
    text = getattr(texts, name)
    assert "Ответы через бот после отзыва не отправляются" in text
    assert "официальные каналы" in text


@pytest.mark.parametrize("name", ["ERASE_CONFIRM", "ERASE_REQUESTED"])
def test_erase_screens_explain_manual_external_cleanup(name):
    text = getattr(texts, name)
    assert "MAX" in text
    assert "выгрузки" in text
    assert "сотрудники" in text
    assert "автоматически не удаляет" in text
