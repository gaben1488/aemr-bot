"""Тесты на timeline-блок (полная переписка) в карточках обращения.

Запрос владельца: «карточки должны содержать ПОЛНУЮ информацию —
содержание обращения - ответ - в случае если ответов и дополнений
несколько - чтобы они также содержались в карточке».

Реализовано в `services/card_format.appeal_timeline_block` (для
admin) и `user_appeal_timeline_block` (для жителя). Здесь — контракт
на формат и порядок.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("maxapi", reason="нужен для config/texts")


def _make_msg(direction: str, text: str, *, minutes_offset: int = 0,
              attachments=None) -> SimpleNamespace:
    return SimpleNamespace(
        direction=direction,
        text=text,
        attachments=attachments or [],
        created_at=datetime(2026, 5, 21, 14, 0, tzinfo=timezone.utc)
        + timedelta(minutes=minutes_offset),
    )


def _make_appeal_with_messages(messages: list) -> SimpleNamespace:
    """Appeal с предзагруженными messages (как после selectinload)."""
    appeal = SimpleNamespace(
        id=42,
        status="answered",
        locality="Елизовское ГП",
        address="ул. Ленина, 5",
        topic="Дороги",
        summary="Яма во дворе.",
        attachments=[],
        created_at=datetime(2026, 5, 21, 13, 0, tzinfo=timezone.utc),
    )
    # _loaded_messages читает из __dict__, поэтому ставим напрямую.
    appeal.__dict__["messages"] = messages
    return appeal


class TestAdminTimeline:
    def test_no_messages_no_block(self) -> None:
        from aemr_bot.services.card_format import appeal_timeline_block

        appeal = _make_appeal_with_messages([])
        assert appeal_timeline_block(appeal) == ""

    def test_only_followups_falls_back_to_old_block(self) -> None:
        """Если только дополнения жителя (нет ответов оператора) —
        старый формат «Дополнения к обращению» без timeline-маркеров."""
        from aemr_bot.services.card_format import appeal_timeline_block

        appeal = _make_appeal_with_messages([
            _make_msg("from_user", "Уточнение по фото"),
        ])
        result = appeal_timeline_block(appeal)
        # Старый формат: «Дополнение к обращению:» без «История переписки»
        assert "Дополнение к обращению:" in result
        assert "История переписки:" not in result

    def test_reply_present_uses_timeline_format(self) -> None:
        """Как только появился ответ оператора — переключаемся на
        timeline («История переписки»)."""
        from aemr_bot.services.card_format import appeal_timeline_block

        appeal = _make_appeal_with_messages([
            _make_msg("from_user", "Уточнение", minutes_offset=0),
            _make_msg("from_operator", "Принято в работу", minutes_offset=5),
        ])
        result = appeal_timeline_block(appeal)
        assert "История переписки:" in result
        assert "📩 Дополнение жителя" in result
        assert "📨 Ответ оператора" in result
        # Хронология: дополнение раньше ответа
        assert result.index("Уточнение") < result.index("Принято в работу")

    def test_timeline_orders_chronologically(self) -> None:
        from aemr_bot.services.card_format import appeal_timeline_block

        appeal = _make_appeal_with_messages([
            _make_msg("from_operator", "Второй ответ", minutes_offset=30),
            _make_msg("from_user", "Первое дополнение", minutes_offset=0),
            _make_msg("from_operator", "Первый ответ", minutes_offset=10),
            _make_msg("from_user", "Второе дополнение", minutes_offset=20),
        ])
        result = appeal_timeline_block(appeal)
        # Порядок: 0 → 10 → 20 → 30
        idx1 = result.index("Первое дополнение")
        idx2 = result.index("Первый ответ")
        idx3 = result.index("Второе дополнение")
        idx4 = result.index("Второй ответ")
        assert idx1 < idx2 < idx3 < idx4

    def test_caps_at_10_with_hidden_note(self) -> None:
        """Не больше 10 сообщений; остальные — «Ранее N сообщений»."""
        from aemr_bot.services.card_format import appeal_timeline_block

        msgs = [
            _make_msg(
                "from_user" if i % 2 == 0 else "from_operator",
                f"Сообщение {i}", minutes_offset=i,
            )
            for i in range(15)
        ]
        appeal = _make_appeal_with_messages(msgs)
        result = appeal_timeline_block(appeal)
        assert "Ранее ещё 5 сообщений" in result
        # Старые (0-4) скрыты, новые (5-14) видны
        assert "Сообщение 0" not in result
        assert "Сообщение 14" in result


class TestUserTimeline:
    def test_uses_user_friendly_markers(self) -> None:
        """Житель видит «Ваше дополнение» и «Ответ Администрации»
        вместо служебных маркеров."""
        from aemr_bot.services.card_format import user_appeal_timeline_block

        appeal = _make_appeal_with_messages([
            _make_msg("from_user", "Моё уточнение", minutes_offset=0),
            _make_msg("from_operator", "Спасибо, разберёмся", minutes_offset=5),
        ])
        result = user_appeal_timeline_block(appeal)
        assert "📩 Ваше дополнение" in result
        assert "📨 Ответ Администрации" in result
        # Служебные маркеры не должны попадать
        assert "Дополнение жителя" not in result
        assert "Ответ оператора" not in result

    def test_user_timeline_when_only_followups(self) -> None:
        """Житель видит timeline даже если ответов ещё нет — он же
        отправил дополнения, должен их видеть в карточке."""
        from aemr_bot.services.card_format import user_appeal_timeline_block

        appeal = _make_appeal_with_messages([
            _make_msg("from_user", "Моё дополнение"),
        ])
        result = user_appeal_timeline_block(appeal)
        assert "История переписки:" in result
        assert "Ваше дополнение" in result

    def test_user_timeline_empty_messages_returns_empty(self) -> None:
        from aemr_bot.services.card_format import user_appeal_timeline_block

        appeal = _make_appeal_with_messages([])
        assert user_appeal_timeline_block(appeal) == ""


class TestCardIntegration:
    """Проверка что admin_card и user_card подключают timeline."""

    def test_admin_card_includes_timeline_when_reply_exists(self) -> None:
        from aemr_bot.services.card_format import admin_card

        user = SimpleNamespace(
            first_name="Сергей",
            phone="+79991234567",
            is_blocked=False,
            consent_pdn_at=None,
            consent_revoked_at=None,
            subscribed_broadcast=False,
        )
        appeal = _make_appeal_with_messages([
            _make_msg("from_user", "Уточнение", minutes_offset=0),
            _make_msg("from_operator", "Принято", minutes_offset=10),
        ])
        appeal.user = user
        result = admin_card(appeal, user)
        assert "История переписки:" in result
        assert "📨 Ответ оператора" in result
        assert "Принято" in result

    def test_user_card_includes_timeline_when_reply_exists(self) -> None:
        from aemr_bot.services.card_format import user_card

        appeal = _make_appeal_with_messages([
            _make_msg("from_user", "Мой followup", minutes_offset=0),
            _make_msg("from_operator", "Ответ", minutes_offset=5),
        ])
        result = user_card(appeal)
        assert "История переписки:" in result
        assert "📨 Ответ Администрации" in result
