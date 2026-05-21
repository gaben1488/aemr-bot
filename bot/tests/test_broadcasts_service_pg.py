"""PG-тесты services/broadcasts.

Проверяем не UI-мастер, а инварианты хранилища рассылок. Главный
регресс: аварийное завершение рассылки не должно обнулять счётчики,
если часть доставок уже записана в broadcast_deliveries.
"""
from __future__ import annotations

import pytest

from aemr_bot.db.models import BroadcastStatus
from aemr_bot.services import broadcasts as broadcasts_service
from aemr_bot.services import users as users_service


@pytest.mark.asyncio
async def test_count_delivery_results_counts_success_and_failures(session) -> None:
    """Успешные и ошибочные доставки считаются по строкам delivery-log."""
    user1 = await users_service.get_or_create(session, max_user_id=101, first_name="A")
    user2 = await users_service.get_or_create(session, max_user_id=102, first_name="B")
    bc = await broadcasts_service.create_broadcast(
        session,
        text="важное сообщение",
        operator_id=None,
        subscriber_count=2,
    )
    await broadcasts_service.record_delivery(
        session,
        broadcast_id=bc.id,
        user_id=user1.id,
        error=None,
    )
    await broadcasts_service.record_delivery(
        session,
        broadcast_id=bc.id,
        user_id=user2.id,
        error="RuntimeError('blocked')",
    )

    delivered, failed = await broadcasts_service.count_delivery_results(session, bc.id)

    assert delivered == 1
    assert failed == 1


@pytest.mark.asyncio
async def test_mark_finished_failed_zeroes_are_replaced_by_recorded_counters(session) -> None:
    """Если аварийный wrapper передал FAILED + 0/0, сервис не должен
    потерять уже записанную частичную доставку."""
    user1 = await users_service.get_or_create(session, max_user_id=101, first_name="A")
    user2 = await users_service.get_or_create(session, max_user_id=102, first_name="B")
    bc = await broadcasts_service.create_broadcast(
        session,
        text="важное сообщение",
        operator_id=None,
        subscriber_count=2,
    )
    await broadcasts_service.record_delivery(
        session,
        broadcast_id=bc.id,
        user_id=user1.id,
        error=None,
    )
    await broadcasts_service.record_delivery(
        session,
        broadcast_id=bc.id,
        user_id=user2.id,
        error="RuntimeError('blocked')",
    )

    await broadcasts_service.mark_finished(
        session,
        bc.id,
        status=BroadcastStatus.FAILED,
        delivered=0,
        failed=0,
    )

    refreshed = await broadcasts_service.get_by_id(session, bc.id)
    assert refreshed is not None
    assert refreshed.status == BroadcastStatus.FAILED.value
    assert refreshed.delivered_count == 1
    assert refreshed.failed_count == 1


@pytest.mark.asyncio
async def test_mark_finished_done_keeps_explicit_counters(session) -> None:
    """Для штатного DONE/CANCELLED пути сохраняем счётчики вызывающего кода."""
    bc = await broadcasts_service.create_broadcast(
        session,
        text="важное сообщение",
        operator_id=None,
        subscriber_count=3,
    )

    await broadcasts_service.mark_finished(
        session,
        bc.id,
        status=BroadcastStatus.DONE,
        delivered=3,
        failed=0,
    )

    refreshed = await broadcasts_service.get_by_id(session, bc.id)
    assert refreshed is not None
    assert refreshed.status == BroadcastStatus.DONE.value
    assert refreshed.delivered_count == 3
    assert refreshed.failed_count == 0


# ---- list_failed_deliveries (PR G) ---------------------------------


@pytest.mark.asyncio
async def test_list_failed_deliveries_empty(session) -> None:
    """Рассылка без failed-доставок возвращает пустой список."""
    bc = await broadcasts_service.create_broadcast(
        session, text="t", operator_id=None, subscriber_count=0
    )
    result = await broadcasts_service.list_failed_deliveries(session, bc.id)
    assert result == []


@pytest.mark.asyncio
async def test_list_failed_deliveries_only_failures(session) -> None:
    """Успешные доставки игнорируются — в выборку идут только error IS NOT NULL."""
    user1 = await users_service.get_or_create(session, max_user_id=101, first_name="Анна")
    user2 = await users_service.get_or_create(session, max_user_id=102, first_name="Борис")
    user3 = await users_service.get_or_create(session, max_user_id=103, first_name="Вера")
    bc = await broadcasts_service.create_broadcast(
        session, text="t", operator_id=None, subscriber_count=3
    )
    await broadcasts_service.record_delivery(
        session, broadcast_id=bc.id, user_id=user1.id, error=None
    )
    await broadcasts_service.record_delivery(
        session, broadcast_id=bc.id, user_id=user2.id, error="blocked"
    )
    await broadcasts_service.record_delivery(
        session, broadcast_id=bc.id, user_id=user3.id, error="api_error"
    )

    result = await broadcasts_service.list_failed_deliveries(session, bc.id)
    names = [r[1] for r in result]
    errors = [r[2] for r in result]

    assert "Анна" not in names  # успешная не попала
    assert set(names) == {"Борис", "Вера"}
    assert "blocked" in errors
    assert "api_error" in errors


@pytest.mark.asyncio
async def test_list_failed_deliveries_respects_limit(session) -> None:
    """Запрос с маленьким limit возвращает ровно limit строк."""
    bc = await broadcasts_service.create_broadcast(
        session, text="t", operator_id=None, subscriber_count=5
    )
    for i in range(5):
        u = await users_service.get_or_create(
            session, max_user_id=200 + i, first_name=f"U{i}"
        )
        await broadcasts_service.record_delivery(
            session, broadcast_id=bc.id, user_id=u.id, error="x"
        )

    result = await broadcasts_service.list_failed_deliveries(
        session, bc.id, limit=3
    )
    assert len(result) == 3
