"""Durable receipt must precede transport acknowledgement (no live MAX)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def transport_module():
    from aemr_bot import transport
    return transport


@pytest.mark.asyncio
async def test_marker_moves_only_after_durable_acceptance(monkeypatch):
    transport = transport_module()
    dp = transport.DurableDispatcher()
    bot = SimpleNamespace(marker_updates=9)
    monkeypatch.setattr(dp, '_ensure_bot', lambda: bot)
    accepted = []

    async def accept(events):
        assert bot.marker_updates == 9
        accepted.extend(events)

    monkeypatch.setattr(transport.inbox, 'accept', accept)
    await dp._dispatch_fetched_events({'updates': [{'update_type': 'bot_started'}], 'marker': 10}, 0, skip_updates=False)
    assert accepted == [{'update_type': 'bot_started'}]
    assert bot.marker_updates == 10


@pytest.mark.asyncio
async def test_receipt_failure_preserves_marker(monkeypatch):
    transport = transport_module()
    dp = transport.DurableDispatcher()
    bot = SimpleNamespace(marker_updates=9)
    monkeypatch.setattr(dp, '_ensure_bot', lambda: bot)
    monkeypatch.setattr(transport.inbox, 'accept', AsyncMock(side_effect=RuntimeError('DB down')))
    monkeypatch.setattr(dp, '_sleep_unless_stopped', AsyncMock())
    await dp._dispatch_fetched_events({'updates': [{'update_type': 'bot_started'}], 'marker': 10}, 0, skip_updates=False)
    assert bot.marker_updates == 9


@pytest.mark.asyncio
async def test_webhook_requires_canonical_header_and_committed_receipt(monkeypatch):
    transport = transport_module()
    persisted = []
    async def accept(events):
        persisted.extend(events)
    monkeypatch.setattr(transport.inbox, 'accept', accept)
    app = web.Application()
    transport.setup_webhook(app, secret='synthetic-secret-value')
    async with TestClient(TestServer(app)) as client:
        payload = {'update_type': 'bot_started', 'timestamp': 1}
        response = await client.post('/max/webhook', json=payload, headers={'X-Max-Secret': 'synthetic-secret-value'})
        assert response.status == 403
        assert persisted == []
        response = await client.post('/max/webhook', json=payload, headers={'X-Max-Bot-Api-Secret': 'synthetic-secret-value'})
        assert response.status == 200
        assert persisted == [payload]
        monkeypatch.setattr(transport.inbox, 'accept', AsyncMock(side_effect=RuntimeError('DB down')))
        response = await client.post('/max/webhook', json=payload, headers={'X-Max-Bot-Api-Secret': 'synthetic-secret-value'})
        assert response.status == 503
        response = await client.post('/max/webhook', json=[], headers={'X-Max-Bot-Api-Secret': 'synthetic-secret-value'})
        assert response.status == 400
