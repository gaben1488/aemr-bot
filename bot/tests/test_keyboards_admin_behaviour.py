"""Поведение служебных клавиатур: настройки, рассылки, мастера.

Продолжение `test_keyboards_behaviour.py` для трёх модулей, которые
аудит покрытия по графу (graphify, 2026-08-08) показал невызываемыми ни
одним тестом. Контракт-тест сторожит связку «кнопка ↔ маршрут», читая
исходники; здесь функции вызываются по-настоящему — ловится то, что
контракту не видно: пустой экран, кнопка без подписи, потерянный
идентификатор в payload, перепутанные ветки.
"""
from __future__ import annotations

import pytest

pytest.importorskip("maxapi", reason="клавиатуры требуют maxapi")

from aemr_bot.ui import broadcast_keyboards as bk
from aemr_bot.ui import settings_keyboards as sk
from aemr_bot.ui import wizard_keyboards as wk


def _buttons(markup) -> list:
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


def _payloads(markup) -> list[str]:
    return [str(getattr(b, "payload", "") or "") for b in _buttons(markup)]


class TestBroadcastKeyboards:
    """Рассылка — необратимое действие, кнопки вокруг неё важны особо."""

    def test_confirm_offers_both_outcomes(self) -> None:
        """Экран подтверждения даёт и отправить, и отказаться.

        Клавиатура с одной кнопкой «Отправить» превращает подтверждение
        в ловушку: оператор, открывший его по ошибке, вынужден жать
        именно её.
        """
        texts = " ".join(_texts(bk.broadcast_confirm_keyboard())).lower()
        assert texts.strip(), "экран подтверждения без кнопок"
        assert "разослать" in texts or "отправ" in texts, "нет кнопки запуска"
        assert "отмен" in texts or "назад" in texts, "нет пути отказаться"
        # Правка текста — третий исход: оператор заметил опечатку в
        # объявлении и не должен ради неё отменять и начинать заново.
        assert "изменить" in texts or "правит" in texts, (
            f"нет способа поправить текст перед отправкой: {texts}"
        )

    @pytest.mark.parametrize("broadcast_id", [1, 42, 999999])
    def test_stop_button_carries_the_right_broadcast(self, broadcast_id: int) -> None:
        """Кнопка остановки помнит, какую именно рассылку останавливать.

        Потерянный идентификатор в payload означает, что оператор жмёт
        «Стоп» на одной рассылке, а останавливается другая — или ничего.
        При оповещении о чрезвычайной ситуации цена такой ошибки высока.
        """
        payloads = " ".join(_payloads(bk.broadcast_stop_keyboard(broadcast_id)))
        assert str(broadcast_id) in payloads, (
            f"идентификатор {broadcast_id} потерян в payload: {payloads}"
        )

    def test_cooldown_screen_allows_cancelling_before_send(self) -> None:
        """Пока идёт пауза, отмена обязана быть доступна.

        Пауза перед отправкой существует ровно ради этого окна: если на
        экране нет кнопки отмены, окно бессмысленно.
        """
        texts = " ".join(_texts(bk.broadcast_cooldown_keyboard(5))).lower()
        assert "отмен" in texts or "стоп" in texts, (
            f"во время паузы нет способа остановить рассылку: {texts}"
        )

    def test_unsubscribe_button_is_present_for_citizen(self) -> None:
        """Под рассылкой у жителя есть выход из неё.

        Требование добровольности согласия: отказаться должно быть не
        сложнее, чем подписаться.
        """
        texts = " ".join(_texts(bk.broadcast_unsubscribe_keyboard())).lower()
        assert "отписаться" in texts or "не хочу" in texts, texts


