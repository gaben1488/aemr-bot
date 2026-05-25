"""Тесты event-log семантики `services/admin_card.render` (DDD pivot).

Контракт:
- Карточка обращения = иммутабельная запись о событии.
- Каждый render() публикует НОВУЮ карточку (send_message, не edit).
- Обновляет `Appeal.last_admin_card_mid` каждый раз — точка stale-
  detection и свайп-reply.
- `is_first_publication=True` (только при finalize) дополнительно
  обновляет `admin_message_id` (для reply-link при relay вложений).
- На любых других render — admin_message_id НЕ двигается; оригинал
  остаётся как «mid первой публикации».

Этот контракт заменяет старый «edit vs new» — последнее правило
смешивало two miры (event-карточка vs навигация).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope


pytest.importorskip("maxapi", reason="нужен maxapi для card_format")


def _make_appeal(*, appeal_id: int = 5, admin_mid=None, last_card_mid=None):
    user = SimpleNamespace(
        first_name="Сергей",
        phone="+79991234567",
        is_blocked=False,
        consent_pdn_at=None,
        consent_revoked_at=None,
        subscribed_broadcast=False,
        max_user_id=42,
    )
    appeal = SimpleNamespace(
        id=appeal_id,
        user=user,
        status="new",
        locality="Елизовское ГП",
        address="ул. Ленина, 5",
        topic="Дороги",
        summary="Яма во дворе.",
        attachments=[],
        admin_message_id=admin_mid,
        last_admin_card_mid=last_card_mid,
        closed_due_to_revoke=False,
    )
    appeal.__dict__["messages"] = []
    return appeal


def _make_bot(new_mid="new-mid-1"):
    return SimpleNamespace(
        send_message=AsyncMock(
            return_value=SimpleNamespace(
                message=SimpleNamespace(body=SimpleNamespace(mid=new_mid))
            )
        ),
        edit_message=AsyncMock(),
    )


class TestEventLogSemantics:
    """Каждый render = новая карточка, НИКОГДА edit."""

    @pytest.mark.asyncio
    async def test_render_always_sends_new_never_edits(self) -> None:
        from aemr_bot.services import admin_card

        appeal = _make_appeal(admin_mid="original-1", last_card_mid="latest-7")
        bot = _make_bot(new_mid="event-card-8")
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
            patch("aemr_bot.services.admin_card.session_scope",
                  _fake_session_scope),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_last_admin_card_mid",
                AsyncMock(),
            ),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_admin_message_id",
                AsyncMock(),
            ),
        ):
            mid = await admin_card.render(bot, appeal)

        # ВСЕГДА send, никогда edit
        bot.send_message.assert_awaited_once()
        bot.edit_message.assert_not_called()
        assert mid == "event-card-8"

    @pytest.mark.asyncio
    async def test_render_updates_last_admin_card_mid_every_time(self) -> None:
        from aemr_bot.services import admin_card

        appeal = _make_appeal(admin_mid="original-1", last_card_mid="latest-7")
        bot = _make_bot(new_mid="even-newer-9")
        update_last = AsyncMock()
        update_first = AsyncMock()
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
            patch("aemr_bot.services.admin_card.session_scope",
                  _fake_session_scope),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_last_admin_card_mid",
                update_last,
            ),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_admin_message_id",
                update_first,
            ),
        ):
            await admin_card.render(bot, appeal)

        update_last.assert_awaited_once()
        assert update_last.await_args.args[2] == "even-newer-9"
        # is_first_publication=False (default) → admin_message_id НЕ трогаем
        update_first.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_publication_updates_both_mids(self) -> None:
        """На finalize обновляем оба: admin_message_id (sacred первый)
        и last_admin_card_mid (текущая карточка)."""
        from aemr_bot.services import admin_card

        appeal = _make_appeal(admin_mid=None, last_card_mid=None)
        bot = _make_bot(new_mid="finalize-1")
        update_last = AsyncMock()
        update_first = AsyncMock()
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
            patch("aemr_bot.services.admin_card.session_scope",
                  _fake_session_scope),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_last_admin_card_mid",
                update_last,
            ),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_admin_message_id",
                update_first,
            ),
        ):
            mid = await admin_card.render(bot, appeal, is_first_publication=True)

        update_last.assert_awaited_once()
        update_first.assert_awaited_once()
        assert update_first.await_args.args[2] == "finalize-1"
        assert mid == "finalize-1"


class TestEventLogClosesBug:
    """Регрессия на конкретный bug владельца:
    «открыл 2 карточки обращения и закрыл одну — одна обновилась,
    другая нет». Корень — старый edit-режим менял только admin_message_id.
    Новая семантика: каждое close = новая карточка с CLOSED статусом,
    обе старые карточки остаются как audit-trail, оператор видит
    результат внизу чата гарантированно."""

    @pytest.mark.asyncio
    async def test_close_publishes_new_card_regardless_of_tap_location(
        self,
    ) -> None:
        """Тап на любой карточке (оригинал/следовая) → НОВАЯ карточка
        внизу. Оператор всегда видит результат."""
        from aemr_bot.services import admin_card

        # Шаг 1: 2 карточки обращения уже опубликованы (оригинал +
        # следовая после followup жителя)
        appeal = _make_appeal(
            admin_mid="original-1",
            last_card_mid="followup-card-2",
        )
        appeal.status = "closed"  # close уже произошёл в БД
        bot = _make_bot(new_mid="close-event-card-3")
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
            patch("aemr_bot.services.admin_card.session_scope",
                  _fake_session_scope),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_last_admin_card_mid",
                AsyncMock(),
            ),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_admin_message_id",
                AsyncMock(),
            ),
        ):
            mid = await admin_card.render(bot, appeal)

        # Главная проверка: НИКАКОГО edit_message ни на оригинале,
        # ни на следовой. Только send новой карточки внизу.
        bot.edit_message.assert_not_called()
        bot.send_message.assert_awaited_once()
        assert mid == "close-event-card-3"


class TestNoUserGuard:
    @pytest.mark.asyncio
    async def test_appeal_without_user_returns_none(self) -> None:
        from aemr_bot.services import admin_card

        appeal = SimpleNamespace(
            id=99, user=None, admin_message_id=None,
            last_admin_card_mid=None, attachments=[],
        )
        appeal.__dict__["messages"] = []
        bot = _make_bot()
        with patch("aemr_bot.config.settings.admin_group_id", 555):
            mid = await admin_card.render(bot, appeal)
        assert mid is None
        bot.send_message.assert_not_called()


class TestNoAdminGroupGuard:
    @pytest.mark.asyncio
    async def test_no_admin_group_returns_none(self) -> None:
        from aemr_bot.services import admin_card

        appeal = _make_appeal()
        bot = _make_bot()
        with patch("aemr_bot.config.settings.admin_group_id", 0):
            mid = await admin_card.render(bot, appeal)
        assert mid is None
