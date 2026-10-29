from aemr_bot.services.complete_stats import cell_chunks
from io import BytesIO

import pytest
from openpyxl import load_workbook


def test_excel_long_text_is_preserved_and_each_chunk_fits():
    original = "Ж" * 70000
    chunks = cell_chunks(original)
    assert "".join(chunks) == original
    assert all(len(chunk) <= 32766 for chunk in chunks)


def test_invalid_xml_character_is_identified_instead_of_dropped():
    assert cell_chunks("начало\x01конец") == ["начало⟦U+0001⟧конец"]


@pytest.mark.asyncio
async def test_report_includes_all_history_and_attachment_labels(session):
    from aemr_bot.db.models import Appeal, Message, User
    from aemr_bot.services.complete_stats import build_xlsx

    user = User(max_user_id=490101, first_name="Иван")
    session.add(user)
    await session.flush()
    appeal = Appeal(user_id=user.id, topic="Дорога", summary="Яма " * 3000,
                    attachments=[{"type": "image", "payload": {"token": "never-export-token"}}])
    session.add(appeal)
    await session.flush()
    session.add_all([Message(appeal_id=appeal.id, direction="from_user", text=f"Дополнение {i}")
                     for i in range(50)])
    await session.flush()
    content, _, count = await build_xlsx(session, "all")
    workbook = load_workbook(BytesIO(content), read_only=True, data_only=True)
    rows = list(workbook["История"].values)
    assert count == 1
    assert len(rows) == 52  # header, original, 50 messages
    assert rows[1][-1] == "Яма " * 3000
    assert rows[-1][-1] == "Дополнение 49"
    attachment = list(workbook["Вложения"].values)[1]
    assert "Фото" in attachment[-1]
    assert "never-export-token" not in str(attachment)
