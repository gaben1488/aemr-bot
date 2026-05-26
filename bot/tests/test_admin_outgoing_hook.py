"""Тесты для `admin_bus.install_outgoing_tracker_hook` — sacred-инвариант
«каждый исходящий в admin chat двигает tracker».

Закрывает архитектурную дыру из жалобы владельца 2026-05-27: «меню в
админ-чате редактируется при тапе на не-последнем сообщении». Без
hook'а 62 прямых `bot.send_message(chat_id=admin_group_id, ...)` в
коде оставляли tracker устаревшим — freshness rule ошибочно edit'ил
карточки вверху чата.

С hook'ом — любой direct send в admin chat автоматически двигает
tracker, независимо от того, идёт ли он через admin_bus.send,
admin_card.render или один из 60 ad-hoc вызовов в handlers.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


ADMIN_CHAT_ID = 555


def _make_bot(send_mids: list[str] | None = None):
    """Bot со стримом mid'ов для последовательных send'ов."""
    sequence = list(send_mids or ["m-1"])

    def _next_send(*args, **kwargs):
        mid = sequence.pop(0) if sequence else f"m-extra-{len(sequence)}"
        return SimpleNamespace(
            message=SimpleNamespace(body=SimpleNamespace(mid=mid))
        )

    return SimpleNamespace(
        send_message=AsyncMock(side_effect=_next_send),
    )


@pytest.fixture(autouse=True)
def _clean_tracker():
    from aemr_bot.utils import menu_tracker
    menu_tracker.clear_all()
    yield
    menu_tracker.clear_all()


class TestInstallHookIdempotent:
    """Двойной install на тот же bot — не должен оборачивать дважды.
    Иначе каждое сообщение прошло бы tracker.set N раз и любая ошибка
    в одной обёртке поломала бы все следующие."""

    @pytest.mark.asyncio
    async def test_double_install_no_double_wrap(self) -> None:
        from aemr_bot.services import admin_bus

        bot = _make_bot(send_mids=["m-1"])
        with patch("aemr_bot.config.settings.admin_group_id", ADMIN_CHAT_ID):
            admin_bus.install_outgoing_tracker_hook(bot)
            wrapper_after_first = bot.send_message
            admin_bus.install_outgoing_tracker_hook(bot)  # повторный
            wrapper_after_second = bot.send_message

        # Двойной install — no-op, send_message указывает на тот же
        # wrapper. Иначе указатель бы менялся → tracker.set N раз и
        # любая ошибка в одной обёртке поломала бы следующие.
        assert wrapper_after_first is wrapper_after_second

    @pytest.mark.asyncio
    async def test_no_op_without_admin_group_id(self) -> None:
        """Без ADMIN_GROUP_ID hook не устанавливается (citizen-only)."""
        from aemr_bot.services import admin_bus

        bot = _make_bot()
        with patch("aemr_bot.config.settings.admin_group_id", 0):
            admin_bus.install_outgoing_tracker_hook(bot)
            # send_message не обёрнут — это AsyncMock from _make_bot.
            assert not getattr(
                bot, "_aemr_admin_outgoing_tracker_installed", False
            )


class TestHookSyncsAdminTracker:
    """Главный sacred-контракт: send в admin chat обновляет tracker."""

    @pytest.mark.asyncio
    async def test_admin_send_updates_tracker(self) -> None:
        from aemr_bot.services import admin_bus
        from aemr_bot.utils import menu_tracker

        bot = _make_bot(send_mids=["fresh-1"])
        with patch("aemr_bot.config.settings.admin_group_id", ADMIN_CHAT_ID):
            admin_bus.install_outgoing_tracker_hook(bot)
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text="hello admin")

        assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) == "fresh-1"

    @pytest.mark.asyncio
    async def test_non_admin_send_does_not_touch_admin_tracker(self) -> None:
        """Send в чат жителя не должен трогать admin_tracker.
        Citizen-chat имеет свой per-chat tracker через
        `_send_or_edit_menu`, его hook не управляет."""
        from aemr_bot.services import admin_bus
        from aemr_bot.utils import menu_tracker

        # Имитируем: admin tracker уже = "menu-old".
        menu_tracker.set_last_menu_mid(ADMIN_CHAT_ID, "menu-old")

        bot = _make_bot(send_mids=["citizen-msg-1"])
        with patch("aemr_bot.config.settings.admin_group_id", ADMIN_CHAT_ID):
            admin_bus.install_outgoing_tracker_hook(bot)
            await bot.send_message(user_id=42, text="hello citizen")

        # admin tracker не двинулся — это send в личку жителя, не в admin.
        assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) == "menu-old"

    @pytest.mark.asyncio
    async def test_send_failure_does_not_pollute_tracker(self) -> None:
        """Если bot.send_message бросает Exception — tracker не двигаем.
        Caller получает исключение как обычно."""
        from aemr_bot.services import admin_bus
        from aemr_bot.utils import menu_tracker

        # Pre-state: tracker = "old".
        menu_tracker.set_last_menu_mid(ADMIN_CHAT_ID, "old")

        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=RuntimeError("MAX 500")),
        )
        with patch("aemr_bot.config.settings.admin_group_id", ADMIN_CHAT_ID):
            admin_bus.install_outgoing_tracker_hook(bot)
            with pytest.raises(RuntimeError, match="MAX 500"):
                await bot.send_message(chat_id=ADMIN_CHAT_ID, text="x")

        # Tracker не двинулся — send упал до того как мы извлекли mid.
        assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) == "old"

    @pytest.mark.asyncio
    async def test_hook_works_via_admin_bus_send(self) -> None:
        """`admin_bus.send` сам делает set_last_menu_mid — после hook'а
        это становится двойным sync, что идемпотентно (один и тот же mid)."""
        from aemr_bot.services import admin_bus
        from aemr_bot.utils import menu_tracker

        bot = _make_bot(send_mids=["bus-mid-1"])
        with patch("aemr_bot.config.settings.admin_group_id", ADMIN_CHAT_ID):
            admin_bus.install_outgoing_tracker_hook(bot)
            mid = await admin_bus.send(bot, text="from bus")

        assert mid == "bus-mid-1"
        # Tracker = bus-mid-1, неважно через какой путь он был set.
        assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) == "bus-mid-1"
