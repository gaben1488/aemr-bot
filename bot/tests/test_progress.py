"""Тесты на services/progress — прогресс-карта FSM-воронки.

`render_progress` — pure-функция, тестируется без моков.
`send_or_edit_progress` — async с mock'ом bot.edit_message / send_message.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from aemr_bot.services.progress import (
    _STAGES,
    render_progress,
    send_or_edit_progress,
)


class TestRenderProgressBar:
    def test_first_stage_has_zero_filled(self) -> None:
        text = render_progress(stage="name")
        # ▓░░░░ — 0 заполнено, 5 пусто (мы НА первом шаге, до него ничего)
        assert "░░░░░" in text
        assert "Шаг 1/5" in text

    def test_third_stage(self) -> None:
        text = render_progress(
            stage="address", name="Иван", locality="Елизовское ГП"
        )
        # ▓▓░░░ — 2 заполнено (имя + локалити пройдены)
        assert "▓▓░░░" in text
        assert "Шаг 3/5" in text

    def test_last_stage(self) -> None:
        text = render_progress(
            stage="summary",
            name="Иван", locality="Елизовское ГП",
            address="ул. Ленина, 5", topic="Дороги",
        )
        # ▓▓▓▓░ — 4 заполнено
        assert "▓▓▓▓░" in text
        assert "Шаг 5/5" in text

    def test_unknown_stage_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown stage"):
            render_progress(stage="bogus")  # type: ignore[arg-type]


class TestRenderProgressContent:
    def test_completed_steps_show_value(self) -> None:
        text = render_progress(
            stage="topic", name="Иван", locality="Елизово", address="ул. Ленина, 5"
        )
        # 3 завершённых шага должны иметь ✅ + значение
        assert "✅ Имя: Иван" in text
        assert "✅ Населённый пункт: Елизово" in text
        assert "✅ Адрес: ул. Ленина, 5" in text

    def test_current_step_has_blue_marker_and_prompt(self) -> None:
        text = render_progress(stage="address", name="X", locality="Y")
        # Текущий шаг — 🔵 с подсказкой что делать
        assert "🔵 Адрес —" in text
        assert "улица" in text  # подсказка

    def test_future_steps_have_white_marker_no_value(self) -> None:
        text = render_progress(stage="locality", name="Иван")
        # locality — current; address/topic/summary — future
        # Future = "⚪ Адрес" БЕЗ значения и без подсказки
        assert "⚪ Адрес" in text
        assert "⚪ Тема" in text
        assert "⚪ Суть" in text
        # И никаких подсказок «улица и дом» в future-секциях:
        # text для future — только лейбл
        future_lines = [
            line for line in text.split("\n") if line.startswith("⚪")
        ]
        for line in future_lines:
            assert ":" not in line  # значения не показываем

    def test_empty_value_falls_back_to_dash(self) -> None:
        """Если в dialog_data пусто (например пользователь нажал
        skip) — completed-шаг показывает «—» вместо value."""
        text = render_progress(stage="topic", name="", locality="X", address="Y")
        assert "✅ Имя: —" in text


class TestRenderProgressHeader:
    def test_has_emoji_title(self) -> None:
        text = render_progress(stage="name")
        assert text.startswith("📋 Подача обращения")


class TestSendOrEditProgress:
    """send_or_edit_progress: либо edit, либо новое сообщение."""

    @pytest.mark.asyncio
    async def test_no_existing_mid_sends_new(self) -> None:
        bot = AsyncMock()
        # send_message возвращает SendedMessage-like с body.mid="m-1"
        from types import SimpleNamespace
        bot.send_message.return_value = SimpleNamespace(
            message=SimpleNamespace(body=SimpleNamespace(mid="m-1"))
        )

        mid, edited = await send_or_edit_progress(
            bot,
            chat_id=42,
            dialog_data={},  # нет progress_message_id
            text="step 1",
            attachments=[],
        )
        bot.edit_message.assert_not_called()
        bot.send_message.assert_called_once()
        assert mid == "m-1"
        assert edited is False

    @pytest.mark.asyncio
    async def test_existing_mid_edits(self) -> None:
        bot = AsyncMock()
        bot.edit_message.return_value = None  # успех

        mid, edited = await send_or_edit_progress(
            bot,
            chat_id=42,
            dialog_data={"progress_message_id": "m-old"},
            text="step 2",
            attachments=[],
        )
        bot.edit_message.assert_called_once_with(
            message_id="m-old", text="step 2", attachments=[]
        )
        bot.send_message.assert_not_called()
        assert mid == "m-old"
        assert edited is True

    @pytest.mark.asyncio
    async def test_edit_failure_falls_back_to_new(self) -> None:
        bot = AsyncMock()
        bot.edit_message.side_effect = RuntimeError("API rate limit")
        from types import SimpleNamespace
        bot.send_message.return_value = SimpleNamespace(
            message=SimpleNamespace(body=SimpleNamespace(mid="m-new"))
        )

        mid, edited = await send_or_edit_progress(
            bot,
            chat_id=42,
            dialog_data={"progress_message_id": "m-old"},
            text="step 3",
            attachments=[],
        )
        bot.edit_message.assert_called_once()
        bot.send_message.assert_called_once()
        assert mid == "m-new"
        assert edited is False

    @pytest.mark.asyncio
    async def test_send_failure_returns_none(self) -> None:
        """Совсем нет связи с MAX — возвращаем (None, False) без crash."""
        bot = AsyncMock()
        bot.send_message.side_effect = RuntimeError("network down")

        mid, edited = await send_or_edit_progress(
            bot, chat_id=42, dialog_data={}, text="step", attachments=[]
        )
        assert mid is None
        assert edited is False


class TestStagesIntegrity:
    def test_five_stages_in_order(self) -> None:
        """Регрессия: порядок шагов — name, locality, address, topic, summary.
        Если кто-то изменит этот порядок — прогресс-бар сбросится по логике
        в render_progress."""
        assert _STAGES == ("name", "locality", "address", "topic", "summary")
