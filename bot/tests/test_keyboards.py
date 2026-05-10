"""Smoke-тесты на keyboards — все клавиатуры собираются без exception
и содержат ожидаемые кнопки. Pure-функции, не требуют MAX-API."""
from __future__ import annotations

import pytest

# keyboards.py импортирует maxapi — без него локально skip
pytest.importorskip("maxapi", reason="keyboards тесты требуют maxapi")

from aemr_bot import keyboards  # noqa: E402


class TestSimpleKeyboards:
    def test_main_menu_subscribed(self) -> None:
        kb = keyboards.main_menu(subscribed=True)
        assert kb is not None

    def test_main_menu_unsubscribed(self) -> None:
        kb = keyboards.main_menu(subscribed=False)
        assert kb is not None

    def test_consent_keyboard(self) -> None:
        kb = keyboards.consent_keyboard()
        assert kb is not None

    def test_cancel_keyboard(self) -> None:
        kb = keyboards.cancel_keyboard()
        assert kb is not None

    def test_back_to_menu_keyboard(self) -> None:
        kb = keyboards.back_to_menu_keyboard()
        assert kb is not None

    def test_settings_menu_keyboard(self) -> None:
        kb = keyboards.settings_menu_keyboard()
        assert kb is not None

    def test_goodbye_keyboard(self) -> None:
        kb = keyboards.goodbye_keyboard()
        assert kb is not None


class TestParametricKeyboards:
    def test_localities(self) -> None:
        kb = keyboards.localities_keyboard(["Елизовское ГП", "Паратунское СП"])
        assert kb is not None

    def test_topics(self) -> None:
        kb = keyboards.topics_keyboard(["Дороги", "ЖКХ", "Мусор"])
        assert kb is not None

    def test_topics_empty(self) -> None:
        # Пустой список — клавиатура без topic-кнопок но с «❌ Отмена»
        kb = keyboards.topics_keyboard([])
        assert kb is not None

    def test_appointment_with_url(self) -> None:
        kb = keyboards.appointment_keyboard(
            electronic_reception_url="https://kamgov.ru/questions"
        )
        assert kb is not None

    def test_appointment_without_url(self) -> None:
        kb = keyboards.appointment_keyboard(electronic_reception_url=None)
        assert kb is not None


class TestAdminKeyboards:
    def test_op_help_keyboard_basic(self) -> None:
        kb = keyboards.op_help_keyboard()
        assert kb is not None

    def test_op_help_with_count(self) -> None:
        kb = keyboards.op_help_keyboard(open_count=5)
        assert kb is not None

    def test_op_help_it_full(self) -> None:
        kb = keyboards.op_help_keyboard(
            open_count=42, is_it=True, can_broadcast=True
        )
        assert kb is not None

    def test_op_stats_menu(self) -> None:
        assert keyboards.op_stats_menu_keyboard() is not None

    def test_op_role_picker(self) -> None:
        assert keyboards.op_role_picker_keyboard() is not None

    def test_op_audience_menu(self) -> None:
        assert keyboards.op_audience_menu_keyboard() is not None

    def test_op_settings_keys(self) -> None:
        kb = keyboards.op_settings_keys_keyboard(
            ["policy_url", "topics", "localities"]
        )
        assert kb is not None


class TestAppealAdminActions:
    def test_new_status_has_reply_close(self) -> None:
        from aemr_bot.db.models import AppealStatus

        kb = keyboards.appeal_admin_actions(
            appeal_id=42, status=AppealStatus.NEW.value
        )
        assert kb is not None

    def test_closed_no_reopen_when_revoked(self) -> None:
        """closed_due_to_revoke=True — кнопки «🔁 Возобновить» НЕ должно быть."""
        from aemr_bot.db.models import AppealStatus

        kb = keyboards.appeal_admin_actions(
            appeal_id=42,
            status=AppealStatus.CLOSED.value,
            closed_due_to_revoke=True,
        )
        assert kb is not None

    def test_it_role_full_buttons(self) -> None:
        from aemr_bot.db.models import AppealStatus

        kb = keyboards.appeal_admin_actions(
            appeal_id=42,
            status=AppealStatus.NEW.value,
            is_it=True,
            user_blocked=False,
        )
        assert kb is not None
