"""Safety-critical guidance must not discourage timely action after fraud."""

from aemr_bot.copy.security_texts import OP_HELP_SECURITY, SECURITY_INFO_TEXT
from aemr_bot.services.card_format import _maybe_url_warning


def test_fraud_guidance_directs_resident_and_operator_to_bank() -> None:
    for text in (SECURITY_INFO_TEXT, OP_HELP_SECURITY):
        assert "банк" in text.lower()
        assert "вернуть деньги, как правило, нельзя" not in text.lower()


def test_money_theft_guidance_includes_bank_claim_and_routine_police_report() -> None:
    for text in (SECURITY_INFO_TEXT, OP_HELP_SECURITY):
        assert "заявление" in text.lower()
        assert "несогласии с операцией" in text
        assert "суток" in text
        assert "любое отделение полиции" in text
        assert "при срочной опасности — 112" in text
        assert "Возврат денег не гарантирован" in text


def test_resident_can_report_a_suspicious_message_without_opening_it() -> None:
    assert "📝 Написать обращение" in SECURITY_INFO_TEXT
    assert "«Другое»" in SECURITY_INFO_TEXT
    assert "не открывая ссылку" in SECURITY_INFO_TEXT


async def test_resident_suspicious_link_report_and_followup_are_preserved(session, monkeypatch):
    from aemr_bot.db.models import User
    from aemr_bot.services import appeals, threat_intel
    from aemr_bot.services.card_format import admin_card, admin_followup

    store = threat_intel.ThreatIntelStore(hosts={"known-phish.test"})
    monkeypatch.setattr(threat_intel, "get_store", lambda: store)
    user = User(max_user_id=808, first_name="Житель")
    session.add(user)
    await session.flush()
    report = "Мне прислали https://known-phish.test/login, это мошенники?"
    appeal = await appeals.create_appeal(session, user, "Елизово", "Другое", report, [])
    followup = await appeals.add_user_message(session, appeal, report)
    await session.refresh(appeal, ["messages"])
    assert appeal.summary == report
    assert followup.text == report
    for rendered in (admin_card(appeal, user), admin_followup(appeal, user, followup.text)):
        assert "⛔" in rendered
        assert "https://known-phish.test/login" not in rendered


def test_operator_warning_does_not_recommend_visiting_unverified_link(monkeypatch) -> None:
    from aemr_bot.services import threat_intel

    class EmptyStore:
        def is_malicious(self, candidate):
            return False, None

    monkeypatch.setattr(threat_intel, "get_store", lambda: EmptyStore())
    text = _maybe_url_warning("Ссылка https://example.test/path")
    assert "не делает ссылку безопасной" in text
    assert "введите в браузер вручную" not in text


def test_feed_match_is_a_risk_indicator_not_a_verdict(monkeypatch) -> None:
    from aemr_bot.services import threat_intel

    class MatchedStore:
        def is_malicious(self, candidate):
            return True, "источник"

    monkeypatch.setattr(threat_intel, "get_store", lambda: MatchedStore())
    text = _maybe_url_warning("Ссылка https://example.test/path")
    assert "совпал" in text and "ошибаться" in text
    assert "Не открывайте" in text
