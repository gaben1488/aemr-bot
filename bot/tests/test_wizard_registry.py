"""Тесты на services/wizard_registry — единое хранилище состояния
визардов оператора. Pure-логика, без БД."""
from __future__ import annotations

import pytest

from aemr_bot.services import wizard_registry as wr


@pytest.fixture(autouse=True)
def _clean():
    wr.reset_all()
    yield
    wr.reset_all()


class TestOpWizard:
    def test_get_returns_none_when_empty(self) -> None:
        assert wr.get_op_wizard(42) is None

    def test_set_and_get(self) -> None:
        wr.set_op_wizard(42, {"step": "awaiting_id"})
        assert wr.get_op_wizard(42) == {"step": "awaiting_id"}

    def test_update_merges(self) -> None:
        wr.set_op_wizard(42, {"step": "awaiting_id"})
        result = wr.update_op_wizard(42, target_id=100)
        assert result == {"step": "awaiting_id", "target_id": 100}

    def test_update_creates_when_missing(self) -> None:
        result = wr.update_op_wizard(42, step="awaiting_id")
        assert result == {"step": "awaiting_id"}
        assert wr.get_op_wizard(42) == {"step": "awaiting_id"}

    def test_clear(self) -> None:
        wr.set_op_wizard(42, {"x": 1})
        wr.clear_op_wizard(42)
        assert wr.get_op_wizard(42) is None

    def test_clear_idempotent(self) -> None:
        # Повторный clear по отсутствующему ключу — no-op: состояние
        # остаётся пустым, исключения нет.
        assert wr.clear_op_wizard(42) is None
        assert wr.clear_op_wizard(42) is None
        assert wr.get_op_wizard(42) is None


class TestBroadcastWizard:
    def test_set_get_clear(self) -> None:
        wr.set_broadcast_wizard(42, {"step": "awaiting_text"})
        assert wr.get_broadcast_wizard(42) == {"step": "awaiting_text"}
        wr.clear_broadcast_wizard(42)
        assert wr.get_broadcast_wizard(42) is None

    def test_independent_from_op_wizard(self) -> None:
        wr.set_op_wizard(42, {"a": 1})
        wr.set_broadcast_wizard(42, {"b": 2})
        # Один оператор может иметь оба wizard'а одновременно
        # (на самом деле не должен по бизнес-логике, но registry
        # хранит их раздельно — конфликты решает business).
        assert wr.get_op_wizard(42) == {"a": 1}
        assert wr.get_broadcast_wizard(42) == {"b": 2}


class TestReplyIntent:
    def test_set_get_drop(self) -> None:
        assert wr.get_reply_intent(42) is None
        wr.set_reply_intent(42, appeal_id=100, ts=1000.0)
        # default is_final=True; формат теперь (appeal_id, is_final, ts)
        assert wr.get_reply_intent(42) == (100, True, 1000.0)
        wr.drop_reply_intent(42)
        assert wr.get_reply_intent(42) is None

    def test_set_get_intermediate(self) -> None:
        """is_final=False сохраняется и возвращается."""
        wr.set_reply_intent(50, appeal_id=200, ts=2000.0, is_final=False)
        assert wr.get_reply_intent(50) == (200, False, 2000.0)
        wr.drop_reply_intent(50)


class TestResetAll:
    def test_clears_everything(self) -> None:
        wr.set_op_wizard(1, {"a": 1})
        wr.set_op_wizard(2, {"b": 2})
        wr.set_broadcast_wizard(1, {"c": 3})
        wr.set_reply_intent(1, 100, 1000.0)

        wr.reset_all()

        assert wr.get_op_wizard(1) is None
        assert wr.get_op_wizard(2) is None
        assert wr.get_broadcast_wizard(1) is None
        assert wr.get_reply_intent(1) is None
