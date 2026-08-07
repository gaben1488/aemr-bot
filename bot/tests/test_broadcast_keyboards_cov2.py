"""Покрытие непокрытых builder'ов/веток ui/broadcast_keyboards.

Pure-функции без БД. Не покрыты базовыми тестами:
- broadcast_stop_keyboard (экстренная остановка во время рассылки).
- broadcast_history_list_keyboard: все 4 ветки status-emoji
  (done/failed-cancelled/sending/fallback).
- broadcast_templates_list_keyboard: комбинации show_search/can_create
  (включая пустой top-row) и индикатор use_count в подписи.
- broadcast_templates_search_results_keyboard: use_count в подписи.

Assert проверяют payload и (для emoji/label) текст кнопок.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

from aemr_bot.handlers import callback_payloads as cp
from aemr_bot.ui import broadcast_keyboards as bk


def _payloads(kb) -> list[str]:
    return [b.payload for row in kb.payload.buttons for b in row]


def _texts(kb) -> list[str]:
    return [b.text for row in kb.payload.buttons for b in row]


class TestBroadcastStop:
    def test_has_stop_and_menu(self) -> None:
        pls = _payloads(bk.broadcast_stop_keyboard(5))
        assert cp.broadcast_stop(5) in pls
        assert cp.OP_MENU in pls


class TestHistoryListStatusEmoji:
    def _bc(self, bc_id, status):
        return NS(
            id=bc_id, status=status, delivered_count=1, subscriber_count_at_start=2
        )

    def test_all_status_emoji_branches(self) -> None:
        items = [
            self._bc(1, "done"),
            self._bc(2, "failed"),
            self._bc(3, "cancelled"),
            self._bc(4, "sending"),
            self._bc(5, "draft"),  # fallback "•"
        ]
        kb = bk.broadcast_history_list_keyboard(items)
        joined = "\n".join(_texts(kb))
        assert "✅" in joined  # done
        assert "⚠️" in joined  # failed/cancelled
        assert "▶️" in joined  # sending
        assert "•" in joined  # fallback
        # каждая строка кликабельна на карточку
        pls = _payloads(kb)
        for i in range(1, 6):
            assert cp.op_bc("open", i) in pls
        assert cp.OP_MENU in pls

    def test_none_status_is_fallback(self) -> None:
        kb = bk.broadcast_history_list_keyboard(
            [NS(id=9, status=None, delivered_count=0, subscriber_count_at_start=0)]
        )
        assert "•" in "\n".join(_texts(kb))


class TestTemplatesList:
    def _tmpl(self, tid, name, use_count=0):
        return NS(id=tid, name=name, use_count=use_count)

    def test_both_top_buttons_present(self) -> None:
        kb = bk.broadcast_templates_list_keyboard(
            [self._tmpl(1, "Отключение воды")], can_create=True, show_search=True
        )
        pls = _payloads(kb)
        assert cp.op_tmpl("search") in pls
        assert cp.op_tmpl("new") in pls
        assert cp.op_tmpl("open:1") in pls

    def test_no_search_no_create_top_row_empty(self) -> None:
        # show_search=False и can_create=False → top_row пуст, kb.row(*[])
        # не вызывается (ветка `if top_row` ложна).
        kb = bk.broadcast_templates_list_keyboard(
            [self._tmpl(2, "Тест")], can_create=False, show_search=False
        )
        pls = _payloads(kb)
        assert cp.op_tmpl("search") not in pls
        assert cp.op_tmpl("new") not in pls
        # шаблон и «назад» всё равно есть
        assert cp.op_tmpl("open:2") in pls
        assert cp.OP_MENU in pls

    def test_only_search(self) -> None:
        kb = bk.broadcast_templates_list_keyboard(
            [], can_create=False, show_search=True
        )
        pls = _payloads(kb)
        assert cp.op_tmpl("search") in pls
        assert cp.op_tmpl("new") not in pls

    def test_use_count_indicator_in_label(self) -> None:
        kb = bk.broadcast_templates_list_keyboard(
            [self._tmpl(3, "Горячий", use_count=7)]
        )
        texts = _texts(kb)
        assert any("×7" in t for t in texts)

    def test_zero_use_count_no_indicator(self) -> None:
        kb = bk.broadcast_templates_list_keyboard(
            [self._tmpl(4, "Холодный", use_count=0)]
        )
        texts = _texts(kb)
        assert not any("×" in t for t in texts)


class TestTemplatesSearchResults:
    def test_use_count_in_results_and_nav(self) -> None:
        items = [NS(id=10, name="Авария", use_count=3)]
        kb = bk.broadcast_templates_search_results_keyboard(items, "ав")
        pls = _payloads(kb)
        assert cp.op_tmpl("open:10") in pls
        assert cp.op_tmpl("search") in pls  # уточнить
        assert cp.op_tmpl("list") in pls  # к списку
        assert any("×3" in t for t in _texts(kb))
