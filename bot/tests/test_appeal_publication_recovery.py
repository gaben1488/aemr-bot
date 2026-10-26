"""Durable first-card recovery and per-user serialization regressions."""
from __future__ import annotations

import asyncio
import gc
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from aemr_bot.db.models import Appeal, Base, Message, Operator, User
from aemr_bot.handlers import appeal_runtime
from aemr_bot.services import admin_card


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(_type, _compiler, **_kw):
    return "JSON"


@pytest.mark.asyncio
async def test_user_lock_survives_release_with_waiter_and_drop():
    lock = appeal_runtime.get_user_lock(987654)
    await lock.acquire()
    entered = asyncio.Event()

    async def waiter():
        async with appeal_runtime.get_user_lock(987654):
            entered.set()
            await asyncio.sleep(0)

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    lock.release()
    appeal_runtime.drop_user_lock(987654)
    replacement = appeal_runtime.get_user_lock(987654)
    assert replacement is lock
    await task
    assert entered.is_set()
    del lock, replacement, task
    gc.collect()
    assert 987654 not in appeal_runtime._user_locks


@pytest_asyncio.fixture
async def card_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cards.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[User.__table__, Operator.__table__, Appeal.__table__, Message.__table__]
            )
        )

    @asynccontextmanager
    async def scope():
        async with factory.begin() as session:
            yield session

    monkeypatch.setattr(admin_card, "session_scope", scope)
    monkeypatch.setattr(appeal_runtime, "session_scope", scope)
    monkeypatch.setattr(admin_card.cfg, "admin_group_id", 555)
    async with scope() as session:
        user = User(max_user_id=12, first_name="Житель", consent_pdn_at=datetime.now(UTC))
        session.add(user)
        await session.flush()
        appeal = Appeal(user_id=user.id, summary="Не горит фонарь", topic="Свет")
        session.add(appeal)
        await session.flush()
        appeal_id = appeal.id
    yield SimpleNamespace(scope=scope, id=appeal_id)
    await engine.dispose()


def _bot(*, side_effect=None):
    return SimpleNamespace(send_message=AsyncMock(
        return_value=SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid="card-1"))),
        side_effect=side_effect,
    ))


@pytest.mark.asyncio
async def test_recovery_publishes_saved_card_once_and_loads_relationships(card_db):
    bot = _bot()
    assert await appeal_runtime.recover_unpublished_appeals(bot) == 1
    assert await appeal_runtime.recover_unpublished_appeals(bot) == 0
    assert bot.send_message.await_count == 1
    assert "Не горит фонарь" in bot.send_message.call_args.kwargs["text"]
    async with card_db.scope() as session:
        saved = await session.get(Appeal, card_db.id)
        assert saved.admin_message_id == "card-1"
        assert saved.initial_card_state == "published"


@pytest.mark.asyncio
async def test_recovery_and_initial_publication_do_not_duplicate(card_db):
    sending = asyncio.Event()
    finish = asyncio.Event()

    async def delayed_send(**_kwargs):
        sending.set()
        await finish.wait()
        return SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid="card-1")))

    bot = _bot(side_effect=delayed_send)
    async with card_db.scope() as session:
        appeal = await admin_card.appeals_service.get_by_id_with_messages(session, card_db.id)
    initial = asyncio.create_task(admin_card.render(bot, appeal, is_first_publication=True))
    await sending.wait()
    recovery = asyncio.create_task(appeal_runtime.recover_unpublished_appeals(bot))
    await asyncio.sleep(0)
    finish.set()
    await asyncio.gather(initial, recovery)
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["blocked", "revoked", "erased", "closed"])
async def test_recovery_skips_ineligible_or_erased_appeal(card_db, reason):
    async with card_db.scope() as session:
        saved = await session.get(Appeal, card_db.id)
        user = await session.scalar(select(User).where(User.id == saved.user_id))
        if reason == "blocked":
            user.is_blocked = True
        elif reason == "revoked":
            user.consent_pdn_at = None
        elif reason == "erased":
            saved.closed_due_to_revoke = True
            saved.summary = None
        else:
            saved.status = "closed"
    bot = _bot()
    assert await appeal_runtime.recover_unpublished_appeals(bot) == 0
    assert bot.send_message.await_count == 0


@pytest.mark.asyncio
async def test_started_send_is_not_replayed_and_warns_once_after_restart(card_db):
    async with card_db.scope() as session:
        saved = await session.get(Appeal, card_db.id)
        saved.initial_card_state = "sending"
    bot = _bot()
    assert await appeal_runtime.recover_unpublished_appeals(bot) == 0
    assert await appeal_runtime.recover_unpublished_appeals(bot) == 0
    assert bot.send_message.await_count == 1
    assert "Не горит фонарь" not in bot.send_message.call_args.kwargs["text"]
    async with card_db.scope() as session:
        saved = await session.get(Appeal, card_db.id)
        assert saved.admin_message_id is None
        assert saved.initial_card_state == "uncertain"
        assert saved.initial_card_notified is True


