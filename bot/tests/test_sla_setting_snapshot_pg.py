"""The current target must not retroactively rewrite prior appeal periods."""
import pytest

from aemr_bot.db.models import User
from aemr_bot.services import appeals, settings_store


@pytest.mark.asyncio
async def test_sla_setting_applies_to_new_periods_and_preserves_history(session):
    user = User(max_user_id=91020)
    session.add(user)
    await session.flush()
    await settings_store.set_value(session, "sla_response_hours", 2)
    first = await appeals.create_appeal(session, user, "Адрес", "Дорога", "Текст", [])
    assert first.sla_hours_at_start == 2
    await settings_store.set_value(session, "sla_response_hours", 8)
    second = await appeals.create_appeal(session, user, "Адрес", "Дорога", "Текст", [])
    assert first.sla_hours_at_start == 2
    assert second.sla_hours_at_start == 8
    assert await appeals.close(session, first.id)
    assert await appeals.reopen(session, first.id) == "reopened"
    await session.refresh(first)
    assert first.sla_history[-1]["sla_hours"] == 2
    assert first.sla_hours_at_start == 8
