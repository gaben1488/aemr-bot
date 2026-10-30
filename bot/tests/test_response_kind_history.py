"""Historical replies retain their recorded kind across presentations."""
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

from aemr_bot.services.appeal_history import pages_for
from aemr_bot.services.appeal_word import build_appeal_docx
from aemr_bot.services.message_labels import response_label


def _appeal(messages):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    user = SimpleNamespace(first_name="Житель", phone=None, consent_records=[])
    return SimpleNamespace(id=17, status="in_progress", created_at=now, user=user,
                           locality="Елизово", address="Адрес", topic="Дорога",
                           summary="Текст", attachments=[], messages=messages,
                           related_appeal_id=None)


def _message(kind, body, number):
    return SimpleNamespace(id=number, direction="from_operator", response_kind=kind,
                           text=body, attachments=[], created_at=datetime(2026, 1, number, tzinfo=UTC))


def test_recorded_reply_kind_is_consistent_in_resident_and_operator_history():
    appeal = _appeal([_message("intermediate", "Уточняем", 1),
                      _message("final", "Решено", 2), _message(None, "Старая запись", 3)])
    resident = [p.label for p in pages_for(appeal, operator=False)]
    operator = [p.label for p in pages_for(appeal, operator=True)]
    assert resident[1:] == ["Промежуточный ответ администрации", "Окончательный ответ администрации", "Ответ администрации"]
    assert operator[1:] == ["Промежуточный ответ специалиста", "Окончательный ответ специалиста", "Ответ специалиста"]
    with ZipFile(BytesIO(build_appeal_docx(appeal))) as doc:
        xml = doc.read("word/document.xml").decode()
    assert "Промежуточный ответ специалиста" in xml
    assert "Окончательный ответ специалиста" in xml
    assert "Старая запись" in xml


def test_missing_historical_kind_is_never_guessed():
    assert response_label(SimpleNamespace(), citizen=True) == "Ответ администрации"
