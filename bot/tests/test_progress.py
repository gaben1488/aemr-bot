"""Тесты на services/progress — прогресс-карта FSM-воронки.

`render_progress` — pure-функция, тестируется без моков.
`send_or_edit_progress` — async с mock'ом bot.edit_message / send_message.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aemr_bot.services.progress import (
    _STAGES,
    render_progress,
    send_or_edit_progress,
)


class TestRenderProgressCounter:
    def test_first_stage_counter_layout(self) -> None:
        text = render_progress(stage="name")
        assert "<code>1 / 5</code>" in text
        assert "🟦⬜⬜⬜⬜" not in text
        assert "▶ <b>Имя</b>" in text

    def test_third_stage_counter_layout(self) -> None:
        text = render_progress(
            stage="address", name="Иван", locality="Елизовское ГП"
        )
        assert "<code>3 / 5</code>" in text
        assert "🟢🟢🟦⬜⬜" not in text
        assert "▶ <b>Адрес</b>" in text

    def test_last_stage_counter_layout(self) -> None:
        text = render_progress(
            stage="summary",
            name="Иван", locality="Елизовское ГП",
            address="ул. Ленина, 5", topic="Дороги",
        )
        assert "<code>5 / 5</code>" in text
        assert "🟢🟢🟢🟢🟦" not in text
        assert "▶ <b>Суть</b>" in text

    def test_unknown_stage_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown stage"):
            render_progress(stage="bogus")  # type: ignore[arg-type]


class TestRenderProgressContent:
    def test_completed_steps_bold_value(self) -> None:
        text = render_progress(
            stage="topic", name="Иван", locality="Елизово", address="ул. Ленина, 5"
        )
        # Завершённые шаги — ✓ + label + · + <b>value</b>
        assert "✓ Имя · <b>Иван</b>" in text
        assert "✓ Населённый пункт · <b>Елизово</b>" in text
        assert "✓ Адрес · <b>ул. Ленина, 5</b>" in text

    def test_current_step_blockquote_prompt(self) -> None:
        text = render_progress(stage="address", name="X", locality="Y")
        # Текущий шаг — ▶ <b>label</b> + <blockquote>prompt</blockquote>
        assert "▶ <b>Адрес</b>" in text
        assert "<blockquote>" in text
        assert "улица" in text  # подсказка внутри blockquote

    def test_future_steps_are_not_rendered(self) -> None:
        text = render_progress(stage="locality", name="Иван")
        # Новый компактный UX: будущие этапы не показываются, чтобы не
        # перегружать экран. Остаются только завершённые шаги, текущий
        # шаг и короткий счётчик 2 / 5.
        assert "<code>2 / 5</code>" in text
        assert "✓ Имя · <b>Иван</b>" in text
        assert "▶ <b>Населённый пункт</b>" in text
        assert "○ Адрес" not in text
        assert "○ Тема" not in text
        assert "○ Суть" not in text

    def test_empty_value_falls_back_to_dash(self) -> None:
        """Если в dialog_data пусто (skip / пропуск) — completed-шаг
        показывает «—» вместо value."""
        text = render_progress(stage="topic", name="", locality="X", address="Y")
        assert "✓ Имя · <b>—</b>" in text


class TestRenderProgressEscape:
    """HTML-escape значений жителя — защита от поломки парсинга."""

    def test_escapes_angle_brackets_in_name(self) -> None:
        text = render_progress(stage="locality", name="<script>alert(1)</script>")
        # Тег НЕ должен попасть в финальный текст как есть
        assert "<script>" not in text
        assert "&lt;script&gt;" in text

    def test_escapes_ampersand_in_address(self) -> None:
        text = render_progress(
            stage="topic", name="Иван", locality="Елизово",
            address="ул. AT&T, 5",
        )
        assert "AT&amp;T" in text
        # А наши собственные HTML-теги остаются — render не трогает шаблон
        assert "<b>" in text

    def test_strip_whitespace_in_value(self) -> None:
        text = render_progress(stage="locality", name="  Иван  ")
        assert "<b>Иван</b>" in text


class TestRenderProgressHeader:
    def test_has_emoji_title_with_bold(self) -> None:
        text = render_progress(stage="name")
        assert text.startswith("📋 <b>Подача обращения</b>")


class TestSendOrEditProgress:
    """send_or_edit_progress: либо edit, либо новое сообщение, всегда с format."""

    @pytest.mark.asyncio
    async def test_no_existing_mid_sends_new(self) -> None:
        bot = AsyncMock()
        bot.send_message.return_value = SimpleNamespace(
            message=SimpleNamespace(body=SimpleNamespace(mid="m-1"))
        )

        mid, edited = await send_or_edit_progress(
            bot,
            chat_id=42,
            dialog_data={},
            text="step 1",
            attachments=[],
        )
        bot.edit_message.assert_not_called()
        bot.send_message.assert_called_once()
        # format-параметр должен быть передан
        kwargs = bot.send_message.call_args.kwargs
        assert kwargs["chat_id"] == 42
        assert kwargs["user_id"] is None
        assert "format" in kwargs
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
        bot.edit_message.assert_called_once()
        kwargs = bot.edit_message.call_args.kwargs
        assert kwargs["message_id"] == "m-old"
        assert kwargs["text"] == "step 2"
        assert kwargs["attachments"] == []
        assert "format" in kwargs
        bot.send_message.assert_not_called()
        assert mid == "m-old"
        assert edited is True

    @pytest.mark.asyncio
    async def test_force_new_message_skips_edit_even_with_existing_mid(self) -> None:
        bot = AsyncMock()
        bot.send_message.return_value = SimpleNamespace(
            message=SimpleNamespace(body=SimpleNamespace(mid="m-forced"))
        )

        mid, edited = await send_or_edit_progress(
            bot,
            chat_id=42,
            dialog_data={"progress_message_id": "m-old"},
            text="step after geo",
            attachments=[],
            force_new_message=True,
        )

        bot.edit_message.assert_not_called()
        bot.send_message.assert_called_once()
        assert mid == "m-forced"
        assert edited is False

    @pytest.mark.asyncio
    async def test_callback_without_chat_id_sends_by_user_id(self) -> None:
        """Регрессия geo-confirm: MessageCallback в личке может прийти без chat_id.

        В этом случае после нажатия «✅ Всё правильно» состояние уже
        меняется, но новая карточка с темами должна уйти по user_id, а
        не молча потеряться из-за chat_id=None.
        """
        bot = AsyncMock()
        bot.send_message.return_value = SimpleNamespace(
            message=SimpleNamespace(body=SimpleNamespace(mid="m-user"))
        )

        mid, edited = await send_or_edit_progress(
            bot,
            chat_id=None,
            user_id=777,
            dialog_data={},
            text="step after geo confirm",
            attachments=[],
        )

        bot.send_message.assert_called_once()
        kwargs = bot.send_message.call_args.kwargs
        assert kwargs["chat_id"] is None
        assert kwargs["user_id"] == 777
        assert mid == "m-user"
        assert edited is False

    @pytest.mark.asyncio
    async def test_missing_chat_and_user_id_returns_none_without_send(self) -> None:
        bot = AsyncMock()

        mid, edited = await send_or_edit_progress(
            bot,
            chat_id=None,
            user_id=None,
            dialog_data={},
            text="step",
            attachments=[],
        )

        bot.send_message.assert_not_called()
        assert mid is None
        assert edited is False

    @pytest.mark.asyncio
    async def test_edit_failure_falls_back_to_new(self) -> None:
        bot = AsyncMock()
        bot.edit_message.side_effect = RuntimeError("API rate limit")
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
        Если кто-то изменит этот порядок — счётчик этапов собьётся по
        логике в render_progress."""
        assert _STAGES == ("name", "locality", "address", "topic", "summary")