class TestSettingsKeyboards:
    """Настройки бота — экран ИТ, где легко потерять несохранённое."""

    def test_menu_is_not_empty_and_labels_present(self) -> None:
        texts = _texts(sk.op_settings_menu_keyboard())
        assert texts, "меню настроек пустое"
        assert all(t.strip() for t in texts), f"кнопка без подписи: {texts}"

    def test_unsynced_count_is_visible_when_present(self) -> None:
        """Число несинхронизированных правок видно на экране.

        Иначе ИТ не знает, что настройки разошлись с репозиторием, и
        обнаруживает это при следующем развёртывании.
        """
        with_dirty = " ".join(_texts(sk.op_settings_menu_keyboard(dirty_count=3)))
        clean = " ".join(_texts(sk.op_settings_menu_keyboard(dirty_count=0)))
        assert "3" in with_dirty, f"счётчик правок не показан: {with_dirty}"
        assert with_dirty != clean, "экран не отличает наличие правок от их отсутствия"

    @pytest.mark.parametrize("key", ["welcome_text", "appointment_text"])
    def test_text_editing_screens_keep_the_key(self, key: str) -> None:
        """Экраны правки текста помнят, какой именно ключ правят.

        Потеря ключа в payload = правка уедет не в ту настройку;
        заметят это уже жители, увидев чужой текст на своём экране.
        """
        for markup in (
            sk.op_settings_text_actions_keyboard(key),
            sk.op_settings_text_cancel_keyboard(key),
        ):
            payloads = " ".join(_payloads(markup))
            assert key in payloads, f"ключ {key} потерян: {payloads}"

    def test_cancel_screen_offers_a_way_back(self) -> None:
        """С экрана отмены есть выход — иначе ИТ застревает."""
        texts = " ".join(_texts(sk.op_settings_text_cancel_keyboard("welcome_text")))
        assert texts.strip(), "экран отмены без кнопок — тупик"


class TestWizardKeyboards:
    """Мастера операторов: роли и подтверждения."""

    def test_role_picker_lists_every_role_from_the_enum(self) -> None:
        """В выборе роли присутствуют все роли, а не часть.

        Роль, забытая в клавиатуре, недостижима через интерфейс: её
        нельзя выдать, и об этом ничто не сообщит.
        """
        from aemr_bot.db.models import OperatorRole

        payloads = " ".join(_payloads(wk.op_role_picker_keyboard()))
        for role in OperatorRole:
            assert role.value in payloads, (
                f"роль {role.value} недоступна в мастере: {payloads}"
            )

    def test_role_labels_are_distinguishable(self) -> None:
        """Подписи ролей различимы, а не «aemr» против «egp».

        ИТ выбирает роль по подписи; две строки из трёх букв заставляют
        угадывать. Подписи взяты из Регламента (часть 43).
        """
        texts = _texts(wk.op_role_picker_keyboard())
        assert len(set(texts)) == len(texts), f"одинаковые подписи ролей: {texts}"
        assert all(len(t) > 10 for t in texts if t), (
            f"подпись роли без пояснения: {texts}"
        )

    def test_role_change_marks_current_role(self) -> None:
        """Текущая роль помечена и не предлагается к повторной выдаче.

        Без пометки ИТ переназначает ту же роль и думает, что действие
        не сработало.
        """
        from aemr_bot.db.models import OperatorRole

        texts = _texts(
            wk.op_operator_role_change_keyboard(42, OperatorRole.AEMR.value)
        )
        current = [t for t in texts if "текущая" in t.lower()]
        assert current, f"текущая роль не помечена: {texts}"

    def test_operators_list_renders_every_row(self) -> None:
        """Список операторов показывает всех, а не первых попавшихся."""
        rows = [
            (1, "Иванов И.И.", "coordinator", True),
            (2, "Петров П.П.", "aemr", True),
            (3, "Сидоров С.С.", "egp", False),
        ]
        texts = " ".join(_texts(wk.op_operators_list_keyboard(rows)))
        for _, name, _, _ in rows:
            assert name.split()[0] in texts, f"оператор {name} пропал из списка"
