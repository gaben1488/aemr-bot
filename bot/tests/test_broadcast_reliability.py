"""Regression tests for F04/F07: accounting must precede external effects."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from maxapi.exceptions import MaxApiError

from aemr_bot.handlers import broadcast as bc


@asynccontextmanager
async def scope():
    yield SimpleNamespace()


@pytest.fixture
def pipeline(monkeypatch):
    monkeypatch.setattr(bc, "session_scope", scope)
    monkeypatch.setattr(bc.broadcasts_service, "get_status", AsyncMock(return_value="sending"))
    monkeypatch.setattr(bc.broadcasts_service, "update_progress", AsyncMock())
    monkeypatch.setattr(bc.broadcasts_service, "record_deliveries", AsyncMock())
    monkeypatch.setattr(bc.broadcasts_service, "begin_delivery", AsyncMock(return_value=True))
    monkeypatch.setattr(bc.broadcasts_service, "discard_delivery_intent", AsyncMock())
    return SimpleNamespace(send_message=AsyncMock())


async def run_loop(bot, targets=None):
    return await bc._run_send_loop(
        bot, broadcast_id=7, body="message", total=2,
        targets=targets or [(1, 101), (2, 102)], admin_mid=None,
        rate_delay=0, progress_step_sec=999,
    )


async def test_existing_cancel_prevents_first_external_send(pipeline, monkeypatch):
    monkeypatch.setattr(bc.broadcasts_service, "get_status", AsyncMock(return_value="cancelled"))
    assert await run_loop(pipeline) == (0, 0, True)
    pipeline.send_message.assert_not_awaited()


async def test_journal_failure_aborts_before_next_recipient(pipeline, monkeypatch):
    monkeypatch.setattr(
        bc.broadcasts_service, "record_deliveries", AsyncMock(side_effect=RuntimeError("DB down"))
    )
    with pytest.raises(RuntimeError, match="DB down"):
        await run_loop(pipeline)
    assert pipeline.send_message.await_count == 1


async def test_cancel_during_backoff_prevents_retry(pipeline, monkeypatch):
    cancelled = False

    async def status(*args):
        return "cancelled" if cancelled else "sending"

    async def wait(_delay):
        nonlocal cancelled
        cancelled = True

    pipeline.send_message.side_effect = MaxApiError(code=429, raw="Too Many Requests")
    monkeypatch.setattr(bc.broadcasts_service, "get_status", status)
    monkeypatch.setattr(bc.asyncio, "sleep", wait)
    assert await run_loop(pipeline) == (0, 0, True)
    assert pipeline.send_message.await_count == 1


async def test_journal_failure_never_finishes_done(pipeline, monkeypatch):
    monkeypatch.setattr(bc, "_resolve_admin_progress_message", AsyncMock(return_value=None))
    monkeypatch.setattr(bc, "_send_final_summary", AsyncMock())
    monkeypatch.setattr(bc.broadcasts_service, "mark_started", AsyncMock())
    monkeypatch.setattr(bc.broadcasts_service, "list_subscriber_targets", AsyncMock(return_value=[(1, 101)]))
    monkeypatch.setattr(bc.broadcasts_service, "get_by_id", AsyncMock(return_value=SimpleNamespace(attachments=[])))
    monkeypatch.setattr(bc.broadcasts_service, "record_deliveries", AsyncMock(side_effect=RuntimeError("DB down")))
    finished = AsyncMock()
    monkeypatch.setattr(bc.broadcasts_service, "mark_finished", finished)
    await bc._run_broadcast(pipeline, 7, "message", 1)
    assert finished.await_args.kwargs["status"].value == "failed"
    bc._send_final_summary.assert_not_awaited()


async def test_delivery_intent_commits_before_external_send(pipeline, monkeypatch):
    durable = set()
    staged = set()

    @asynccontextmanager
    async def transaction():
        yield SimpleNamespace()
        durable.update(staged)
        staged.clear()

    async def begin(session, *, broadcast_id, user_id):
        staged.add(user_id)
        return True

    async def send(**kwargs):
        assert kwargs["user_id"] - 100 in durable
        return SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid="m")))

    monkeypatch.setattr(bc, "session_scope", transaction)
    monkeypatch.setattr(bc.broadcasts_service, "begin_delivery", begin, raising=False)
    pipeline.send_message.side_effect = send
    assert await run_loop(pipeline) == (2, 0, False)


async def test_existing_intent_is_never_replayed(pipeline, monkeypatch):
    monkeypatch.setattr(
        bc.broadcasts_service, "begin_delivery", AsyncMock(return_value=False), raising=False
    )
    with pytest.raises(RuntimeError, match="already|существует"):
        await run_loop(pipeline)
    pipeline.send_message.assert_not_awaited()


async def test_unknown_network_outcome_is_recorded_and_aborts(pipeline):
    pipeline.send_message.side_effect = TimeoutError("response lost after send")
    with pytest.raises(RuntimeError, match="unknown|неизвест"):
        await run_loop(pipeline)
    assert pipeline.send_message.await_count == 1
    results = bc.broadcasts_service.record_deliveries.await_args.kwargs["results"]
    assert "UNKNOWN" in results[0][1]


async def test_intent_commit_failure_prevents_external_send(pipeline, monkeypatch):
    @asynccontextmanager
    async def broken_commit():
        yield SimpleNamespace()
        raise RuntimeError("commit response lost")

    monkeypatch.setattr(bc, "session_scope", broken_commit)
    with pytest.raises(RuntimeError, match="commit response lost"):
        await run_loop(pipeline)
    pipeline.send_message.assert_not_awaited()


async def test_explicit_api_rejection_is_known_failure_not_unknown(pipeline):
    from maxapi.exceptions import MaxApiError

    pipeline.send_message.side_effect = MaxApiError(code=403, raw={"code": "user.blocked"})
    result = await bc._send_one(pipeline, 101, "message")
    assert "UNKNOWN" not in result
    assert "403" in result


async def test_unrelated_429_in_error_payload_does_not_trigger_replay(pipeline):
    from maxapi.exceptions import MaxApiError

    pipeline.send_message.side_effect = MaxApiError(code=503, raw="request id 429001")
    result = await bc._send_one(pipeline, 101, "message")
    assert "UNKNOWN" in result
    assert pipeline.send_message.await_count == 1


async def test_unknown_error_with_rate_limit_text_is_not_replayed(pipeline):
    pipeline.send_message.side_effect = TimeoutError("rate limit response lost; request 429")
    result = await bc._send_one(pipeline, 101, "message")
    assert "UNKNOWN" in result
    assert pipeline.send_message.await_count == 1


async def test_missing_broadcast_status_prevents_send(pipeline, monkeypatch):
    monkeypatch.setattr(bc.broadcasts_service, "get_status", AsyncMock(return_value=None))
    with pytest.raises(RuntimeError, match="status"):
        await run_loop(pipeline)
    pipeline.send_message.assert_not_awaited()


async def test_api_none_response_cannot_count_as_confirmed_delivery(pipeline):
    pipeline.send_message.return_value = None
    result = await bc._send_one(pipeline, 101, "message")
    assert result is not None and "UNKNOWN" in result


async def test_failed_list_explains_unknown_delivery_may_have_arrived(pipeline, monkeypatch):
    monkeypatch.setattr(bc, "_ensure_role", AsyncMock(return_value=True))
    monkeypatch.setattr(bc.broadcasts_service, "get_by_id", AsyncMock(return_value=SimpleNamespace(id=7, failed_count=1)))
    monkeypatch.setattr(bc.broadcasts_service, "list_failed_deliveries", AsyncMock(return_value=[
        (1, "Resident", "DELIVERY_UNKNOWN: TimeoutError('response lost')"),
    ]))
    screen = AsyncMock()
    monkeypatch.setattr(bc, "op_screen", screen)
    await bc._list_failed_deliveries(SimpleNamespace(), 7)
    text = screen.await_args.args[1]
    assert "могло дойти" in text
    assert "перед повторной отправкой" in text


async def test_failed_card_does_not_claim_unknown_delivery_was_not_delivered(pipeline, monkeypatch):
    monkeypatch.setattr(bc, "_ensure_role", AsyncMock(return_value=True))
    row = SimpleNamespace(
        id=7, status="failed", failed_count=1, delivered_count=0,
        subscriber_count_at_start=2, created_at=datetime.now(UTC), attachments=[], text="message",
    )
    monkeypatch.setattr(bc.broadcasts_service, "get_by_id", AsyncMock(return_value=row))
    screen = AsyncMock()
    monkeypatch.setattr(bc, "op_screen", screen)
    await bc._open_broadcast(SimpleNamespace(), 7)
    text = screen.await_args.args[1]
    assert "неподтверждённые" in text
    assert "Не доставлено: 1" not in text
