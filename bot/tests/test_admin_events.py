"""Тесты служебных уведомлений о действиях жителя."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aemr_bot.services import notify_toggles


@pytest.fixture(autouse=True)
def _reset_notify_toggles():
    """Изоляция: cache тумблеров в default state (все True)."""
    notify_toggles.reset_cache_for_tests()
    yield
    notify_toggles.reset_cache_for_tests()


@pytest.mark.asyncio
async def test_notify_consent_given_sends_to_admin_group() -> None:
    from aemr_bot.services import admin_events

    bot = AsyncMock()
    with patch("aemr_bot.services.admin_events.cfg.admin_group_id", 777):
        await admin_events.notify_consent_given(bot, max_user_id=42)

    bot.send_message.assert_called_once()
    assert bot.send_message.call_args.kwargs["chat_id"] == 777
    text = bot.send_message.call_args.kwargs["text"]
    assert "согласие" in text.lower()
    assert "42" in text


@pytest.mark.asyncio
async def test_notify_consent_revoked_points_to_final_reply() -> None:
    from aemr_bot.services import admin_events

    bot = AsyncMock()
    with patch("aemr_bot.services.admin_events.cfg.admin_group_id", 777):
        await admin_events.notify_consent_revoked(
            bot,
            max_user_id=42,
            open_appeal_ids=[10, 11],
        )

    text = bot.send_message.call_args.kwargs["text"]
    assert "финального ответа" in text.lower()
    assert "#10" in text
    assert "#11" in text
    assert "телефон" not in text.lower()


@pytest.mark.asyncio
async def test_notify_skips_when_admin_group_is_not_configured() -> None:
    from aemr_bot.services import admin_events

    bot = AsyncMock()
    with patch("aemr_bot.services.admin_events.cfg.admin_group_id", None):
        await admin_events.notify_broadcast_subscribed(bot, max_user_id=42)

    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_notify_logs_and_does_not_raise_on_delivery_error() -> None:
    from aemr_bot.services import admin_events

    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("MAX unavailable")

    with patch("aemr_bot.services.admin_events.cfg.admin_group_id", 777):
        await admin_events.notify_broadcast_unsubscribed(
            bot,
            max_user_id=42,
            source="меню",
        )

    bot.send_message.assert_called_once()


class TestNotifyToggleGates:
    """2026-07-09: `admin_notify_consent` / `admin_notify_subscriptions`
    гейтят рутинные уведомления (services/notify_toggles.py)."""

    @pytest.mark.asyncio
    async def test_consent_given_suppressed_when_toggle_disabled(self) -> None:
        from aemr_bot.services import admin_events

        bot = AsyncMock()
        with patch("aemr_bot.services.admin_events.cfg.admin_group_id", 777), \
             patch.object(notify_toggles, "is_enabled", return_value=False):
            await admin_events.notify_consent_given(bot, max_user_id=42)
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_consent_given_sent_when_toggle_enabled(self) -> None:
        from aemr_bot.services import admin_events

        bot = AsyncMock()
        with patch("aemr_bot.services.admin_events.cfg.admin_group_id", 777), \
             patch.object(notify_toggles, "is_enabled", return_value=True):
            await admin_events.notify_consent_given(bot, max_user_id=42)
        bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_subscribed_suppressed_when_toggle_disabled(self) -> None:
        from aemr_bot.services import admin_events

        bot = AsyncMock()
        with patch("aemr_bot.services.admin_events.cfg.admin_group_id", 777), \
             patch.object(notify_toggles, "is_enabled", return_value=False):
            await admin_events.notify_broadcast_subscribed(bot, max_user_id=42)
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsubscribed_suppressed_when_toggle_disabled(self) -> None:
        from aemr_bot.services import admin_events

        bot = AsyncMock()
        with patch("aemr_bot.services.admin_events.cfg.admin_group_id", 777), \
             patch.object(notify_toggles, "is_enabled", return_value=False):
            await admin_events.notify_broadcast_unsubscribed(
                bot, max_user_id=42, source="меню",
            )
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_toggle_check_uses_correct_key_per_event(self) -> None:
        """Каждое событие проверяет СВОЙ ключ тумблера, не абы какой."""
        from aemr_bot.services import admin_events

        bot = AsyncMock()
        checked_keys: list[str] = []

        def _fake_is_enabled(key: str) -> bool:
            checked_keys.append(key)
            return True

        with patch("aemr_bot.services.admin_events.cfg.admin_group_id", 777), \
             patch.object(notify_toggles, "is_enabled", side_effect=_fake_is_enabled):
            await admin_events.notify_consent_given(bot, max_user_id=1)
            await admin_events.notify_broadcast_subscribed(bot, max_user_id=2)
            await admin_events.notify_broadcast_unsubscribed(
                bot, max_user_id=3, source="меню",
            )
        assert checked_keys == [
            "admin_notify_consent",
            "admin_notify_subscriptions",
            "admin_notify_subscriptions",
        ]


class TestCriticalLegalEvents:
    """2026-07-09 (находка security-ревью): `notify_consent_revoked` и
    `notify_data_erased` — юридически значимые события (152-ФЗ), НЕ
    подчиняются тумблерам/quiet hours, всегда идут через
    `admin_bus.send(critical=True)`."""

    @pytest.mark.asyncio
    async def test_consent_revoked_passes_critical_true(self) -> None:
        from aemr_bot.services import admin_events

        bot = AsyncMock()
        with patch("aemr_bot.services.admin_events.cfg.admin_group_id", 777), \
             patch(
                 "aemr_bot.services.admin_bus.send", AsyncMock(),
             ) as send_mock:
            await admin_events.notify_consent_revoked(
                bot, max_user_id=42, open_appeal_ids=[1],
            )
        send_mock.assert_awaited_once()
        assert send_mock.await_args.kwargs["critical"] is True

    @pytest.mark.asyncio
    async def test_data_erased_passes_critical_true(self) -> None:
        from aemr_bot.services import admin_events

        bot = AsyncMock()
        with patch("aemr_bot.services.admin_events.cfg.admin_group_id", 777), \
             patch(
                 "aemr_bot.services.admin_bus.send", AsyncMock(),
             ) as send_mock:
            await admin_events.notify_data_erased(
                bot, max_user_id=42, closed_appeal_ids=[1, 2],
            )
        send_mock.assert_awaited_once()
        assert send_mock.await_args.kwargs["critical"] is True

    @pytest.mark.asyncio
    async def test_consent_revoked_not_gated_by_toggle(self) -> None:
        """Даже если бы кто-то попытался проверить тумблер — отзыв
        согласия не должен зависеть от notify_toggles.is_enabled вовсе
        (функция его не вызывает)."""
        from aemr_bot.services import admin_events

        bot = AsyncMock()
        with patch("aemr_bot.services.admin_events.cfg.admin_group_id", 777), \
             patch.object(
                 notify_toggles, "is_enabled",
                 side_effect=AssertionError("не должен вызываться"),
             ):
            await admin_events.notify_consent_revoked(
                bot, max_user_id=42, open_appeal_ids=[],
            )
        # Дошло до реальной отправки, is_enabled не звался.
        bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_consent_given_is_not_critical(self) -> None:
        """Контраст: рутинное согласие (не отзыв) идёт БЕЗ critical —
        подчиняется quiet hours как обычно."""
        from aemr_bot.services import admin_events

        bot = AsyncMock()
        with patch("aemr_bot.services.admin_events.cfg.admin_group_id", 777), \
             patch(
                 "aemr_bot.services.admin_bus.send", AsyncMock(),
             ) as send_mock:
            await admin_events.notify_consent_given(bot, max_user_id=42)
        send_mock.assert_awaited_once()
        assert send_mock.await_args.kwargs["critical"] is False
