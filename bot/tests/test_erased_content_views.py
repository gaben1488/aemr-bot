from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from xml.etree import ElementTree
from zipfile import ZipFile

from aemr_bot.services.appeal_history import pages_for
from aemr_bot.services.appeal_word import build_appeal_docx
from aemr_bot.services.card_format import admin_card, user_card


def appeal():
    return SimpleNamespace(id=1, created_at=datetime.now(UTC), answered_at=None,
        closed_at=None, status="closed", locality="Елизово", address=None, topic="Дорога",
        summary=None, attachments=[], messages=[], content_erased_at=datetime.now(UTC),
        user=SimpleNamespace(first_name="Анна", phone=None, is_blocked=False,
            consent_pdn_at=None, consent_revoked_at=None, subscribed_broadcast=False,
            consent_records=[]), closed_due_to_revoke=False)


def test_retention_erasure_is_explicit_in_both_cards_and_history():
    record = appeal()
    assert "очищен" in admin_card(record, record.user)
    assert "очищен" in user_card(record)
    for operator in (False, True):
        assert "очищен" in pages_for(record, operator=operator)[0].render(record.id)


def test_word_distinguishes_erased_content_from_never_supplied_text():
    with ZipFile(BytesIO(build_appeal_docx(appeal()))) as archive:
        text = "".join(ElementTree.fromstring(archive.read("word/document.xml")).itertext())
    assert "очищен" in text
