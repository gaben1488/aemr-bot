"""Тесты на services/card_format — рендер карточек обращений.

Без БД, только форматирование. Реальные обращения тестируются в
test_appeal_flow."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from aemr_bot.services.card_format import (
    admin_card,
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


class TestAdminCard:
    def test_shows_user_followup_inside_card(self) -> None:
        appeal = SimpleNamespace(
            id=18,
            locality="Елизовское ГП",
            address="ул. Ленина, 5",
            topic="Дороги",
            summary="Яма во дворе.",
            attachments=[],
            messages=[
                SimpleNamespace(
                    direction="from_user",
                    text="Уточнение: яма у второго подъезда.",
                    attachments=[],
                )
            ],
        )
        user = SimpleNamespace(first_name="Сергей", phone="+79991234567")

        result = admin_card(appeal, user)

        assert "Суть:" in result
        assert "Яма во дворе." in result
        assert "Дополнение к обращению:" in result
        assert "яма у второго подъезда" in result


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

    # ---- даты ответа/закрытия (PR E) ----

    def test_answered_shows_response_date(self) -> None:
        """Контракт: для answered — показывается ДАТА ОТВЕТА (не только
        создания), чтобы житель видел «когда мне ответили»."""
        appeal = SimpleNamespace(
            id=42,
            status="answered",
            created_at=datetime(2026, 5, 10, 14, 30, tzinfo=timezone.utc),
            answered_at=datetime(2026, 5, 11, 9, 15, tzinfo=timezone.utc),
            closed_at=None,
            summary="Тест",
        )
        label = appeal_list_label(appeal)
        # 11.05 — день ответа в локальном времени Камчатки (UTC+12)
        assert "11.05" in label, (
            f"дата ответа не отображена в метке: {label}"
        )

    def test_closed_shows_closure_date(self) -> None:
        """Контракт: для closed — показывается ДАТА ЗАКРЫТИЯ."""
        appeal = SimpleNamespace(
            id=42,
            status="closed",
            created_at=datetime(2026, 5, 10, 14, 30, tzinfo=timezone.utc),
            answered_at=None,
            closed_at=datetime(2026, 5, 12, 16, 0, tzinfo=timezone.utc),
            summary="Тест",
        )
        label = appeal_list_label(appeal)
        # 13.05 в Камчатке (UTC+12 даст +12ч от UTC 16:00 → 13.05 04:00 след. дня)
        assert "13.05" in label, (
            f"дата закрытия не отображена в метке: {label}"
        )

    def test_new_still_shows_created_date(self) -> None:
        """Regression: для new/in_progress дата создания остаётся."""
        appeal = SimpleNamespace(
            id=42,
            status="new",
            created_at=datetime(2026, 5, 10, 14, 30, tzinfo=timezone.utc),
            answered_at=None,
            closed_at=None,
            summary="Тест",
        )
        label = appeal_list_label(appeal)
        # 11.05 в Камчатке (UTC+12 от 14:30 → 02:30 след. дня)
        assert "11.05" in label


class TestAdminCardCitizenStateMarkers:
    """PR F: admin appeal card показывает маркеры состояния жителя —
    подписка, согласие, блокировка. Оператор видит контекст «нормальный
    житель» vs «отозвавший согласие, в работе» vs «заблокированный»."""

    def _make_appeal(self) -> SimpleNamespace:
        return SimpleNamespace(
            id=18,
            locality="Елизовское ГП",
            address="ул. Ленина, 5",
            topic="Дороги",
            summary="Яма во дворе.",
            attachments=[],
            messages=[],
        )

    def test_normal_user_subscribed_consent_active(self) -> None:
        """Обычный житель: подписан, согласие активно — показывается
        компактная строка статуса с маркерами."""
        appeal = self._make_appeal()
        user = SimpleNamespace(
            first_name="Сергей",
            phone="+79991234567",
            consent_pdn_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            consent_revoked_at=None,
            subscribed_broadcast=True,
            is_blocked=False,
        )

        result = admin_card(appeal, user)

        # Подписан — маркер 🔔
        assert "🔔" in result, f"маркер подписки не найден:\n{result}"
        # Согласие активно — маркер ✅
        assert "✅" in result, f"маркер согласия не найден:\n{result}"

    def test_blocked_user_visible(self) -> None:
        """Заблокированный житель — явный маркер 🚫."""
        appeal = self._make_appeal()
        user = SimpleNamespace(
            first_name="Иван",
            phone="+79990000000",
            consent_pdn_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            consent_revoked_at=None,
            subscribed_broadcast=False,
            is_blocked=True,
        )

        result = admin_card(appeal, user)
        assert "🚫" in result, f"маркер блокировки не найден:\n{result}"

    def test_revoked_consent_visible(self) -> None:
        """Отозвавший согласие житель (обращение всё ещё в работе для
        финального ответа) — маркер 🔁."""
        appeal = self._make_appeal()
        user = SimpleNamespace(
            first_name="Анна",
            phone="+79991111111",
            consent_pdn_at=None,
            consent_revoked_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
            subscribed_broadcast=False,
            is_blocked=False,
        )

        result = admin_card(appeal, user)
        assert "🔁" in result, f"маркер отозванного согласия не найден:\n{result}"

    def test_not_subscribed_marker(self) -> None:
        """Житель не подписан на рассылку — маркер 🔕."""
        appeal = self._make_appeal()
        user = SimpleNamespace(
            first_name="Пётр",
            phone="+79992222222",
            consent_pdn_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            consent_revoked_at=None,
            subscribed_broadcast=False,
            is_blocked=False,
        )

        result = admin_card(appeal, user)
        assert "🔕" in result, f"маркер «не подписан» не найден:\n{result}"
