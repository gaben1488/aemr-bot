"""Покрытие непокрытых builder'ов ui/operator_keyboards.

Pure-функции без БД и MAX-API — собирают inline-клавиатуры из
CallbackButton. Базовый test_keyboards.py трогает только op_help_keyboard /
op_stats_menu / op_audience_menu / appeal_admin_actions(smoke), оставляя
без тестов:
- op_help_main_keyboard / op_help_security_keyboard (двухэкранная памятка)
- open_tickets_listing_keyboard (включая ветку пустого списка и status-emoji)
- op_audience_user_actions (blocked True/False)
- op_audience_paginated_list_keyboard (ветки пагинации, bulk-dump, search)
- op_audience_user_card_keyboard (category-возврат)
- op_audience_search_cancel_keyboard (category vs None)
- appeal_admin_actions: ветка attachment_count>0 и reopen при revoke

Все assert проверяют реальные payload-строки кнопок, а не «kb is not None».
"""
from __future__ import annotations

from aemr_bot.db.models import AppealStatus
from aemr_bot.handlers import callback_payloads as cp
from aemr_bot.ui import operator_keyboards as ok


def _payloads(kb) -> list[str]:
    """Все payload кнопок в порядке появления (с дублями — для подсчёта)."""
    return [b.payload for row in kb.payload.buttons for b in row]


def _texts(kb) -> list[str]:
    return [b.text for row in kb.payload.buttons for b in row]


def _rows(kb) -> list[list]:
    return kb.payload.buttons


class TestHelpTwoScreens:
    def test_main_screen_links_to_security_and_menu(self) -> None:
        pls = _payloads(ok.op_help_main_keyboard())
        assert cp.OP_HELP_SECURITY in pls
        assert cp.OP_MENU in pls
        # ровно две кнопки, по одной в ряд
        assert len(pls) == 2

    def test_security_screen_links_back_to_help_and_menu(self) -> None:
        pls = _payloads(ok.op_help_security_keyboard())
        assert cp.OP_HELP_FULL in pls
        assert cp.OP_MENU in pls
        assert len(pls) == 2


class TestOpenTicketsListing:
    def test_each_item_is_a_clickable_card_button(self) -> None:
        items = [
            (1, AppealStatus.NEW.value, "дороги"),
            (2, AppealStatus.IN_PROGRESS.value, "ЖКХ"),
        ]
        kb = ok.open_tickets_listing_keyboard(items)
        pls = _payloads(kb)
        assert cp.op_open_card(1) in pls
        assert cp.op_open_card(2) in pls
        # плюс кнопка возврата
        assert cp.OP_MENU in pls
        assert len(pls) == 3

    def test_status_emoji_mapping_and_unknown_fallback(self) -> None:
        items = [
            (10, AppealStatus.NEW.value, "a"),
            (11, AppealStatus.IN_PROGRESS.value, "b"),
            (12, AppealStatus.ANSWERED.value, "c"),
            (13, AppealStatus.CLOSED.value, "d"),
            (14, "weird_unknown_status", "e"),
        ]
        texts = _texts(ok.open_tickets_listing_keyboard(items))
        joined = "\n".join(texts)
        assert "🆕" in joined
        assert "🔄" in joined
        assert "✅" in joined
        assert "⛔" in joined
        # неизвестный статус → fallback "•"
        assert "•" in joined

    def test_empty_items_only_back_button(self) -> None:
        kb = ok.open_tickets_listing_keyboard([])
        pls = _payloads(kb)
        assert pls == [cp.OP_MENU]


class TestAudienceUserActions:
    def test_blocked_shows_unblock(self) -> None:
        pls = _payloads(ok.op_audience_user_actions(555, blocked=True))
        assert cp.op_aud("unblock:555") in pls
        assert cp.op_aud("block:555") not in pls
        assert cp.op_aud("erase:555") in pls

    def test_unblocked_shows_block(self) -> None:
        pls = _payloads(ok.op_audience_user_actions(555, blocked=False))
        assert cp.op_aud("block:555") in pls
        assert cp.op_aud("unblock:555") not in pls
        assert cp.op_aud("erase:555") in pls


class TestAudiencePaginatedList:
    def _rows(self, category="subs", n_rows=2, page=1, total_pages=1):
        rows = [(100 + i, f"#{100 + i} · Имя") for i in range(n_rows)]
        return ok.op_audience_paginated_list_keyboard(
            category, rows, page=page, total_pages=total_pages
        )

    def test_rows_clickable_and_dump_and_search_present(self) -> None:
        kb = self._rows(n_rows=3, page=1, total_pages=1)
        pls = _payloads(kb)
        assert cp.op_aud("show:100") in pls
        assert cp.op_aud("show:102") in pls
        # bulk-dump показывает число строк на странице
        assert cp.op_aud("dump:subs:1") in pls
        assert cp.op_aud("search:subs") in pls

    def test_single_page_has_no_pagination_row(self) -> None:
        kb = self._rows(total_pages=1)
        pls = _payloads(kb)
        # noop-кнопка пагинации появляется только при >1 странице
        assert cp.op_aud("page:subs:noop") not in pls

    def test_first_page_only_forward_arrow(self) -> None:
        kb = self._rows(page=1, total_pages=3)
        pls = _payloads(kb)
        assert cp.op_aud("page:subs:noop") in pls  # индикатор "1 / 3"
        assert cp.op_aud("page:subs:2") in pls  # вперёд
        assert cp.op_aud("page:subs:0") not in pls  # назад с первой нельзя

    def test_middle_page_both_arrows(self) -> None:
        kb = self._rows(page=2, total_pages=3)
        pls = _payloads(kb)
        assert cp.op_aud("page:subs:1") in pls  # назад
        assert cp.op_aud("page:subs:3") in pls  # вперёд

    def test_last_page_only_back_arrow(self) -> None:
        kb = self._rows(page=3, total_pages=3)
        pls = _payloads(kb)
        assert cp.op_aud("page:subs:2") in pls  # назад
        assert cp.op_aud("page:subs:4") not in pls  # вперёд за последнюю нельзя

    def test_empty_rows_no_dump_but_search_stays(self) -> None:
        kb = ok.op_audience_paginated_list_keyboard("blocked", [], page=1, total_pages=1)
        pls = _payloads(kb)
        # dump только при непустых rows
        assert not any(p.startswith("op:aud:dump") for p in pls)
        # search доступен всегда
        assert cp.op_aud("search:blocked") in pls
        assert cp.OP_AUDIENCE in pls
        assert cp.OP_MENU in pls


