from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from xml.etree import ElementTree
from zipfile import ZipFile

from aemr_bot.services.appeal_word import build_appeal_docx


def test_word_export_keeps_long_history_and_identifies_attachments():
    text = "Яма после дождя. " * 900
    appeal = SimpleNamespace(
        id=107, created_at=datetime.now(UTC), status="in_progress", locality="Елизово",
        address="Ленина, 15", topic="Дорога", summary=text,
        attachments=[{"type": "image", "payload": {"token": "secret-token"}}],
        user=SimpleNamespace(first_name="Иван", phone=None, consent_records=[]),
        messages=[SimpleNamespace(id=1, direction="from_operator", created_at=datetime.now(UTC),
                                  text="Ответ специалиста", attachments=[{"type": "file"}])],
    )
    with ZipFile(BytesIO(build_appeal_docx(appeal))) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    contents = "".join(root.itertext())
    assert text in contents
    assert "Ответ специалиста" in contents
    assert "Фото" in contents and "Файл" in contents
    assert "secret-token" not in contents


def test_word_export_preserves_consent_editions_and_control_character():
    appeal = SimpleNamespace(
        id=1, created_at=datetime.now(UTC), status="closed", locality=None,
        address="", topic="", summary="До\x01после", attachments=[], messages=[],
        user=SimpleNamespace(first_name=None, phone=None,
                             consent_records=[{"accepted_at": "2026-10-01T01:00:00+00:00",
                                               "text": "Условия, показанные жителю"}]),
    )
    with ZipFile(BytesIO(build_appeal_docx(appeal))) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    text = "".join(root.itertext())
    assert "До⟦U+0001⟧после" in text
    assert "Условия, показанные жителю" in text
