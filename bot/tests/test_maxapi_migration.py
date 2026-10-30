"""Exercise aemr adapters against the pinned SDK over a real local HTTP transport."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp import TCPConnector, web
from maxapi import Bot, Dispatcher
from maxapi.client.default import DefaultConnectionProperties
from maxapi.methods.types.getted_updates import process_update_request

from aemr_bot import logging_setup, main
from aemr_bot.config import Settings


def message(text="synthetic text", mid="migration-message"):
    return {
        "recipient": {"chat_id": 98765001, "chat_type": "dialog"},
        "sender": {"user_id": 98765002, "first_name": "Synthetic", "is_bot": False},
        "body": {"mid": mid, "seq": 1, "text": text},
        "timestamp": 1700000000000,
    }


@pytest_asyncio.fixture
async def transport(monkeypatch):
    requests = []
    state = {"updates": [], "marker": 101}
    base = ""

    async def serve(request):
        body = await request.read()
        payload = await request.json() if body and request.content_type == "application/json" else None
        requests.append((request.method, request.path, dict(request.query), payload, body))
        path = request.path.removeprefix("/gateway")
        if path == "/me/commands":
            return web.json_response({"commands": payload["commands"]})
        if path == "/me" and state.get("unauthorized"):
            return web.json_response({"code": "verify.token", "message": "Invalid token"}, status=401)
        if path == "/me":
            return web.json_response({"user_id": 98765000, "first_name": "Test bot", "is_bot": True})
        if path == "/subscriptions":
            return web.json_response({"subscriptions": []})
        if path == "/updates":
            if state.get("hold"):
                state["entered"].set()
                await state["release"].wait()
            return web.json_response({"updates": state["updates"], "marker": state["marker"]})
        if path == "/uploads":
            return web.json_response({"url": base + "/binary"})
        if path == "/binary":
            return web.json_response({"token": "synthetic-file-token"})
        if path == "/messages" and request.method == "POST":
            return web.json_response({"message": message(payload.get("text", ""))})
        if path in {"/messages", "/answers"}:
            return web.json_response({"success": True})
        raise web.HTTPNotFound()

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", serve)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}/gateway"
    # A loopback transport should not depend on the runner's corporate proxy.
    monkeypatch.setattr(main.network, "session_kwargs", dict)
    monkeypatch.setattr(main.settings, "bot_mode", "polling")
    bot = main.build_bot()
    bot.set_api_url(base)
    bot.auto_requests = False
    try:
        yield bot, requests, state
    finally:
        await bot.close_session()
        await runner.cleanup()


async def test_command_menu_uses_supported_sdk_endpoint_and_retains_commands(transport):
    bot, requests, _ = transport
    await main._register_bot_commands(bot)
    commands = [r for r in requests if r[0] == "PATCH" and r[1] == "/gateway/me/commands"]
    assert len(commands) == 1
    assert [c["name"] for c in commands[0][3]["commands"]] == ["start", "menu"]
    assert all(c["description"] for c in commands[0][3]["commands"])


async def test_sdk_update_diagnostics_do_not_persist_resident_payload(tmp_path):
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    dispatcher_log = logging.getLogger("dispatcher")
    saved_filters = list(dispatcher_log.filters)
    bot = Bot("synthetic-token", auto_requests=False)
    try:
        logging_setup.setup_logging(Settings(_env_file=None, LOG_DIR=str(tmp_path)))
        private = "PRIVATE-RESIDENT-TEXT-DO-NOT-LOG"
        await process_update_request({"updates": [
            private,
            {"update_type": "future_update", "text": private},
            {"update_type": "message_created", "message": private},
        ]}, bot)
        for handler in root.handlers:
            handler.flush()
        output = (tmp_path / "bot.log").read_text()
        assert private not in output
        assert "MAX" in output
        assert "WARNING" in output
    finally:
        await bot.close_session()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
        dispatcher_log.filters[:] = saved_filters


@pytest.mark.parametrize("parallel", [False, True])
async def test_wrapped_polling_keeps_timeout_marker_and_valid_events(transport, monkeypatch, parallel):
    # SQL confirmation invalidation has real MessageCreated coverage with PostgreSQL.
    monkeypatch.setattr("aemr_bot.handlers.menu.invalidate_privacy_confirmation", AsyncMock())
    bot, requests, state = transport
    state["updates"] = [None, {"update_type": "message_created", "timestamp": 1700000000000,
                               "message": message()}]
    # The product wrapper must preserve the real SDK's transport parameters.
    batch = await bot.get_updates(marker=99)
    poll = next(r for r in requests if r[1] == "/gateway/updates")
    assert poll[2]["marker"] == "99"
    assert int(poll[2]["timeout"]) == main.settings.polling_timeout_seconds
    seen = []
    dp = Dispatcher(use_create_task=parallel)
    dp.bot = bot
    monkeypatch.setattr(main, "_POLLING_DISPATCH_SEMAPHORE", None)

    @dp.message_created()
    async def record(event):
        seen.append(event.message.body.text)

    main._install_dispatch_guards(dp)
    await dp.startup(bot)
    try:
        await dp._dispatch_fetched_events(batch, 0, skip_updates=False)
        # Production dispatch is concurrent; shutdown must drain queued handlers.
        await dp.shutdown()
        assert seen == ["synthetic text"]
        assert bot.marker_updates == 101
    finally:
        await dp.shutdown()


async def test_existing_message_edit_callback_and_file_adapters(transport, monkeypatch):
    # Preserve the installed edit hook while isolating its external inventory query.
    @asynccontextmanager
    async def edit_inventory():
        yield SimpleNamespace(bind=SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
                              scalar=AsyncMock(return_value=None))

    monkeypatch.setattr("aemr_bot.services.data_erasure.session_scope", edit_inventory)

    from aemr_bot.services.uploads import file_attachment, upload_bytes
    from aemr_bot.utils.event import ack_callback, get_ids

    bot, requests, _ = transport
    sent = await bot.send_message(chat_id=98765001, text="operator reply")
    assert sent.message.body.text == "operator reply"
    edited = await bot.edit_message(message_id=sent.message.body.mid, text="edited reply")
    assert edited.success
    token = await upload_bytes(bot, b"synthetic-unknown-format-file-data")
    assert token == "synthetic-file-token"
    await bot.send_message(chat_id=98765001, attachments=[file_attachment(token)])
    assert any(r[1] == "/gateway/binary" and b"synthetic-unknown-format-file-data" in r[4]
               for r in requests)
    attachment_request = [r for r in requests if r[0] == "POST" and r[1] == "/gateway/messages"][-1]
    assert attachment_request[3]["attachments"][0]["payload"]["token"] == token

    [callback] = await process_update_request({"updates": [{
        "update_type": "message_callback", "timestamp": 1700000000000,
        "callback": {"timestamp": 1700000000000, "callback_id": "synthetic-callback",
                     "payload": "menu", "user": {"user_id": 98765002,
                     "first_name": "Synthetic", "is_bot": False}},
    }]}, bot)
    assert get_ids(callback) == (None, 98765002)
    await ack_callback(callback)
    assert any(r[1] == "/gateway/answers" for r in requests)


async def test_factory_session_recreation_retains_proxy_and_timeout(monkeypatch):
    import ssl

    monkeypatch.setattr(main.network, "session_kwargs", lambda: {"trust_env": True})
    bot = main.build_bot()
    try:
        first = await bot.ensure_session()
        connector = first.connector
        assert first.trust_env
        assert connector._ssl.verify_mode == ssl.CERT_REQUIRED
        ca_names = {value for cert in connector._ssl.get_ca_certs()
                    for rdn in cert["subject"] for key, value in rdn if key == "commonName"}
        assert "Russian Trusted Root CA" in ca_names
        assert first.timeout.total > main.settings.polling_timeout_seconds
        await bot.close_session()
        assert first.closed and connector.closed
        second = await bot.ensure_session()
        assert second is not first
        assert second.trust_env
        assert second.timeout == first.timeout
    finally:
        await bot.close_session()


async def test_custom_connector_remains_owned_by_caller():
    connector = TCPConnector()
    bot = Bot("synthetic-token", default_connection=DefaultConnectionProperties(connector=connector))
    try:
        first = await bot.ensure_session()
        await bot.close_session()
        assert not connector.closed
        second = await bot.ensure_session()
        assert second.connector is connector
        assert second is not first
    finally:
        await bot.close_session()
        await connector.close()


async def test_stop_polling_cancels_inflight_wrapped_request(transport, monkeypatch):
    bot, _, state = transport
    state.update(hold=True, entered=asyncio.Event(), release=asyncio.Event())
    dp = Dispatcher(use_create_task=True)
    main._install_dispatch_guards(dp)
    monkeypatch.setattr(main, "_POLLING_DISPATCH_SEMAPHORE", None)
    task = asyncio.create_task(dp.start_polling(bot))
    try:
        await asyncio.wait_for(state["entered"].wait(), timeout=3)
        await asyncio.wait_for(dp.stop_polling(), timeout=2)
        await asyncio.wait_for(task, timeout=2)
        assert not dp.polling
    finally:
        state["release"].set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await dp.shutdown()


async def test_invalid_token_reaches_product_preflight_without_retry(transport):
    bot, requests, state = transport
    state["unauthorized"] = True
    with pytest.raises(SystemExit) as exc:
        await main._preflight_check_token(bot)
    assert exc.value.code == 1
    assert len([r for r in requests if r[1] == "/gateway/me"]) == 1
