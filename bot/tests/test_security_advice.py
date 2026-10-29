"""Safety-critical guidance must not discourage timely action after fraud."""

from aemr_bot.copy.security_texts import OP_HELP_SECURITY, SECURITY_INFO_TEXT
from aemr_bot.services.card_format import _maybe_url_warning


def test_fraud_guidance_directs_resident_and_operator_to_bank() -> None:
    for text in (SECURITY_INFO_TEXT, OP_HELP_SECURITY):
        assert "банк" in text.lower()
        assert "вернуть деньги, как правило, нельзя" not in text.lower()


def test_operator_warning_does_not_recommend_visiting_unverified_link(monkeypatch) -> None:
    from aemr_bot.services import threat_intel

    class EmptyStore:
        def is_malicious(self, candidate):
            return False, None

    monkeypatch.setattr(threat_intel, "get_store", lambda: EmptyStore())
    text = _maybe_url_warning("Ссылка https://example.test/path")
    assert "не делает ссылку безопасной" in text
    assert "введите в браузер вручную" not in text
