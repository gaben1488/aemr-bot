"""Ошибка валидации не должна съедать ввод оператора.

Спасено из удалённого `test_admin_settings_characterization.py` (класс
был помечен там как P0). Регрессия, ради которой это писалось: оператор
вводил «99» вместо часа, получал «вне диапазона», вводил «18» — и
значение уходило в пустоту, потому что intent (ожидание ввода) уже был
снят. Ни один другой тест корпуса не проверяет, что intent переживает
отказ валидации.

Формат докстрингов: «если <поломка>, то <что случится с оператором/
данными>».
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("maxapi", reason="нужен maxapi для admin_settings импортов")

from tests._helpers import make_event


def _patch_scope(mod, session=None):
    """patch для `mod.session_scope`, чей `async with` отдаёт сессию.

    Патчим по месту резолва в подмодуле, где живёт leaf-функция
    (`admin_settings_quiet` / `_list` / `_text`) — фасадный re-export
    патч не перехватывает (урок PR #139).
    """
    sess = MagicMock() if session is None else session

    @asynccontextmanager
    async def _cm():
        yield sess

    return patch.object(mod, "session_scope", _cm), sess


class TestIntentSurvivesValidationError:
    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import admin_settings as facade

        facade._edit_intents.clear()
        yield
        facade._edit_intents.clear()

    @pytest.mark.asyncio
    async def test_invalid_quiet_hour_keeps_intent_so_retry_applies(self) -> None:
        """Если intent снимать до валидации, оператор после опечатки в
        часе тихих часов будет писать в никуда: повторный правильный
        ввод бот воспримет как обычное сообщение в группе, а окно
        молчания останется старым — жителям придут ночные уведомления."""
        from aemr_bot.handlers import admin_settings as facade
        from aemr_bot.handlers import admin_settings_quiet as quiet

        event = make_event(user_id=500)
        facade._intent_set(
            500, key="admin_quiet_hours_start", kind="quiet_hour", which="start",
        )
        scope_patch, sess = _patch_scope(quiet)
        sess.commit = AsyncMock()
        with patch.object(facade, "ensure_role", AsyncMock(return_value=True)), \
             scope_patch, \
             patch.object(quiet.settings_store, "set_value",
                          AsyncMock()) as set_value, \
             patch.object(quiet.ops_svc, "write_audit", AsyncMock()), \
             patch("aemr_bot.services.quiet_hours.refresh_cache_from_db",
                   AsyncMock()), \
             patch.object(quiet, "_show_quiet_card", AsyncMock()):
            # «99» вне диапазона 0-23: сообщение поглощено, но ничего не
            # записано и ожидание ввода живо.
            consumed = await facade.handle_settings_edit_text(event, "99")
            assert consumed is True
            assert "вне диапазона" in (
                event.bot.send_message.await_args.kwargs["text"]
            )
            assert facade._intent_get(500) is not None
            set_value.assert_not_awaited()

            # Повторный валидный ввод применяется и снимает ожидание.
            consumed2 = await facade.handle_settings_edit_text(event, "18")
            assert consumed2 is True
            set_value.assert_awaited_once()
            assert set_value.await_args.args[2] == 18
            assert facade._intent_get(500) is None

    @pytest.mark.asyncio
    async def test_invalid_list_add_keeps_intent_so_retry_applies(self) -> None:
        """То же для списков (темы обращений): если ожидание ввода
        слетает от пустой строки, оператор добавит тему «в воздух», а
        житель не увидит её в меню — и никто не поймёт почему."""
        from aemr_bot.handlers import admin_settings as facade
        from aemr_bot.handlers import admin_settings_list as lst

        event = make_event(user_id=501)
        facade._intent_set(501, key="topics", kind="list_add")
        scope_patch, _ = _patch_scope(lst)
        with patch.object(facade, "ensure_role", AsyncMock(return_value=True)), \
             scope_patch, \
             patch.object(lst.settings_store, "get",
                          AsyncMock(return_value=["ЖКХ"])), \
             patch.object(lst.settings_store, "set_value",
                          AsyncMock()) as set_value, \
             patch.object(lst.ops_svc, "write_audit", AsyncMock()), \
             patch.object(lst, "_show_list_card", AsyncMock()):
            consumed = await facade.handle_settings_edit_text(event, "   ")
            assert consumed is True
            assert "Пустая строка" in (
                event.bot.send_message.await_args.kwargs["text"]
            )
            assert facade._intent_get(501) is not None
            set_value.assert_not_awaited()

            consumed2 = await facade.handle_settings_edit_text(event, "Дороги")
            assert consumed2 is True
            set_value.assert_awaited_once()
            assert set_value.await_args.args[2] == ["ЖКХ", "Дороги"]
            assert facade._intent_get(501) is None

    @pytest.mark.asyncio
    async def test_rejected_url_is_not_persisted_and_retry_applies(self) -> None:
        """Если отклонённый whitelist'ом адрес всё же записать, ссылка на
        сторонний сайт уедет жителю в тексте политики ПДн — и она же
        останется в настройках после «ошибки»."""
        from aemr_bot.handlers import admin_settings as facade
        from aemr_bot.handlers import admin_settings_text as txt

        event = make_event(user_id=503)
        facade._intent_set(503, key="policy_url", kind="single")
        scope_patch, _ = _patch_scope(txt)
        # validate: отказ на первый ввод, согласие на второй — без
        # зависимости от точного состава whitelist.
        validate = MagicMock(
            side_effect=[(False, "ссылка вне whitelist"), (True, "")]
        )
        with patch.object(facade, "ensure_role", AsyncMock(return_value=True)), \
             scope_patch, \
             patch.object(txt.settings_store, "validate", validate), \
             patch.object(txt.settings_store, "get",
                          AsyncMock(return_value="https://old.example")), \
             patch.object(txt.settings_store, "set_value",
                          AsyncMock()) as set_value, \
             patch.object(txt.ops_svc, "write_audit", AsyncMock()), \
             patch.object(txt, "_show_text_card", AsyncMock()):
            consumed = await facade.handle_settings_edit_text(
                event, "https://evil.example"
            )
            assert consumed is True
            assert "❌" in event.bot.send_message.await_args.kwargs["text"]
            assert facade._intent_get(503) is not None
            set_value.assert_not_awaited()

            consumed2 = await facade.handle_settings_edit_text(
                event, "https://kamgov.ru/policy"
            )
            assert consumed2 is True
            set_value.assert_awaited_once()
            assert facade._intent_get(503) is None
