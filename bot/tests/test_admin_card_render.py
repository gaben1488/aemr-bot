"""Тесты единого render_admin_card (services/admin_card.render).

Контракт:
- `Appeal.admin_message_id` указывает на ОРИГИНАЛЬНУЮ карточку
  обращения (sacred artifact с finalize). Меняется только при
  изменении статуса (edit fail → переезд на новый mid; первая
  публикация).
- `force_new=False`: пытается edit оригинала; на fail — fallback
  send-new + update admin_message_id (старый mid недействителен).
- `force_new=True`: ВСЕГДА send-new, НО admin_message_id НЕ
  обновляется (это «следовая» карточка от followup, оригинал
  остаётся в БД).

Разделение: оригинал = «вот обращение, отвечайте здесь». Followup-
карточки = «вот ещё информация». Reply/reopen/close через оригинал.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope


pytest.importorskip("maxapi", reason="нужен maxapi для card_format")


def _make_appeal(*, appeal_id: int = 5, admin_mid: str | None = None) -> SimpleNamespace:
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
        messages=[],
        admin_message_id=admin_mid,
        closed_due_to_revoke=False,
    )
    return appeal


def _make_bot_with_returned_mid(new_mid: str = "new-mid-1") -> SimpleNamespace:
    """Мок bot с send_message возвращающим object с mid."""
    bot = SimpleNamespace()
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(
            message=SimpleNamespace(body=SimpleNamespace(mid=new_mid))
        )
    )
    bot.edit_message = AsyncMock()
    return bot


class TestEditPath:
    @pytest.mark.asyncio
    async def test_existing_mid_force_false_edits_in_place(self) -> None:
        """force_new=False + есть admin_message_id → edit, не send."""
        from aemr_bot.services import admin_card

        appeal = _make_appeal(admin_mid="existing-mid-7")
        bot = _make_bot_with_returned_mid()
        with patch("aemr_bot.config.settings.admin_group_id", 555):
            mid = await admin_card.render(bot, appeal, force_new=False)

        bot.edit_message.assert_awaited_once()
        assert bot.edit_message.await_args.kwargs["message_id"] == "existing-mid-7"
        bot.send_message.assert_not_called()
        assert mid == "existing-mid-7"

    @pytest.mark.asyncio
    async def test_edit_failure_falls_back_to_send_and_updates_mid(self) -> None:
        """Если edit_message бросает — шлём новую + обновляем admin_message_id."""
        from aemr_bot.services import admin_card

        appeal = _make_appeal(admin_mid="stale-mid")
        bot = _make_bot_with_returned_mid(new_mid="fresh-mid-9")
        bot.edit_message = AsyncMock(side_effect=Exception("MAX 404"))
        update_mid = AsyncMock()
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
            patch("aemr_bot.services.admin_card.session_scope", _fake_session_scope),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_admin_message_id",
                update_mid,
            ),
        ):
            mid = await admin_card.render(bot, appeal, force_new=False)

        bot.send_message.assert_awaited_once()
        update_mid.assert_awaited_once()
        assert update_mid.await_args.args[1] == appeal.id
        assert update_mid.await_args.args[2] == "fresh-mid-9"
        assert mid == "fresh-mid-9"


class TestSendNewPath:
    @pytest.mark.asyncio
    async def test_no_existing_mid_sends_new_and_saves(self) -> None:
        """Нет admin_message_id (первая публикация) → send + сохранение."""
        from aemr_bot.services import admin_card

        appeal = _make_appeal(admin_mid=None)
        bot = _make_bot_with_returned_mid(new_mid="first-mid-1")
        update_mid = AsyncMock()
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
            patch("aemr_bot.services.admin_card.session_scope", _fake_session_scope),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_admin_message_id",
                update_mid,
            ),
        ):
            mid = await admin_card.render(bot, appeal, force_new=False)

        bot.edit_message.assert_not_called()
        bot.send_message.assert_awaited_once()
        update_mid.assert_awaited_once()
        assert mid == "first-mid-1"

    @pytest.mark.asyncio
    async def test_force_new_true_sends_but_keeps_original_admin_mid(
        self,
    ) -> None:
        """Главный инвариант: force_new=True (followup) шлёт следовую
        карточку, НО admin_message_id оригинала остаётся неизменным.
        Оригинал sacred — там живут reply/reopen/close."""
        from aemr_bot.services import admin_card

        appeal = _make_appeal(admin_mid="original-mid-3")
        bot = _make_bot_with_returned_mid(new_mid="followup-mid-4")
        update_mid = AsyncMock()
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
            patch("aemr_bot.services.admin_card.session_scope", _fake_session_scope),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_admin_message_id",
                update_mid,
            ),
        ):
            mid = await admin_card.render(bot, appeal, force_new=True)

        bot.edit_message.assert_not_called()
        bot.send_message.assert_awaited_once()
        # admin_message_id НЕ обновлён — оригинал sacred при force_new=True
        update_mid.assert_not_called()
        assert mid == "followup-mid-4"


class TestInvariantOriginalCardStable:
    """Главный инвариант: оригинальная карточка обращения (admin_message_id
    из БД на finalize) остаётся sacred. Followup жителя публикует
    следовую карточку, НО reply/reopen/close всё равно идут в оригинал."""

    @pytest.mark.asyncio
    async def test_followup_then_reply_edits_original_not_followup_card(
        self,
    ) -> None:
        from aemr_bot.services import admin_card

        appeal = _make_appeal(admin_mid="original-mid-1")
        bot = _make_bot_with_returned_mid(new_mid="followup-mid-2")
        update_mid = AsyncMock()
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
            patch("aemr_bot.services.admin_card.session_scope", _fake_session_scope),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_admin_message_id",
                update_mid,
            ),
        ):
            # Шаг 1: followup жителя → force_new=True (новая карточка,
            # но admin_message_id оригинала НЕ меняется в БД)
            await admin_card.render(bot, appeal, force_new=True)
            # admin_message_id в appeal остаётся прежним (БД-state)
            assert appeal.admin_message_id == "original-mid-1"
            # Шаг 2: оператор отвечает → force_new=False (edit оригинала)
            mid = await admin_card.render(bot, appeal, force_new=False)

        # Главная проверка: edit улетел в ОРИГИНАЛ, не в followup-карточку
        assert bot.edit_message.await_args.kwargs["message_id"] == (
            "original-mid-1"
        )
        assert mid == "original-mid-1"
        update_mid.assert_not_called()  # admin_message_id неизменен


class TestNoUserGuard:
    @pytest.mark.asyncio
    async def test_appeal_without_user_returns_none_no_crash(self) -> None:
        from aemr_bot.services import admin_card

        appeal = SimpleNamespace(
            id=99, user=None, admin_message_id=None, attachments=[], messages=[]
        )
        bot = _make_bot_with_returned_mid()
        with patch("aemr_bot.config.settings.admin_group_id", 555):
            mid = await admin_card.render(bot, appeal)

        assert mid is None
        bot.send_message.assert_not_called()
        bot.edit_message.assert_not_called()


class TestNoAdminGroupGuard:
    @pytest.mark.asyncio
    async def test_no_admin_group_returns_none(self) -> None:
        from aemr_bot.services import admin_card

        appeal = _make_appeal()
        bot = _make_bot_with_returned_mid()
        with patch("aemr_bot.config.settings.admin_group_id", 0):
            mid = await admin_card.render(bot, appeal)
        assert mid is None
        bot.send_message.assert_not_called()