class TestAudienceUserCard:
    def test_blocked_with_category_returns_to_list(self) -> None:
        pls = _payloads(
            ok.op_audience_user_card_keyboard(7, blocked=True, category="consent")
        )
        assert cp.op_aud("unblock:7") in pls
        assert cp.op_aud("erase:7") in pls
        # category задан → есть «↩️ К списку» = op:aud:consent
        assert cp.op_aud("consent") in pls

    def test_unblocked_without_category_no_list_return(self) -> None:
        pls = _payloads(
            ok.op_audience_user_card_keyboard(7, blocked=False, category=None)
        )
        assert cp.op_aud("block:7") in pls
        # без category «К списку» не добавляется
        assert cp.op_aud("subs") not in pls
        assert cp.op_aud("consent") not in pls
        assert cp.op_aud("blocked") not in pls
        assert cp.OP_AUDIENCE in pls

    def test_invalid_category_ignored(self) -> None:
        # category не из {subs,consent,blocked} — «К списку» не добавляется
        pls = _payloads(
            ok.op_audience_user_card_keyboard(7, blocked=False, category="garbage")
        )
        assert not any(
            p in {cp.op_aud("subs"), cp.op_aud("consent"), cp.op_aud("blocked")}
            for p in pls
        )


class TestAudienceSearchCancel:
    def test_known_category_offers_back_to_list(self) -> None:
        pls = _payloads(ok.op_audience_search_cancel_keyboard("subs"))
        assert cp.op_aud("subs") in pls
        assert cp.OP_AUDIENCE in pls
        assert cp.OP_MENU in pls

    def test_none_category_no_back_to_list(self) -> None:
        pls = _payloads(ok.op_audience_search_cancel_keyboard(None))
        assert cp.op_aud("subs") not in pls
        assert cp.OP_AUDIENCE in pls
        assert cp.OP_MENU in pls

    def test_invalid_category_no_back_to_list(self) -> None:
        pls = _payloads(ok.op_audience_search_cancel_keyboard("nope"))
        assert cp.op_aud("nope") not in pls
        assert cp.OP_AUDIENCE in pls


class TestAppealAdminActionsBranches:
    def test_attachment_button_present_when_count_positive(self) -> None:
        pls = _payloads(
            ok.appeal_admin_actions(
                42, AppealStatus.NEW.value, attachment_count=3
            )
        )
        assert cp.op_atts(42) in pls
        # open-state кнопки тоже есть
        assert cp.op_reply(42) in pls
        assert cp.op_replyint(42) in pls
        assert cp.op_close(42) in pls

    def test_no_attachment_button_when_zero(self) -> None:
        pls = _payloads(
            ok.appeal_admin_actions(42, AppealStatus.NEW.value, attachment_count=0)
        )
        assert cp.op_atts(42) not in pls

    def test_closed_not_revoked_shows_reopen(self) -> None:
        pls = _payloads(
            ok.appeal_admin_actions(
                42, AppealStatus.CLOSED.value, closed_due_to_revoke=False
            )
        )
        assert cp.op_reopen(42) in pls

    def test_closed_revoked_hides_reopen(self) -> None:
        pls = _payloads(
            ok.appeal_admin_actions(
                42, AppealStatus.CLOSED.value, closed_due_to_revoke=True
            )
        )
        assert cp.op_reopen(42) not in pls

    def test_it_role_block_unblock_toggle(self) -> None:
        blocked_pls = _payloads(
            ok.appeal_admin_actions(
                42, AppealStatus.NEW.value, is_it=True, user_blocked=True
            )
        )
        assert cp.op_unblock(42) in blocked_pls
        assert cp.op_erase(42) in blocked_pls

        active_pls = _payloads(
            ok.appeal_admin_actions(
                42, AppealStatus.NEW.value, is_it=True, user_blocked=False
            )
        )
        assert cp.op_block(42) in active_pls


class TestSimpleBackKeyboards:
    """Утилитарные одно-двухкнопочные возвраты — каждая своя строка payload."""

    def test_cancel_reply_intent(self) -> None:
        assert cp.OP_REPLY_CANCEL in _payloads(ok.cancel_reply_intent_keyboard())

    def test_back_to_menu(self) -> None:
        assert _payloads(ok.op_back_to_menu_keyboard()) == [cp.OP_MENU]

    def test_back_to_operators(self) -> None:
        pls = _payloads(ok.op_back_to_operators_keyboard())
        assert cp.OP_OPERATORS in pls
        assert cp.OP_MENU in pls

    def test_back_to_settings(self) -> None:
        pls = _payloads(ok.op_back_to_settings_keyboard())
        assert cp.OP_SETTINGS in pls
        assert cp.OP_MENU in pls

    def test_back_to_audience(self) -> None:
        pls = _payloads(ok.op_back_to_audience_keyboard())
        assert cp.OP_AUDIENCE in pls
        assert cp.OP_MENU in pls
