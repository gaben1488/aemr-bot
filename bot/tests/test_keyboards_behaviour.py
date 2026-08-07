"""Поведение клавиатур: то, что видит человек, а не только payload'ы.

Контракт-тест `test_callback_coverage_contract.py` уже сторожит, что у
каждой кнопки есть маршрут, а у каждого маршрута — кнопка. Он читает
исходники и НЕ вызывает функции, поэтому мимо него проходит целый класс
поломок: пустая клавиатура, кнопка без подписи, перепутанная ветка
условия. Житель в этом случае видит сообщение без единой кнопки или
кнопку «Подписаться», будучи подписанным, — маршрут при этом
формально на месте.

Аудит покрытия по графу (graphify, 2026-08-08) показал: пять модулей
`ui/*_keyboards.py` не вызываются ни одним тестом. Эти тесты закрывают
разрыв по существу, а не по проценту строк.
"""
from __future__ import annotations

import pytest

pytest.importorskip("maxapi", reason="клавиатуры требуют maxapi")

from aemr_bot.ui import citizen_keyboards as ck
from aemr_bot.ui import operator_keyboards as ok


def _buttons(markup) -> list:
    """Плоский список кнопок из разметки любой вложенности."""
    payload = getattr(markup, "payload", markup)
    rows = getattr(payload, "buttons", None)
    if rows is None and isinstance(payload, dict):
        rows = payload.get("buttons")
    out: list = []
    for row in rows or []:
        out.extend(row if isinstance(row, (list, tuple)) else [row])
    return out


def _texts(markup) -> list[str]:
    return [str(getattr(b, "text", "") or "") for b in _buttons(markup)]


class TestCitizenMenu:
    def test_main_menu_is_not_empty_and_every_button_has_a_label(self) -> None:
        """Экран без кнопок — тупик: житель не понимает, что делать.

        Подпись обязана быть у каждой кнопки: пустая ловится глазами
        только на живом боте, а тест видит её сразу.
        """
        texts = _texts(ck.main_menu())
        assert texts, "главное меню жителя не может быть пустым"
        assert all(t.strip() for t in texts), f"кнопка без подписи: {texts}"

    def test_subscribe_button_matches_actual_state(self) -> None:
        """Кнопка подписки отражает состояние, а не всегда «Подписаться».

        Регресс из жалобы владельца: житель подписывался, возвращался в
        меню и снова видел «Подписаться» — думал, что подписка не
        сработала, и жал повторно.
        """
        unsubscribed = " ".join(_texts(ck.main_menu(subscribed=False)))
        subscribed = " ".join(_texts(ck.main_menu(subscribed=True)))

        assert "Подписаться" in unsubscribed
        assert "Подписаться" not in subscribed, (
            "подписанному предлагают подписаться ещё раз"
        )
        assert "не хочу" in subscribed.lower() or "отписаться" in subscribed.lower(), (
            f"подписанный должен видеть выход из рассылки: {subscribed}"
        )

    def test_blocked_menu_hides_appeal_and_subscribe(self) -> None:
        """Заблокированному не показывают то, что ему запрещено.

        Кнопка, которая упрётся в запрет, — обман: житель жмёт и
        получает отказ вместо действия. Оставляем только «Полезную
        информацию»: это публичные контакты экстренных служб, они не
        привязаны к его данным.
        """
        texts = " ".join(_texts(ck.blocked_user_menu()))
        assert "Полезная информация" in texts
        assert "обращение" not in texts.lower(), "заблокированному предложена подача"
        assert "подписаться" not in texts.lower(), "заблокированному предложена подписка"

    def test_blocked_menu_adds_reception_link_only_when_configured(self) -> None:
        """Электронная приёмная — запасной канал для заблокированного.

        Ссылки нет в настройках → кнопки быть не должно: пустая ссылка
        в мессенджере даёт неработающую кнопку.
        """
        without = _texts(ck.blocked_user_menu())
        with_url = _texts(ck.blocked_user_menu("https://example.gov.ru/reception"))
        assert len(with_url) == len(without) + 1
        assert any("приёмная" in t.lower() for t in with_url)


class TestOperatorMenu:
    """Меню оператора: права роли видны на экране, а не только в отказе."""

    def test_menu_is_not_empty_for_any_role_combination(self) -> None:
        """У любой роли есть хотя бы одно действие.

        Пустое меню означает, что оператор вошёл в служебную группу и не
        может ничего — молчаливая потеря доступа, которую заметят только
        по жалобе.
        """
        for is_it in (False, True):
            for can_broadcast in (False, True):
                texts = _texts(
                    ok.op_help_keyboard(is_it=is_it, can_broadcast=can_broadcast)
                )
                combo = f"is_it={is_it} can_broadcast={can_broadcast}"
                assert texts, f"пустое меню оператора при {combo}"
                assert all(t.strip() for t in texts), f"кнопка без подписи: {combo}"

    def test_admin_actions_only_for_it(self) -> None:
        """Управление операторами и настройки — право ИТ.

        Кнопка у того, кому нельзя, ведёт к отказу на нажатии: лишний
        путь к запрету и подсказка, что такая возможность существует.
        """
        plain = " ".join(_texts(ok.op_help_keyboard(is_it=False))).lower()
        it = " ".join(_texts(ok.op_help_keyboard(is_it=True))).lower()

        assert "настройк" in it, "ИТ обязан видеть настройки бота"
        assert "настройк" not in plain, "специалисту показали настройки ИТ"
        assert len(_texts(ok.op_help_keyboard(is_it=True))) > len(
            _texts(ok.op_help_keyboard(is_it=False))
        ), "у ИТ должно быть больше действий"

    def test_broadcast_hidden_without_permission(self) -> None:
        """Рассылки видят только те, кто может их запускать.

        Специалисты АЕМО и ЕГП всё равно получили бы отказ от проверки
        роли — кнопка лишь плодила бы шум в служебной группе.
        """
        allowed = " ".join(_texts(ok.op_help_keyboard(can_broadcast=True))).lower()
        denied = " ".join(_texts(ok.op_help_keyboard(can_broadcast=False))).lower()

        assert "рассылк" in allowed
        assert "рассылк" not in denied, "рассылка предложена тому, кому она запрещена"

    def test_open_count_shown_next_to_button(self) -> None:
        """Число открытых обращений видно прямо на кнопке.

        Координатор оценивает нагрузку до нажатия: без счётчика он
        открывает список ради одной цифры.
        """
        with_count = " ".join(_texts(ok.op_help_keyboard(open_count=7)))
        assert "7" in with_count, f"счётчик обращений не попал на кнопку: {with_count}"


class TestNoDuplicatePayloads:
    @pytest.mark.parametrize(
        "markup_factory",
        [
            lambda: ck.main_menu(),
            lambda: ck.main_menu(subscribed=True),
            lambda: ck.blocked_user_menu(),
        ],
        ids=["main", "main_subscribed", "blocked"],
    )
    def test_payloads_unique_within_one_keyboard(self, markup_factory) -> None:
        """Две кнопки с одним payload — вторая мертва.

        Житель жмёт нижнюю, срабатывает верхняя: поведение выглядит
        случайным, а маршрут при этом зарегистрирован — контракт-тест
        такое пропускает.
        """
        payloads = [
            getattr(b, "payload", None)
            for b in _buttons(markup_factory())
            if getattr(b, "payload", None)
        ]
        assert len(payloads) == len(set(payloads)), f"дубли payload: {payloads}"
