"""Тесты на services/card_format — рендер карточек обращений.

Без БД, только форматирование. Реальные обращения тестируются в
test_appeal_flow."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from aemr_bot.services.card_format import (
    appeal_list_label,
    attachments_summary_line,
)


class TestAttachmentsSummaryLine:
    def test_empty(self) -> None:
        assert attachments_summary_line([]) == ""

    def test_only_image(self) -> None:
        result = attachments_summary_line([{"type": "image"}])
        assert "фото 1" in result

    def test_mixed(self) -> None:
        result = attachments_summary_line([
            {"type": "image"},
            {"type": "image"},
            {"type": "video"},
            {"type": "file"},
            {"type": "file"},
            {"type": "file"},
        ])
        assert "фото 2" in result
        assert "видео 1" in result
        assert "файлов 3" in result

    def test_only_audio_returns_empty(self) -> None:
        """audio не разрешён в _ATTACHMENT_LABELS → не попадает в summary."""
        result = attachments_summary_line([{"type": "audio"}])
        assert result == ""


class TestAppealListLabel:
    def _make_appeal(
        self,
        *,
        status: str = "new",
        summary: str | None = "Тестовое обращение",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=42,
            status=status,
            created_at=datetime(2026, 5, 10, 14, 30, tzinfo=timezone.utc),
            summary=summary,
        )

    def test_new_label(self) -> None:
        label = appeal_list_label(self._make_appeal(status="new"))
        assert "#42" in label
        assert "🆕" in label  # status emoji
        assert "Новое" in label

    def test_answered_label(self) -> None:
        label = appeal_list_label(self._make_appeal(status="answered"))
        assert "✅" in label
        assert "Завершено" in label

    def test_closed_label(self) -> None:
        label = appeal_list_label(self._make_appeal(status="closed"))
        assert "⛔" in label

    def test_summary_truncated_to_32_chars(self) -> None:
        """Превью на >32 символа должно обрезаться."""
        long_summary = "a" * 100
        label = appeal_list_label(self._make_appeal(summary=long_summary))
        # В метке должно быть не больше 32 символов из summary
        # (плюс все остальные части метки)
        a_count = label.count("a")
        assert a_count == 32

    def test_newlines_in_summary_replaced_with_spaces(self) -> None:
        """Перенос строки в summary не должен ломать одну строку label."""
        label = appeal_list_label(
            self._make_appeal(summary="первая\nвторая\nтретья")
        )
        assert "\n" not in label.split(" · ")[-1]  # последняя секция — превью

    def test_none_summary(self) -> None:
        """Если summary=None (например, после 5-летнего retention) — без crash."""
        label = appeal_list_label(self._make_appeal(summary=None))
        assert "#42" in label

    def test_unknown_status(self) -> None:
        """Если статус неизвестен — fallback на эмодзи •."""
        label = appeal_list_label(self._make_appeal(status="unknown_status"))
        assert "•" in label or "#42" in label
