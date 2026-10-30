"""Durable shared queue: only confirmed identities may be edited/reused."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from aemr_bot.db.models import Setting
from aemr_bot.db.session import session_scope


def panel_module():
    from aemr_bot.services import queue_panel
    return queue_panel


@pytest.mark.asyncio
async def test_one_panel_survives_changes_restart_and_empty_queue(session):
    panel = panel_module()
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id="panel-1")),
                          edit_message=AsyncMock())
    with patch.object(panel, "_snapshot", AsyncMock(return_value=("queue A", True))):
        await panel.refresh(bot)
        await panel.refresh(bot)
    with patch.object(panel, "_snapshot", AsyncMock(return_value=("queue B", True))):
        await panel.refresh(bot)
    with patch.object(panel, "_snapshot", AsyncMock(return_value=("empty", False))):
        await panel.refresh(bot)
        await panel.refresh(bot)
    assert bot.send_message.await_count == 1
    assert [c.kwargs["text"] for c in bot.edit_message.await_args_list] == ["queue B", "empty"]
    assert all(c.kwargs["message_id"] == "panel-1" for c in bot.edit_message.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, TimeoutError("MAX accepted? unknown")])
async def test_unknown_initial_send_is_never_retried_even_after_change(session, result):
    panel = panel_module()
    send = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
    bot = SimpleNamespace(send_message=send, edit_message=AsyncMock())
    with patch.object(panel, "_snapshot", AsyncMock(return_value=("queue A", True))):
        await panel.refresh(bot)
    with patch.object(panel, "_snapshot", AsyncMock(return_value=("queue B", True))):
        await panel.refresh(bot)
    assert send.await_count == 1
    bot.edit_message.assert_not_awaited()
    async with session_scope() as check:
        values = (await check.scalars(select(Setting.value))).all()
    assert any(isinstance(v, dict) and v.get("state") == "unknown" for v in values)


@pytest.mark.asyncio
async def test_concurrent_refresh_claims_one_initial_send(session):
    panel = panel_module()
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id="panel-1")),
                          edit_message=AsyncMock())
    with patch.object(panel, "_snapshot", AsyncMock(return_value=("queue A", True))):
        await asyncio.gather(*(panel.refresh(bot) for _ in range(5)))
    assert bot.send_message.await_count == 1
    bot.edit_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_queue_does_not_create_panel(session):
    panel = panel_module()
    bot = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock())
    with patch.object(panel, "_snapshot", AsyncMock(return_value=("empty", False))):
        await panel.refresh(bot)
    bot.send_message.assert_not_awaited()
    bot.edit_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_both_cron_jobs_refresh_the_same_panel_and_respect_quiet():
    from aemr_bot.services import cron
    panel = panel_module()
    with (patch.object(panel, "refresh", AsyncMock()) as refresh,
          patch("aemr_bot.services.quiet_hours.is_quiet_hours_now", return_value=False),
          patch("aemr_bot.services.notify_toggles.is_enabled", return_value=True),
          patch.object(cron.sla_service, "is_working_time", return_value=True)):
        bot = SimpleNamespace()
        await cron._job_working_hours_open_reminder(bot)
        await cron._job_working_hours_overdue_reminder(bot)
        assert refresh.await_count == 2
        with patch("aemr_bot.services.quiet_hours.is_quiet_hours_now", return_value=True):
            await cron._job_working_hours_open_reminder(bot)
            await cron._job_working_hours_overdue_reminder(bot)
        assert refresh.await_count == 2


@pytest.mark.asyncio
async def test_crash_after_initial_claim_never_creates_replacement(session):
    panel = panel_module()
    session.add(Setting(key=f"__queue_panel:{panel.settings.admin_group_id}", value={"state": "attempting"}))
    await session.commit()
    bot = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock())
    await panel.refresh(bot)
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejected_edit_retries_only_confirmed_identity(session):
    panel = panel_module()
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id="panel-1")),
                          edit_message=AsyncMock(return_value={"success": False}))
    with patch.object(panel, "_snapshot", AsyncMock(return_value=("queue A", True))):
        await panel.refresh(bot)
    with patch.object(panel, "_snapshot", AsyncMock(return_value=("queue B", True))):
        await panel.refresh(bot)
        bot.edit_message.return_value = {"success": True}
        await panel.refresh(bot)
        await panel.refresh(bot)
    assert bot.send_message.await_count == 1
    assert bot.edit_message.await_count == 2
    assert all(c.kwargs["message_id"] == "panel-1" for c in bot.edit_message.await_args_list)


@pytest.mark.asyncio
async def test_shared_panel_callback_opens_separate_view():
    from aemr_bot.handlers import admin_callback_dispatch as dispatch
    from aemr_bot.handlers import admin_commands
    event = SimpleNamespace(ack=AsyncMock())
    with patch.object(admin_commands, "run_open_tickets", AsyncMock()) as listing:
        await dispatch._EXACT["op:queue_view"](event)
    listing.assert_awaited_once_with(event, new=True)


@pytest.mark.asyncio
async def test_recovery_requires_current_version_and_consumes_it_once(session):
    panel = panel_module()
    session.add(Setting(key=f"__queue_panel:{panel.settings.admin_group_id}",
                        value={"state": "unknown", "token": "current"}))
    await session.commit()
    assert not await panel.allow_recreate("stale")
    assert await panel.allow_recreate("current")
    assert not await panel.allow_recreate("current")


@pytest.mark.asyncio
async def test_queue_recovery_refuses_non_it():
    from aemr_bot.handlers import admin_callback_dispatch as dispatch
    from aemr_bot.services import queue_panel
    event = SimpleNamespace(ack=AsyncMock())
    with (patch.object(dispatch, "get_operator", AsyncMock(return_value=SimpleNamespace(role="operator"))),
          patch.object(queue_panel, "allow_recreate", AsyncMock()) as reset):
        await dispatch.dispatch_admin_callback(event, "op:queue_recreate:current")
    reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_recovery_it_with_stale_token_does_not_send():
    from aemr_bot.db.models import OperatorRole
    from aemr_bot.handlers import admin_callback_dispatch as dispatch
    from aemr_bot.services import queue_panel
    event = SimpleNamespace(ack=AsyncMock())
    with (patch.object(dispatch, "get_operator", AsyncMock(return_value=SimpleNamespace(role=OperatorRole.IT.value))),
          patch.object(queue_panel, "allow_recreate", AsyncMock(return_value=False)),
          patch.object(queue_panel, "refresh", AsyncMock()) as refresh):
        await dispatch.dispatch_admin_callback(event, "op:queue_recreate:stale")
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_initial_attempt_cannot_be_reset(session):
    panel = panel_module()
    session.add(Setting(key=f"__queue_panel:{panel.settings.admin_group_id}",
                        value={"state": "attempting", "token": "live"}))
    await session.commit()
    status = await panel.recovery_status()
    assert status["state"] == "attempting"
    assert not status["recoverable"]
    assert not await panel.allow_recreate("live")


@pytest.mark.asyncio
async def test_uncertain_legacy_panel_has_visible_recovery_version(session):
    panel = panel_module()
    session.add(Setting(key=f"__queue_panel:{panel.settings.admin_group_id}", value={"state": "unknown"}))
    await session.commit()
    status = await panel.recovery_status()
    assert status["recoverable"]
    assert status["token"]
    assert (await panel.recovery_status())["token"] == status["token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError("edit outcome unknown"), ValueError("deleted message")])
async def test_failed_confirmed_edit_is_visible_and_manual_recreation_is_versioned(session, failure):
    panel = panel_module()
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id="old-panel")),
                          edit_message=AsyncMock(side_effect=failure))
    with patch.object(panel, "_snapshot", AsyncMock(return_value=("queue A", True))):
        await panel.refresh(bot)
    with patch.object(panel, "_snapshot", AsyncMock(return_value=("queue B", True))):
        await panel.refresh(bot)
        first = await panel.recovery_status()
        assert first["edit_unknown"]
        assert first["message_id"] == "old-panel"
        assert first["recoverable"]
        await panel.refresh(bot)
        assert bot.send_message.await_count == 1
        assert (await panel.recovery_status())["token"] == first["token"]
        assert not await panel.allow_recreate("stale")
        results = await asyncio.gather(*(panel.allow_recreate(first["token"]) for _ in range(4)))
        assert results.count(True) == 1
        bot.send_message.return_value = SimpleNamespace(message_id="replacement")
        await panel.refresh(bot)
        assert not await panel.allow_recreate(first["token"])
        assert (await panel.recovery_status())["message_id"] == "replacement"
    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_successful_edit_invalidates_old_recreation_confirmation(session):
    panel = panel_module()
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id="panel")),
                          edit_message=AsyncMock(side_effect=TimeoutError()))
    with patch.object(panel, "_snapshot", AsyncMock(return_value=("queue A", True))):
        await panel.refresh(bot)
    with patch.object(panel, "_snapshot", AsyncMock(return_value=("queue B", True))):
        await panel.refresh(bot)
        token = (await panel.recovery_status())["token"]
        bot.edit_message.side_effect = None
        bot.edit_message.return_value = {"success": True}
        await panel.refresh(bot)
        assert not await panel.allow_recreate(token)
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_old_send_completion_cannot_overwrite_manually_recreated_panel(session):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    panel = panel_module()
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def slow_send(**kwargs):
        entered.set()
        await finish.wait()
        return SimpleNamespace(message_id="late-old-panel")

    old_bot = SimpleNamespace(send_message=slow_send)
    new_bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id="replacement")))
    with patch.object(panel, "_snapshot", AsyncMock(return_value=("queue A", True))):
        old = asyncio.create_task(panel.refresh(old_bot))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            await session.execute(update(Setting).where(
                Setting.key == f"__queue_panel:{panel.settings.admin_group_id}"
            ).values(updated_at=datetime.now(UTC) - timedelta(minutes=10)))
            await session.commit()
            token = (await panel.recovery_status())["token"]
            assert await panel.allow_recreate(token)
            await panel.refresh(new_bot)
        finally:
            finish.set()
            await old
    assert (await panel.recovery_status())["message_id"] == "replacement"
    assert not await panel.allow_recreate(token)


@pytest.mark.asyncio
async def test_uncertain_edit_converges_even_when_queue_returns_to_last_confirmed_text(session):
    panel = panel_module()
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id="panel")),
                          edit_message=AsyncMock(side_effect=TimeoutError()))
    with patch.object(panel, "_snapshot", AsyncMock(return_value=("queue A", True))):
        await panel.refresh(bot)
    with patch.object(panel, "_snapshot", AsyncMock(return_value=("queue B", True))):
        await panel.refresh(bot)
    with patch.object(panel, "_snapshot", AsyncMock(return_value=("queue A", True))):
        bot.edit_message.side_effect = None
        bot.edit_message.return_value = {"success": True}
        await panel.refresh(bot)
    assert bot.edit_message.await_count == 2
    assert not (await panel.recovery_status()).get("edit_unknown")
