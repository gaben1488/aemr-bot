"""Покрытие оставшихся мелких веток в citizen/settings/wizard клавиатурах.

Pure-функции без БД. Точечно добиваем непокрытые ветки:
- citizen.my_appeals_list_keyboard: пагинация (prev/next/noop) при
  total_pages>1, на первой/средней/последней странице.
- citizen.useful_info_keyboard: ветка subscribed=True vs False (кнопка
  отписки/подписки).
- citizen.forget_confirm_keyboard / consent_revoke_confirm_keyboard:
  alias-функции (делегируют goodbye_*_confirm).
- settings.op_settings_obj_keyboard: усечение длинной подписи (>45) и
  ветка name-без-phone.
- wizard.op_operator_card_keyboard: ветка inactive → «Реактивировать».
"""
from __future__ import annotations

from aemr_bot.handlers import callback_payloads as cp
from aemr_bot.ui import citizen_keyboards as ck
from aemr_bot.ui import settings_keyboards as sk
from aemr_bot.ui import wizard_keyboards as wk


def _payloads(kb) -> list[str]:
    return [b.payload for row in kb.payload.buttons for b in row if hasattr(b, "payload")]


def _texts(kb) -> list[str]:
    return [getattr(b, "text", "") for row in kb.payload.buttons for b in row]


class TestMyAppealsPagination:
    def test_single_page_no_nav(self) -> None:
        kb = ck.my_appeals_list_keyboard([(1, "#1 дороги")], page=1, total_pages=1)
        pls = _payloads(kb)
        assert cp.appeal_show(1) in pls
        assert cp.PREFIX_APPEALS_PAGE + "noop" not in pls
        assert cp.MENU_MAIN in pls

    def test_first_page_forward_only(self) -> None:
        kb = ck.my_appeals_list_keyboard([(1, "a")], page=1, total_pages=3)
        pls = _payloads(kb)
        assert cp.appeals_page(2) in pls  # вперёд
        assert cp.appeals_page(0) not in pls  # назад нельзя
        assert cp.PREFIX_APPEALS_PAGE + "noop" in pls

    def test_middle_page_both(self) -> None:
        kb = ck.my_appeals_list_keyboard([(1, "a")], page=2, total_pages=3)
        pls = _payloads(kb)
        assert cp.appeals_page(1) in pls
        assert cp.appeals_page(3) in pls

    def test_last_page_back_only(self) -> None:
        kb = ck.my_appeals_list_keyboard([(1, "a")], page=3, total_pages=3)
        pls = _payloads(kb)
        assert cp.appeals_page(2) in pls
        assert cp.appeals_page(4) not in pls


class TestUsefulInfoSubscription:
    def test_subscribed_shows_unsubscribe(self) -> None:
        pls = _payloads(ck.useful_info_keyboard(subscribed=True))
        assert cp.INFO_SUBSCRIBE_OFF in pls

    def test_unsubscribed_no_unsubscribe_button(self) -> None:
        pls = _payloads(ck.useful_info_keyboard(subscribed=False))
        assert cp.INFO_SUBSCRIBE_OFF not in pls


class TestCitizenAliasKeyboards:
    def test_forget_confirm_equiv_goodbye_erase(self) -> None:
        alias = _payloads(ck.forget_confirm_keyboard())
        canonical = _payloads(ck.goodbye_erase_confirm_keyboard())
        assert alias == canonical

    def test_consent_revoke_confirm_equiv_goodbye_revoke(self) -> None:
        alias = _payloads(ck.consent_revoke_confirm_keyboard())
        canonical = _payloads(ck.goodbye_revoke_confirm_keyboard())
        assert alias == canonical


class TestSettingsObjKeyboard:
    def test_long_label_truncated_with_ellipsis(self) -> None:
        long_name = "Очень длинное название аварийной службы превышающее лимит"
        items = [{"name": long_name, "phone": "+7 (4152) 00-00-00"}]
        texts = _texts(sk.op_settings_obj_keyboard("emergency_contacts", items))
        # где-то в подписях есть усечение многоточием.
        assert any("…" in t for t in texts)

    def test_name_without_phone_label(self) -> None:
        items = [{"name": "Служба без телефона", "phone": ""}]
        kb = sk.op_settings_obj_keyboard("emergency_contacts", items)
        texts = _texts(kb)
        assert any("Служба без телефона" in t for t in texts)
        # без " — " разделителя телефона
        assert not any(" — " in t for t in texts)

    def test_routes_fallback_when_no_name(self) -> None:
        # transport-объекты: name пустой → берём routes.
        items = [{"routes": "12, 8", "phone": "+79990001122"}]
        kb = sk.op_settings_obj_keyboard("transport_dispatcher_contacts", items)
        texts = _texts(kb)
        assert any("12, 8" in t for t in texts)


class TestOperatorCardInactive:
    def test_inactive_operator_shows_reactivate(self) -> None:
        pls = _payloads(
            wk.op_operator_card_keyboard(
                500, is_active=False, is_self=False, can_deactivate=True
            )
        )
        assert cp.op_opreact(500) in pls
        # неактивному не показываем смену роли/деактивацию.
        assert cp.op_oprole(500) not in pls

    def test_active_non_self_shows_role_and_deactivate(self) -> None:
        pls = _payloads(
            wk.op_operator_card_keyboard(
                500, is_active=True, is_self=False, can_deactivate=True
            )
        )
        assert cp.op_oprole(500) in pls

    def test_active_self_no_role_change(self) -> None:
        pls = _payloads(
            wk.op_operator_card_keyboard(
                500, is_active=True, is_self=True, can_deactivate=True
            )
        )
        # self не может менять свою роль / деактивировать себя из карточки.
        assert cp.op_oprole(500) not in pls
        assert cp.op_opdeact(500) not in pls
