import pytest

from aemr_bot.services import alert_state


@pytest.mark.asyncio
async def test_uncertain_alert_is_not_replayed_until_incident_recovers(session):
    assert await alert_state.claim(session, "threat_intel")
    await alert_state.finish(session, "threat_intel", confirmed=False)
    assert not await alert_state.claim(session, "threat_intel")
    assert await alert_state.get(session, "threat_intel") == "unknown"
    await alert_state.clear(session, "threat_intel")
    assert await alert_state.claim(session, "threat_intel")


@pytest.mark.asyncio
async def test_reminder_snapshot_is_claimed_only_when_it_changes(session):
    assert await alert_state.claim_snapshot(session, "queue", "day-one-same-list")
    assert not await alert_state.claim_snapshot(session, "queue", "day-one-same-list")
    assert await alert_state.claim_snapshot(session, "queue", "day-one-new-list")
    assert await alert_state.claim_snapshot(session, "queue", "day-two-new-list")
