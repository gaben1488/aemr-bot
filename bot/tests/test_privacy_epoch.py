import pytest

from aemr_bot.services import privacy_epoch


@pytest.mark.asyncio
async def test_export_snapshot_detects_a_later_erasure(session):
    initial = await privacy_epoch.read(session)
    await privacy_epoch.mark_erasure(session)
    assert await privacy_epoch.read(session) == initial + 1
    assert not await privacy_epoch.hold_export_if_current(session, initial)
