"""Real SQL persistence and real SDK dispatch against synthetic events."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from maxapi import Bot, Dispatcher
from maxapi.types.users import User
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from aemr_bot.db.models import Event, InboxEvent
from aemr_bot.handlers import IdempotencyMiddleware
from aemr_bot.services import idempotency, inbox


@compiles(JSONB, 'sqlite')
def _jsonb_sqlite(type_, compiler, **kw):
    return 'JSON'


@pytest.fixture
async def receipt_db(monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: InboxEvent.__table__.create(c))
        await conn.run_sync(lambda c: Event.__table__.create(c))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    @asynccontextmanager
    async def scope():
        async with factory.begin() as session:
            yield session
    monkeypatch.setattr(inbox, 'session_scope', scope)
    monkeypatch.setattr(idempotency, 'session_scope', scope)
    yield scope
    await engine.dispose()


def payload(mid='synthetic-inbox'):
    return {'update_type': 'message_created', 'timestamp': 1700000000000,
            'message': {'timestamp': 1700000000000,
                'recipient': {'chat_id': 91001, 'chat_type': 'dialog'},
                'sender': {'user_id': 91002, 'first_name': 'Synthetic', 'is_bot': False},
                'body': {'mid': mid, 'seq': 1, 'text': 'sensitive synthetic text'}}}


async def stored(scope):
    async with scope() as session:
        return (await session.scalars(select(InboxEvent).order_by(InboxEvent.id))).all()


@pytest.mark.asyncio
async def test_duplicate_receipt_and_restart_preserve_pending_work(receipt_db):
    await inbox.accept([payload(), payload()])
    await inbox.recover_interrupted()
    rows = await stored(receipt_db)
    assert len(rows) == 1
    assert rows[0].state == 'pending'
    assert rows[0].payload['message']['body']['text'] == 'sensitive synthetic text'


@pytest.mark.asyncio
async def test_interrupted_execution_is_visible_not_replayed(receipt_db):
    await inbox.accept([payload()])
    item = await inbox.take_next()
    assert item
    await inbox.recover_interrupted()
    assert await inbox.take_next() is None
    row = (await stored(receipt_db))[0]
    assert row.state == 'failed'
    assert row.reason == 'interrupted_unknown_result'
    assert row.payload == {}


@pytest.mark.asyncio
async def test_erasure_clears_pending_payload_and_actor(receipt_db):
    await inbox.accept([payload()])
    async with receipt_db() as session:
        await inbox.erase_pending(session, 91002)
    row = (await stored(receipt_db))[0]
    assert row.state == 'done'
    assert row.max_user_id is None
    assert row.payload == {}
    assert await inbox.take_next() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_handler', [False, True])
async def test_real_sdk_handler_outcome_is_durable(receipt_db, fail_handler):
    bot = Bot('synthetic-token', auto_requests=False, auto_check_subscriptions=False)
    bot.get_me = AsyncMock(return_value=User(user_id=1, first_name='Audit', is_bot=True))
    dp = Dispatcher()
    dp.register_outer_middleware(IdempotencyMiddleware())
    seen = []
    @dp.message_created()
    async def handler(event):
        seen.append(event.message.body.text)
        if fail_handler:
            raise RuntimeError('synthetic failure after possible side effect')
    await dp.startup(bot)
    try:
        await inbox.accept([payload()])
        assert await inbox.process_one(dp, bot)
        assert seen == ['sensitive synthetic text']
        row = (await stored(receipt_db))[0]
        assert row.state == ('failed' if fail_handler else 'done')
        assert row.payload == {}
        assert not await inbox.process_one(dp, bot)
    finally:
        await dp.shutdown()
        await bot.close_session()


@pytest.mark.asyncio
async def test_pre_handler_db_failure_retries_without_claiming_key(receipt_db, monkeypatch):
    bot = Bot('synthetic-token', auto_requests=False, auto_check_subscriptions=False)
    bot.get_me = AsyncMock(return_value=User(user_id=1, first_name='Audit', is_bot=True))
    dp = Dispatcher()
    dp.register_outer_middleware(IdempotencyMiddleware())
    seen = []
    @dp.message_created()
    async def handler(event):
        seen.append(event.message.body.mid)
    await dp.startup(bot)
    original = idempotency.is_legacy_duplicate
    try:
        await inbox.accept([payload()])
        monkeypatch.setattr(idempotency, 'is_legacy_duplicate', AsyncMock(side_effect=RuntimeError('DB outage')))
        await inbox.process_one(dp, bot)
        assert seen == []
        assert (await stored(receipt_db))[0].state == 'pending'
        monkeypatch.setattr(idempotency, 'is_legacy_duplicate', original)
        await inbox.process_one(dp, bot)
        assert seen == ['synthetic-inbox']
        assert (await stored(receipt_db))[0].state == 'done'
    finally:
        await dp.shutdown()
        await bot.close_session()


@pytest.mark.asyncio
async def test_failed_completion_commit_is_quarantined_without_replay(receipt_db, monkeypatch):
    bot = Bot('synthetic-token', auto_requests=False, auto_check_subscriptions=False)
    bot.get_me = AsyncMock(return_value=User(user_id=1, first_name='Audit', is_bot=True))
    dp = Dispatcher()
    dp.register_outer_middleware(IdempotencyMiddleware())
    seen = []
    @dp.message_created()
    async def handler(event):
        seen.append(event.message.body.mid)
    await dp.startup(bot)
    try:
        await inbox.accept([payload()])
        monkeypatch.setattr(inbox, 'finish', AsyncMock(side_effect=RuntimeError('lost commit')))
        with pytest.raises(RuntimeError, match='lost commit'):
            await inbox.process_one(dp, bot)
        await inbox.recover_interrupted()
        assert not await inbox.process_one(dp, bot)
        assert seen == ['synthetic-inbox']
        row = (await stored(receipt_db))[0]
        assert row.state == 'failed'
        assert row.payload == {}
    finally:
        await dp.shutdown()
        await bot.close_session()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, "synthetic-mid"])
async def test_failure_notice_is_claimed_once_even_when_delivery_is_unknown(
    receipt_db, monkeypatch, caplog, result,
):
    from aemr_bot.services import admin_bus
    await inbox.accept([payload()])
    await inbox.take_next()
    await inbox.recover_interrupted()
    send = AsyncMock(return_value=result)
    monkeypatch.setattr(admin_bus, 'send', send)
    await inbox.maintenance(object())
    row = (await stored(receipt_db))[0]
    # notified is a durable attempt claim, not a MAX delivery receipt.
    assert row.notified
    assert row.state == "failed"
    assert row.reason == "interrupted_unknown_result"
    assert row.payload == {}
    text = send.call_args.kwargs["text"]
    assert f"#{row.id}" in text
    assert "sensitive synthetic text" not in text
    assert send.call_args.kwargs["critical"] is True
    if result is None:
        assert "delivery of failed-event alert was not confirmed" in caplog.text
    await inbox.recover_interrupted()
    await inbox.maintenance(object())
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_batch_receipt_rolls_back_on_second_insert_failure(receipt_db, monkeypatch):
    original = inbox.session_scope
    @asynccontextmanager
    async def failing_scope():
        async with original() as session:
            execute = session.execute
            count = 0
            async def failing_execute(*args, **kwargs):
                nonlocal count
                count += 1
                if count == 2:
                    raise RuntimeError('second insert failure')
                return await execute(*args, **kwargs)
            session.execute = failing_execute
            yield session
    monkeypatch.setattr(inbox, 'session_scope', failing_scope)
    with pytest.raises(RuntimeError, match='second insert'):
        await inbox.accept([payload(str(i)) for i in range(inbox._RECEIPT_BATCH_SIZE + 1)])
    assert await stored(receipt_db) == []


@pytest.mark.asyncio
async def test_duplicate_does_not_depend_on_sender_display_name(receipt_db):
    first = payload()
    second = payload()
    second['message']['sender']['first_name'] = 'Renamed'
    await inbox.accept([first, second])
    assert len(await stored(receipt_db)) == 1


@pytest.mark.asyncio
async def test_malformed_shape_is_retained_until_classified(receipt_db):
    await inbox.accept([{'update_type': 'message_created', 'message': 'invalid-shape'}])
    row = (await stored(receipt_db))[0]
    assert row.state == 'pending'
    assert row.max_user_id is None


@pytest.mark.asyncio
async def test_erased_claimed_event_never_reaches_business_handler(receipt_db, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    claimed, resume = asyncio.Event(), asyncio.Event()
    take = inbox.take_next
    async def delayed_take():
        result = await take()
        claimed.set()
        await resume.wait()
        return result
    monkeypatch.setattr(inbox, 'take_next', delayed_take)
    handle = AsyncMock()
    bot = Bot('synthetic-token', auto_requests=False, auto_check_subscriptions=False)
    await inbox.accept([payload()])
    task = asyncio.create_task(inbox.process_one(SimpleNamespace(handle=handle), bot))
    try:
        await claimed.wait()
        async with receipt_db() as session:
            await inbox.erase_pending(session, 91002)
        resume.set()
        await task
        handle.assert_not_awaited()
    finally:
        resume.set()
        await asyncio.gather(task, return_exceptions=True)
        await bot.close_session()


@pytest.mark.asyncio
async def test_bulk_receipts_preserve_order_dedup_and_bound_sql(receipt_db, monkeypatch):
    original = inbox.session_scope
    counts = []
    @asynccontextmanager
    async def counted_scope():
        async with original() as session:
            execute = session.execute
            async def counted(*args, **kwargs):
                counts.append(1)
                return await execute(*args, **kwargs)
            session.execute = counted
            yield session
    monkeypatch.setattr(inbox, "session_scope", counted_scope)
    await inbox.accept([payload(str(i)) for i in range(501)] + [payload("0")])
    assert len(counts) == 3
    rows = await stored(receipt_db)
    assert [r.payload["message"]["body"]["mid"] for r in rows] == [str(i) for i in range(501)]


def test_callback_receipt_never_attributes_actor_to_message_author():
    _, user_id = inbox._identity({"update_type": "message_callback", "callback": {"callback_id": "c"},
                                "message": {"sender": {"user_id": 123}}})
    assert user_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_once", [False, True])
async def test_worker_recovers_only_at_startup_and_after_failure(monkeypatch, fail_once):
    import asyncio
    stop = asyncio.Event()
    attempts = 0
    async def process(*args):
        nonlocal attempts
        attempts += 1
        if fail_once and attempts == 1:
            raise RuntimeError("outcome commit failed")
        if attempts == 100:
            stop.set()
        return True
    async def no_delay(waiter, **kwargs):
        waiter.close()
        raise TimeoutError
    recover = AsyncMock()
    monkeypatch.setattr(inbox, "recover_interrupted", recover)
    monkeypatch.setattr(inbox, "maintenance", AsyncMock())
    monkeypatch.setattr(inbox, "process_one", process)
    monkeypatch.setattr(inbox.asyncio, "wait_for", no_delay)
    await inbox.run(None, None, stop)
    assert recover.await_count == (2 if fail_once else 1)
