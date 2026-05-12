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
