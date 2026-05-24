"""Тесты единого render_admin_card (services/admin_card.render).

Контракт:
- `Appeal.admin_message_id` всегда указывает на актуальную (последнюю)
  карточку обращения после render().
- force_new=False: пытается edit; на fail или отсутствие
  admin_message_id — fallback на send + update.
- force_new=True: всегда send + update admin_message_id.

Заменяет три несинхронных механизма edit (operator_reply прямой,
admin_appeal_ops через freshness-tracker, appeal_funnel ручной send).
Конкретная проблема, которую закрывает: после followup от жителя
admin_message_id не обновлялся → reply редактировал старую карточку
вверху чата.
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
    async def test_force_new_true_sends_even_with_existing_mid(self) -> None:
        """force_new=True игнорирует существующий admin_message_id и шлёт
        новую карточку (followup от жителя — нужна явная отметка внизу)."""
        from aemr_bot.services import admin_card

        appeal = _make_appeal(admin_mid="old-mid-3")
        bot = _make_bot_with_returned_mid(new_mid="newer-mid-4")
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
        # admin_message_id обновлён на новый
        update_mid.assert_awaited_once()
        assert update_mid.await_args.args[2] == "newer-mid-4"
        assert mid == "newer-mid-4"


class TestInvariantAdminMidPointsToLatest:
    """Главный инвариант: после render(force_new=True) admin_message_id
    указывает на новую карточку — следующий render(force_new=False)
    edit'ит её, а не старую."""

    @pytest.mark.asyncio
    async def test_followup_then_reply_edits_fresh_card_not_original(
        self,
    ) -> None:
        from aemr_bot.services import admin_card

        appeal = _make_appeal(admin_mid="original-mid-1")
        bot = _make_bot_with_returned_mid(new_mid="fresh-after-followup-2")
        update_mid = AsyncMock()
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
            patch("aemr_bot.services.admin_card.session_scope", _fake_session_scope),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_admin_message_id",
                update_mid,
            ),
        ):
            # Шаг 1: followup жителя → force_new=True
            await admin_card.render(bot, appeal, force_new=True)
            # Симулируем что БД обновилась — appeal.admin_message_id теперь новый
            appeal.admin_message_id = "fresh-after-followup-2"
            # Шаг 2: оператор отвечает → force_new=False (edit)
            mid = await admin_card.render(bot, appeal, force_new=False)

        # Главная проверка: edit улетел в СВЕЖИЙ mid, не в original
        assert bot.edit_message.await_args.kwargs["message_id"] == (
            "fresh-after-followup-2"
        )
        assert mid == "fresh-after-followup-2"


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
