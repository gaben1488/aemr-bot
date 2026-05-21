"""Тесты для PR G — карточка рассылки в истории.

- _open_broadcast: показывает текст/картинки/счётчики, кнопки.
- _clone_broadcast: prefill /broadcast wizard в awaiting_confirm.
- _list_failed_deliveries: показывает имена и обрезанные ошибки.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


def _make_event(*, user_id: int = 7) -> SimpleNamespace:
    return make_event(chat_id=555, user_id=user_id, with_callback=True)


@pytest.fixture(autouse=True)
def _clean_wizards():
    from aemr_bot.handlers import broadcast
    from aemr_bot.utils import menu_tracker

    broadcast._wizards.clear()
    menu_tracker.clear_all()
    yield
    broadcast._wizards.clear()
    menu_tracker.clear_all()


def _fake_broadcast(
    *, bc_id: int = 42, failed: int = 0, attachments=None
) -> SimpleNamespace:
    return SimpleNamespace(
        id=bc_id,
        status="done",
        created_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        delivered_count=10,
        failed_count=failed,
        subscriber_count_at_start=10 + failed,
        attachments=list(attachments or []),
        text="Уважаемые жители!",
    )


class TestOpenBroadcast:
    @pytest.mark.asyncio
    async def test_unknown_id_shows_not_found(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event()
        with (
            patch(
                "aemr_bot.handlers.broadcast._ensure_role",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "aemr_bot.handlers.broadcast.session_scope",
                _fake_session_scope,
            ),
            patch(
                "aemr_bot.handlers.broadcast.broadcasts_service.get_by_id",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "aemr_bot.handlers.broadcast.send_or_edit_screen",
                new=AsyncMock(),
            ) as mock_send,
        ):
            await broadcast._open_broadcast(event, 999)
        sent_text = mock_send.await_args.kwargs.get("text")
        assert "не найдена" in (sent_text or "").lower()

    @pytest.mark.asyncio
    async def test_renders_text_and_no_failures_hides_button(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event()
        bc = _fake_broadcast(failed=0)
        with (
            patch(
                "aemr_bot.handlers.broadcast._ensure_role",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "aemr_bot.handlers.broadcast.session_scope",
                _fake_session_scope,
            ),
            patch(
                "aemr_bot.handlers.broadcast.broadcasts_service.get_by_id",
                new=AsyncMock(return_value=bc),
            ),
            patch(
                "aemr_bot.handlers.broadcast.send_or_edit_screen",
                new=AsyncMock(),
            ) as mock_send,
        ):
            await broadcast._open_broadcast(event, bc.id)

        sent_text = mock_send.await_args.kwargs.get("text") or ""
        assert "Уважаемые жители!" in sent_text
        assert f"#{bc.id}" in sent_text
        # «Не доставлено» строка в card отсутствует, когда failed=0
        assert "Не доставлено" not in sent_text

    @pytest.mark.asyncio
    async def test_failures_show_in_card(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event()
        bc = _fake_broadcast(failed=3)
        with (
            patch(
                "aemr_bot.handlers.broadcast._ensure_role",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "aemr_bot.handlers.broadcast.session_scope",
                _fake_session_scope,
            ),
            patch(
                "aemr_bot.handlers.broadcast.broadcasts_service.get_by_id",
                new=AsyncMock(return_value=bc),
            ),
            patch(
                "aemr_bot.handlers.broadcast.send_or_edit_screen",
                new=AsyncMock(),
            ) as mock_send,
        ):
            await broadcast._open_broadcast(event, bc.id)
        sent_text = mock_send.await_args.kwargs.get("text") or ""
        assert "Не доставлено: 3" in sent_text


class TestCloneBroadcast:
    @pytest.mark.asyncio
    async def test_clone_prefills_wizard_and_shows_preview(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event(user_id=7)
        bc = _fake_broadcast()
        with (
            patch(
                "aemr_bot.handlers.broadcast._ensure_role",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "aemr_bot.handlers.broadcast.session_scope",
                _fake_session_scope,
            ),
            patch(
                "aemr_bot.handlers.broadcast.broadcasts_service.get_by_id",
                new=AsyncMock(return_value=bc),
            ),
            patch(
                "aemr_bot.handlers.broadcast.broadcasts_service.count_subscribers",
                new=AsyncMock(return_value=42),
            ),
            patch(
                "aemr_bot.handlers.broadcast.send_or_edit_screen",
                new=AsyncMock(),
            ),
        ):
            await broadcast._clone_broadcast(event, bc.id)
        # wizard в шаге awaiting_confirm, текст и attachments из bc.
        state = broadcast._wizards.get(7)
        assert state is not None
        assert state.step == "awaiting_confirm"
        assert state.text == bc.text

    @pytest.mark.asyncio
    async def test_clone_without_subscribers_skips_prefill(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event(user_id=7)
        bc = _fake_broadcast()
        with (
            patch(
                "aemr_bot.handlers.broadcast._ensure_role",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "aemr_bot.handlers.broadcast.session_scope",
                _fake_session_scope,
            ),
            patch(
                "aemr_bot.handlers.broadcast.broadcasts_service.get_by_id",
                new=AsyncMock(return_value=bc),
            ),
            patch(
                "aemr_bot.handlers.broadcast.broadcasts_service.count_subscribers",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "aemr_bot.handlers.broadcast.send_or_edit_screen",
                new=AsyncMock(),
            ) as mock_send,
        ):
            await broadcast._clone_broadcast(event, bc.id)
        # wizard не задан — нет подписчиков
        assert 7 not in broadcast._wizards
        sent_text = mock_send.await_args.kwargs.get("text") or ""
        assert "некому" in sent_text.lower() or "подписчик" in sent_text.lower()


class TestListFailed:
    @pytest.mark.asyncio
    async def test_no_failures_shows_empty_message(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event()
        bc = _fake_broadcast(failed=0)
        with (
            patch(
                "aemr_bot.handlers.broadcast._ensure_role",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "aemr_bot.handlers.broadcast.session_scope",
                _fake_session_scope,
            ),
            patch(
                "aemr_bot.handlers.broadcast.broadcasts_service.get_by_id",
                new=AsyncMock(return_value=bc),
            ),
            patch(
                "aemr_bot.handlers.broadcast.broadcasts_service.list_failed_deliveries",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "aemr_bot.handlers.broadcast.send_or_edit_screen",
                new=AsyncMock(),
            ) as mock_send,
        ):
            await broadcast._list_failed_deliveries(event, bc.id)
        sent_text = mock_send.await_args.kwargs.get("text") or ""
        assert "нет неуспешных" in sent_text.lower() or "пуст" in sent_text.lower()

    @pytest.mark.asyncio
    async def test_renders_names_and_truncates_long_error(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event()
        bc = _fake_broadcast(failed=2)
        rows = [
            (101, "Анна", "RuntimeError('blocked by user')"),
            (102, "Борис", "X" * 500),  # длинная ошибка — должна обрезаться
        ]
        with (
            patch(
                "aemr_bot.handlers.broadcast._ensure_role",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "aemr_bot.handlers.broadcast.session_scope",
                _fake_session_scope,
            ),
            patch(
                "aemr_bot.handlers.broadcast.broadcasts_service.get_by_id",
                new=AsyncMock(return_value=bc),
            ),
            patch(
                "aemr_bot.handlers.broadcast.broadcasts_service.list_failed_deliveries",
                new=AsyncMock(return_value=rows),
            ),
            patch(
                "aemr_bot.handlers.broadcast.send_or_edit_screen",
                new=AsyncMock(),
            ) as mock_send,
        ):
            await broadcast._list_failed_deliveries(event, bc.id)
        sent_text = mock_send.await_args.kwargs.get("text") or ""
        assert "Анна" in sent_text
        assert "Борис" in sent_text
        # 500 X-ов не уходит — обрезано до 100
        assert "X" * 500 not in sent_text