@pytest.mark.asyncio
async def test_failed_uncertainty_warning_is_retried_without_replaying_card(card_db):
    async with card_db.scope() as session:
        saved = await session.get(Appeal, card_db.id)
        saved.initial_card_state = "sending"
    bot = _bot(side_effect=ConnectionError("offline"))
    assert await appeal_runtime.recover_unpublished_appeals(bot) == 0
    async with card_db.scope() as session:
        saved = await session.get(Appeal, card_db.id)
        assert saved.initial_card_notified is False
    bot.send_message.side_effect = None
    assert await appeal_runtime.recover_unpublished_appeals(bot) == 0
    assert bot.send_message.await_count == 2
    assert all("Не горит фонарь" not in call.kwargs["text"]
               for call in bot.send_message.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "missing_mid", "commit"])
async def test_uncertain_result_is_never_automatically_replayed(card_db, monkeypatch, failure):
    bot = _bot(side_effect=TimeoutError("response lost") if failure == "timeout" else None)
    if failure == "missing_mid":
        bot.send_message.return_value = None
    async with card_db.scope() as session:
        appeal = await admin_card.appeals_service.get_by_id_with_messages(session, card_db.id)
    if failure == "commit":
        # The network succeeded, but the transaction recording its result
        # rolls back. A restart sees durable sending and must not repeat it.
        original = admin_card.session_scope
        calls = 0

        @asynccontextmanager
        async def fail_result_commit():
            nonlocal calls
            calls += 1
            async with original() as session:
                yield session
                if calls == 2:
                    raise RuntimeError("commit unavailable")

        monkeypatch.setattr(admin_card, "session_scope", fail_result_commit)
        with pytest.raises(RuntimeError, match="commit unavailable"):
            await admin_card.render(bot, appeal, is_first_publication=True)
        monkeypatch.setattr(admin_card, "session_scope", original)
    else:
        assert await admin_card.render(bot, appeal, is_first_publication=True) is None
    bot.send_message.side_effect = None
    assert await appeal_runtime.recover_unpublished_appeals(bot) == 0
    # First call was the card, second is only the uncertainty warning.
    assert bot.send_message.await_count == 2
    assert "Не горит фонарь" not in bot.send_message.call_args.kwargs["text"]
    async with card_db.scope() as session:
        saved = await session.get(Appeal, card_db.id)
        assert saved.admin_message_id is None
        assert saved.initial_card_state == "uncertain"


@pytest.mark.asyncio
async def test_explicit_rejection_keeps_card_pending_for_retry(card_db):
    from maxapi.exceptions import MaxApiError

    bot = _bot(side_effect=MaxApiError(code=429, raw="rate limited"))
    assert await appeal_runtime.recover_unpublished_appeals(bot) == 0
    async with card_db.scope() as session:
        saved = await session.get(Appeal, card_db.id)
        assert saved.initial_card_state == "pending"
    bot.send_message.side_effect = None
    assert await appeal_runtime.recover_unpublished_appeals(bot) == 1
    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_recovery_batch_and_parallelism_are_bounded(card_db):
    async with card_db.scope() as session:
        saved = await session.get(Appeal, card_db.id)
        for _ in range(100):
            session.add(Appeal(user_id=saved.user_id, summary="Фонарь", topic="Свет"))
    active = peak = 0

    async def send(**_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid="card-1")))

    bot = _bot(side_effect=send)
    assert await appeal_runtime.recover_unpublished_appeals(bot) == 100
    assert bot.send_message.await_count == 100
    assert peak <= 5
    assert await appeal_runtime.recover_unpublished_appeals(bot) == 1


@pytest.mark.asyncio
async def test_failed_send_claim_commit_prevents_external_send(card_db, monkeypatch):
    bot = _bot()
    original = admin_card.session_scope

    @asynccontextmanager
    async def fail_claim_commit():
        async with original() as session:
            yield session
            raise RuntimeError("claim commit unavailable")

    monkeypatch.setattr(admin_card, "session_scope", fail_claim_commit)
    assert await appeal_runtime.recover_unpublished_appeals(bot) == 0
    assert bot.send_message.await_count == 0
    monkeypatch.setattr(admin_card, "session_scope", original)
    assert await appeal_runtime.recover_unpublished_appeals(bot) == 1


@pytest.mark.asyncio
async def test_cancelled_send_is_durable_and_not_replayed(card_db):
    sending = asyncio.Event()

    async def wait_forever(**_kwargs):
        sending.set()
        await asyncio.Event().wait()

    bot = _bot(side_effect=wait_forever)
    async with card_db.scope() as session:
        appeal = await admin_card.appeals_service.get_by_id_with_messages(session, card_db.id)
    task = asyncio.create_task(admin_card.render(bot, appeal, is_first_publication=True))
    await sending.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with card_db.scope() as session:
        saved = await session.get(Appeal, card_db.id)
        assert saved.initial_card_state == "sending"
    bot.send_message.side_effect = None
    assert await appeal_runtime.recover_unpublished_appeals(bot) == 0
    assert bot.send_message.await_count == 2
    assert "Не горит фонарь" not in bot.send_message.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_warning_without_message_id_is_not_recorded_as_delivered(card_db):
    async with card_db.scope() as session:
        saved = await session.get(Appeal, card_db.id)
        saved.initial_card_state = "sending"
    bot = _bot()
    bot.send_message.return_value = None
    assert await appeal_runtime.recover_unpublished_appeals(bot) == 0
    async with card_db.scope() as session:
        saved = await session.get(Appeal, card_db.id)
        assert saved.initial_card_notified is False
