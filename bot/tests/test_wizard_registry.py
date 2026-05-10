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
        wr.clear_op_wizard(42)  # никого не было
        wr.clear_op_wizard(42)  # повторный clear — без exception


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
        assert wr.get_reply_intent(42) == (100, 1000.0)
        wr.drop_reply_intent(42)
        assert wr.get_reply_intent(42) is None


class TestRecentReplies:
    def test_remember_and_check(self) -> None:
        assert not wr.is_recent_reply("key1")
        wr.remember_recent_reply("key1", 1000.0)
        assert wr.is_recent_reply("key1")

    def test_evict_old(self) -> None:
        wr.remember_recent_reply("old", 100.0)
        wr.remember_recent_reply("new", 2000.0)
        # cutoff = 1000 — old < 1000, new > 1000
        wr.evict_old_replies(cutoff_ts=1000.0)
        assert not wr.is_recent_reply("old")
        assert wr.is_recent_reply("new")

    def test_evict_caps_at_max_entries(self) -> None:
        # Все свежие, но больше чем max_entries — обрезаем до max самых свежих
        for i in range(300):
            wr.remember_recent_reply(f"k{i}", float(i))
        wr.evict_old_replies(cutoff_ts=-1.0, max_entries=100)
        # 100 самых свежих (k200..k299) остались
        assert wr.is_recent_reply("k299")
        assert wr.is_recent_reply("k200")
        assert not wr.is_recent_reply("k50")


class TestClearAllFor:
    def test_clears_all_three(self) -> None:
        wr.set_op_wizard(42, {"a": 1})
        wr.set_broadcast_wizard(42, {"b": 2})
        wr.set_reply_intent(42, 100, 1000.0)
        # Разные оператор — не трогается
        wr.set_op_wizard(99, {"other": 1})

        wr.clear_all_for(42)

        assert wr.get_op_wizard(42) is None
        assert wr.get_broadcast_wizard(42) is None
        assert wr.get_reply_intent(42) is None
        # Чужой оператор не задет
        assert wr.get_op_wizard(99) == {"other": 1}

    def test_idempotent_when_nothing(self) -> None:
        wr.clear_all_for(42)  # ничего не было — без exception


class TestResetAll:
    def test_clears_everything(self) -> None:
        wr.set_op_wizard(1, {"a": 1})
        wr.set_op_wizard(2, {"b": 2})
        wr.set_broadcast_wizard(1, {"c": 3})
        wr.set_reply_intent(1, 100, 1000.0)
        wr.remember_recent_reply("x", 1000.0)

        wr.reset_all()

        assert wr.get_op_wizard(1) is None
        assert wr.get_op_wizard(2) is None
        assert wr.get_broadcast_wizard(1) is None
        assert wr.get_reply_intent(1) is None
        assert not wr.is_recent_reply("x")
