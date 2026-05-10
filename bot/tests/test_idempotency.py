"""Тесты на services/idempotency.build_idempotency_key — pure-логика
сбора ключа из MAX events. claim() требует БД и тестируется через
интеграционные сценарии (не здесь)."""
from __future__ import annotations

from types import SimpleNamespace

from aemr_bot.services.idempotency import build_idempotency_key


def _ev(**kwargs) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


class TestBuildIdempotencyKey:
    def test_callback_with_id(self) -> None:
        event = _ev(
            update_type="message_callback",
            callback=_ev(callback_id="cb-abc-123"),
        )
        key = build_idempotency_key(event)
        assert key is not None
        assert "cb=cb-abc-123" in key
        assert "message_callback" in key

    def test_message_with_mid_and_seq(self) -> None:
        event = _ev(
            update_type="message_created",
            callback=None,
            message=_ev(body=_ev(mid="m-1", seq=42), timestamp=None),
            timestamp=1000,
        )
        key = build_idempotency_key(event)
        assert key is not None
        assert "mid=m-1" in key
        assert "seq=42" in key
        assert "ts=1000" in key

    def test_chat_and_user_appended(self) -> None:
        event = _ev(
            update_type="x",
            callback=None,
            message=None,
            timestamp=None,
            chat_id=12345,
            user=_ev(user_id=99),
        )
        key = build_idempotency_key(event)
        assert key is not None
        assert "chat=12345" in key
        assert "user=99" in key

    def test_only_update_type_returns_none(self) -> None:
        """Без идентифицирующих полей — ключ собрать нельзя.
        Возвращаем None, чтобы вызывающий мог обработать событие
        (лучше обработать дубль, чем потерять вовсе)."""
        event = _ev(
            update_type="empty",
            callback=None,
            message=None,
            timestamp=None,
            chat_id=None,
            user=None,
        )
        assert build_idempotency_key(event) is None

    def test_long_key_truncated(self) -> None:
        """Если ключ выходит длиннее MAX_KEY_LENGTH — обрезается."""
        from aemr_bot.services.idempotency import MAX_KEY_LENGTH

        long_cb_id = "x" * (MAX_KEY_LENGTH + 100)
        event = _ev(
            update_type="message_callback",
            callback=_ev(callback_id=long_cb_id),
        )
        key = build_idempotency_key(event)
        assert key is not None
        assert len(key) == MAX_KEY_LENGTH

    def test_class_name_fallback_when_no_update_type(self) -> None:
        """Если update_type отсутствует — берём __class__.__name__."""

        class FakeUpdate:
            callback = None
            message = None
            timestamp = 999
            chat_id = 1

        event = FakeUpdate()
        key = build_idempotency_key(event)
        assert key is not None
        assert "FakeUpdate" in key

    def test_partial_message_body(self) -> None:
        """body есть, но без mid/seq — таймстамп всё равно ловится."""
        event = _ev(
            update_type="msg",
            callback=None,
            message=_ev(body=_ev(mid=None, seq=None), timestamp=12345),
            timestamp=None,
        )
        key = build_idempotency_key(event)
        assert key is not None
        assert "ts=12345" in key
