"""Evidence records preserve the text actually shown, including later changes."""
from datetime import UTC, datetime

import pytest

from aemr_bot.db.models import User
from aemr_bot.services import users


def test_consent_pages_preserve_full_text_and_label_unknown_legacy():
    from aemr_bot.services.consent_history import pages_for

    text = "🏠" * 4100 + "Конец"
    pages = pages_for([{"accepted_at": "2026-10-01T01:00:00+00:00", "text": text}])
    assert "".join(page.text for page in pages) == text
    assert all(len(page.text.encode("utf-16-le")) // 2 <= 2600 for page in pages)
    assert not pages_for([])


@pytest.mark.asyncio
async def test_record_exact_text_and_do_not_duplicate_same_prompt(session):
    resident = User(max_user_id=19012001, first_name="Иван")
    session.add(resident)
    await session.flush()
    await users.set_consent(session, resident.max_user_id, text_sha256="a" * 64,
                            consent_text="Первая редакция. Ссылка: первая", prompt_mid="prompt-1")
    await users.set_consent(session, resident.max_user_id, text_sha256="a" * 64,
                            consent_text="Первая редакция. Ссылка: первая", prompt_mid="prompt-1")
    await session.refresh(resident)
    assert len(resident.consent_records) == 1
    assert resident.consent_records[0]["text"] == "Первая редакция. Ссылка: первая"
    assert datetime.fromisoformat(resident.consent_records[0]["accepted_at"]).tzinfo is not None

    original_time = resident.consent_pdn_at
    await users.set_consent(session, resident.max_user_id, text_sha256="a" * 64,
                            consent_text="Первая редакция. Ссылка: первая", prompt_mid="prompt-1")
    await session.refresh(resident)
    assert resident.consent_pdn_at == original_time

    resident.consent_pdn_at = None
    resident.consent_revoked_at = datetime.now(UTC)
    await session.flush()
    assert not await users.set_consent(session, resident.max_user_id, text_sha256="a" * 64,
                                       consent_text="Первая редакция. Ссылка: первая", prompt_mid="prompt-1")
    await session.refresh(resident)
    assert resident.consent_pdn_at is None
    assert resident.consent_revoked_at is not None
    await users.set_consent(session, resident.max_user_id, text_sha256="b" * 64,
                            consent_text="Вторая редакция", prompt_mid="prompt-2")
    await session.refresh(resident)
    assert [r["text"] for r in resident.consent_records] == [
        "Первая редакция. Ссылка: первая", "Вторая редакция",
    ]
