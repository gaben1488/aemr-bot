"""PG-тесты расширенного /diag (PR I).

Проверяем, что новые секции (24h activity, pulse, stuck broadcasts,
warnings) корректно собираются из реальной БД.

Без maxapi — диагностика не требует UI, мы напрямую дёргаем _do_diag
через mock event.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


pytest.importorskip("maxapi", reason="diag — handler-level test, нужен maxapi")


def _make_event() -> SimpleNamespace:
    """MAX-событие в админ-группе с минимально достаточной формой."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return SimpleNamespace(
        bot=bot,
        message=SimpleNamespace(
            answer=AsyncMock(),
            sender=SimpleNamespace(user_id=7),
            recipient=SimpleNamespace(chat_id=555),  # cfg.admin_group_id в conftest
            body=SimpleNamespace(text="", attachments=[], mid="m-1"),
        ),
        callback=SimpleNamespace(callback_id="cb-1"),
    )


@pytest.mark.asyncio
async def test_diag_pulse_warning_when_events_silent(session) -> None:
    """Если последнее событие старше 15 мин — pulse-индикатор показывает
    предупреждение в секции «Внимание»."""
    from aemr_bot.db.models import Event
    from aemr_bot.handlers import admin_panel

    # Записываем «древнее» событие
    old_event = Event(
        kind="ping",
        received_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    session.add(old_event)
    await session.flush()

    sent: dict = {}

    async def fake_send_or_edit(event, *, chat_id, text, attachments=None,
                                **kwargs):
        sent["text"] = text

    # Подменяем session_scope() — он отдаёт нашу же сессию (через CM).
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _scope():
        yield session

    with (
        patch("aemr_bot.handlers.admin_panel.session_scope", _scope),
        patch("aemr_bot.handlers.admin_panel.send_or_edit_screen",
              new=fake_send_or_edit),
    ):
        await admin_panel._do_diag(_make_event())

    assert "Pulse" in sent["text"]
    assert "Внимание" in sent["text"], sent["text"]
    assert "молчит" in sent["text"].lower()


@pytest.mark.asyncio
async def test_diag_no_warnings_when_pulse_fresh(session) -> None:
    """Свежий pulse (1 мин назад) → секция «Внимание» отсутствует,
    показывается «Аномалий не обнаружено»."""
    from aemr_bot.db.models import Event
    from aemr_bot.handlers import admin_panel

    fresh = Event(
        kind="ping",
        received_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    session.add(fresh)
    await session.flush()

    sent: dict = {}

    async def fake_send_or_edit(event, *, chat_id, text, attachments=None,
                                **kwargs):
        sent["text"] = text

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _scope():
        yield session

    with (
        patch("aemr_bot.handlers.admin_panel.session_scope", _scope),
        patch("aemr_bot.handlers.admin_panel.send_or_edit_screen",
              new=fake_send_or_edit),
    ):
        await admin_panel._do_diag(_make_event())

    assert "Аномалий не обнаружено" in sent["text"]


@pytest.mark.asyncio
async def test_diag_stuck_broadcast_in_warnings(session) -> None:
    """Рассылка в статусе SENDING старше 10 мин → warning «Зависших»."""
    from aemr_bot.db.models import Broadcast, BroadcastStatus, Event
    from aemr_bot.handlers import admin_panel

    # Свежий pulse (чтобы не сбивал основной сигнал)
    session.add(
        Event(kind="ping", received_at=datetime.now(timezone.utc))
    )
    # Зависшая рассылка
    stuck = Broadcast(
        text="x",
        subscriber_count_at_start=10,
        status=BroadcastStatus.SENDING.value,
        delivered_count=3,
        failed_count=0,
    )
    session.add(stuck)
    await session.flush()
    # Принудительно «состарим» её через UPDATE, иначе SQLAlchemy ставит NOW().
    from sqlalchemy import update

    await session.execute(
        update(Broadcast)
        .where(Broadcast.id == stuck.id)
        .values(created_at=datetime.now(timezone.utc) - timedelta(minutes=30))
    )

    sent: dict = {}

    async def fake_send_or_edit(event, *, chat_id, text, attachments=None,
                                **kwargs):
        sent["text"] = text

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _scope():
        yield session

    with (
        patch("aemr_bot.handlers.admin_panel.session_scope", _scope),
        patch("aemr_bot.handlers.admin_panel.send_or_edit_screen",
              new=fake_send_or_edit),
    ):
        await admin_panel._do_diag(_make_event())

    assert "Зависших" in sent["text"]
    assert "Внимание" in sent["text"]


@pytest.mark.asyncio
async def test_diag_24h_counters_present(session) -> None:
    """Диагностика содержит секции «Жители / Обращения / Рассылки»
    с признаком «за 24ч»."""
    from aemr_bot.handlers import admin_panel

    sent: dict = {}

    async def fake_send_or_edit(event, *, chat_id, text, attachments=None,
                                **kwargs):
        sent["text"] = text

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _scope():
        yield session

    with (
        patch("aemr_bot.handlers.admin_panel.session_scope", _scope),
        patch("aemr_bot.handlers.admin_panel.send_or_edit_screen",
              new=fake_send_or_edit),
    ):
        await admin_panel._do_diag(_make_event())

    assert "Жители" in sent["text"]
    assert "Обращения" in sent["text"]
    assert "Рассылки" in sent["text"]
    assert "за 24ч" in sent["text"].lower()
