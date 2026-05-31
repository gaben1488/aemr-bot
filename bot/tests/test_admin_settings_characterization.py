"""Characterization-тесты handlers/admin_settings.py — НЕПОКРЫТЫЕ ветки.

Дополняет `test_admin_settings_handlers.py` (intent-lifecycle, dispatch,
quiet-wizard, handle_settings_edit_text) и `test_admin_settings_audit.py`
(`_clip_audit_value`). Здесь — характеризация **прикладного слоя**,
который раньше был «слепой зоной» покрытия: рендер карточек, CRUD
списков/объектов, apply-пути с валидацией и записью в `audit_log`,
PR-flow (confirm/create/diff) и экспертная карточка ключа.

Методология (Майкл Физерс, characterization testing): тесты ЗАКРЕПЛЯЮТ
текущее поведение как есть — включая «странности» (например, что
`_list_delete` при `idx out of range` молча возвращается без сообщения,
а `_obj_delete` шлёт «Запись не найдена»; что `_apply_single_edit`
пишет before→after в audit; что `_apply_obj_add` для emergency без
третьей строки кладёт item без `section`). Это страховка перед будущей
декомпозицией god-объекта (~1182 строки) на подмодули: wizard-ввод /
список / карточка / CRUD / валидация.

Стиль — как существующие `test_*characterization.py`: SimpleNamespace-
фейки через `tests._helpers.make_event`, без реального Postgres; мокаем
`session_scope`, `settings_store.*`, `ops_svc.*`, `repo_sync.*`,
`send_or_edit_screen`, `ensure_role`. Клавиатуры (pure-функции из
`aemr_bot.ui.settings_keyboards`, собирают maxapi-markup) НЕ мокаем —
они дёшевы и заодно ловят регрессии форм payload'ов.

ВАЖНО (урок PR #139): тесты патчат символы по месту их использования —
`patch.object(mod, "settings_store")`-атрибуты, `mod.ops_svc`,
`mod.session_scope`, `mod.send_or_edit_screen`. После будущего
извлечения функций эти patch-точки сместятся; карта repoint — в
возвращаемом плане декомпозиции, не в этом файле.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


pytest.importorskip("maxapi", reason="нужен maxapi для admin_settings импортов")

from tests._helpers import make_event


# ──────────────────────────────────────────────────────────────────────
# Общая инфраструктура: фейковый session_scope как context-manager,
# возвращающий переданную сессию (или свежий MagicMock).
# ──────────────────────────────────────────────────────────────────────


def _patch_scope(mod, session=None):
    """Вернуть patch-объект для `mod.session_scope`, чей `async with`
    отдаёт `session` (MagicMock по умолчанию).

    Используется во всех тестах, дёргающих БД-путь: handler внутри
    делает `async with session_scope() as session: ...`. Нам важно,
    чтобы `session` был объектом, к которому handler обращается только
    через уже замоканные `settings_store.*` / `ops_svc.*`.
    """
    sess = MagicMock() if session is None else session

    @asynccontextmanager
    async def _cm():
        yield sess

    return patch.object(mod, "session_scope", _cm), sess


# ══════════════════════════════════════════════════════════════════════
# 1. Карточки текстов/URL: _show_text_card, _start_edit_intent
# ══════════════════════════════════════════════════════════════════════


class TestShowTextCard:
    """`_show_text_card` — рендер карточки текстового/URL-ключа с
    текущим значением, типом и constraints из SCHEMA."""

    @pytest.mark.asyncio
    async def test_text_key_renders_value_and_limit(self) -> None:
        from aemr_bot.handlers import admin_settings_text as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value="Привет, житель!")), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_text_card(event, "welcome_text")
        send.assert_awaited_once()
        text = send.await_args.kwargs["text"]
        # Заголовок из title_map + текущее значение + лимит из SCHEMA.
        assert "👋 Приветствие" in text
        assert "Привет, житель!" in text
        assert "3800" in text  # max_len welcome_text

    @pytest.mark.asyncio
    async def test_url_key_renders_http_constraint(self) -> None:
        from aemr_bot.handlers import admin_settings_text as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value="https://kamgov.ru/questions")), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_text_card(event, "electronic_reception_url")
        text = send.await_args.kwargs["text"]
        assert "🌐 Электронная приёмная" in text
        assert "http://" in text  # подсказка про схему URL

    @pytest.mark.asyncio
    async def test_unknown_key_falls_back_to_key_as_title(self) -> None:
        from aemr_bot.handlers import admin_settings_text as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=None)), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_text_card(event, "mystery_key")
        text = send.await_args.kwargs["text"]
        # Нет в title_map → используется сам key; нет в SCHEMA →
        # type_label = "?" (rule.get("type", str) даёт str по умолчанию
        # — фиксируем фактическое поведение: str.__name__ == "str").
        assert "mystery_key" in text


class TestStartEditIntent:
    """`_start_edit_intent` — установка intent'а на правку ключа + гард
    «ключ не в SCHEMA»."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import admin_settings_text as mod
        mod._edit_intents.clear()
        yield
        mod._edit_intents.clear()

    @pytest.mark.asyncio
    async def test_unknown_key_rejected_no_intent(self) -> None:
        from aemr_bot.handlers import admin_settings_text as mod

        event = make_event()
        with patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._start_edit_intent(event, 42, "not_a_real_key")
        text = send.await_args.kwargs["text"]
        assert "нельзя править из меню" in text
        # Intent НЕ установлен.
        assert mod._intent_get(42) is None

    @pytest.mark.asyncio
    async def test_url_key_sets_intent_and_url_hint(self) -> None:
        from aemr_bot.handlers import admin_settings_text as mod

        event = make_event()
        with patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._start_edit_intent(event, 42, "policy_url")
        intent = mod._intent_get(42)
        assert intent is not None
        assert intent["key"] == "policy_url"
        assert intent["kind"] == "single"
        text = send.await_args.kwargs["text"]
        assert "URL" in text  # url-hint

    @pytest.mark.asyncio
    async def test_str_key_sets_intent_with_maxlen_hint(self) -> None:
        from aemr_bot.handlers import admin_settings_text as mod

        event = make_event()
        with patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._start_edit_intent(event, 7, "appointment_text")
        intent = mod._intent_get(7)
        assert intent is not None and intent["kind"] == "single"
        text = send.await_args.kwargs["text"]
        assert "2000" in text  # max_len appointment_text


# ══════════════════════════════════════════════════════════════════════
# 2. Списки строк: _show_list_card, _list_delete, _apply_list_add
# ══════════════════════════════════════════════════════════════════════


class TestShowListCard:
    """`_show_list_card` — рендер строкового списка (topics/localities)."""

    @pytest.mark.asyncio
    async def test_non_empty_list_numbered(self) -> None:
        from aemr_bot.handlers import admin_settings_list as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=["ЖКХ", "Дороги"])), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_list_card(event, "topics")
        text = send.await_args.kwargs["text"]
        assert "🏷 Тематики обращений (2)" in text
        assert "1. ЖКХ" in text
        assert "2. Дороги" in text

    @pytest.mark.asyncio
    async def test_empty_list_shows_placeholder(self) -> None:
        from aemr_bot.handlers import admin_settings_list as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=[])), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_list_card(event, "localities")
        text = send.await_args.kwargs["text"]
        assert "(список пуст)" in text

    @pytest.mark.asyncio
    async def test_non_list_value_coerced_to_empty(self) -> None:
        from aemr_bot.handlers import admin_settings_list as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        # settings_store.get вернул не-list (мусор/строку) → handler
        # приводит к [] и рисует placeholder.
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value="oops-not-a-list")), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_list_card(event, "topics")
        text = send.await_args.kwargs["text"]
        assert "(список пуст)" in text
        assert "(0)" in text


class TestListDelete:
    """`_list_delete` — удаление элемента строкового списка по indexу.
    Закрепляем edge-кейсы парсинга suffix и валидации результата."""

    @pytest.mark.asyncio
    async def test_malformed_suffix_no_colon_returns_silently(self) -> None:
        from aemr_bot.handlers import admin_settings_list as mod

        event = make_event()
        # Нет ":" → parts != 2 → ранний return без какого-либо вывода.
        with patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._list_delete(event, 42, "topicsonly")
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_int_index_returns_silently(self) -> None:
        from aemr_bot.handlers import admin_settings_list as mod

        event = make_event()
        with patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._list_delete(event, 42, "topics:abc")
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_index_out_of_range_shows_not_found(self) -> None:
        from aemr_bot.handlers import admin_settings_list as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=["ЖКХ"])), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._list_delete(event, 42, "topics:5")
        text = send.await_args.kwargs["text"]
        assert "Элемент не найден" in text

    @pytest.mark.asyncio
    async def test_delete_rejected_by_validation_keeps_item(self) -> None:
        """topics SCHEMA: min_items=1. Удаление последнего элемента →
        validate отклоняет → «Удаление отменено», set_value НЕ зовётся."""
        from aemr_bot.handlers import admin_settings_list as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=["ЕдинственнаяТема"])), \
             patch.object(mod.settings_store, "set_value",
                          AsyncMock()) as set_value, \
             patch.object(mod.ops_svc, "write_audit", AsyncMock()) as audit, \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._list_delete(event, 42, "topics:0")
        text = send.await_args.kwargs["text"]
        assert "Удаление отменено" in text
        set_value.assert_not_awaited()
        audit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_successful_delete_persists_and_audits(self) -> None:
        from aemr_bot.handlers import admin_settings_list as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=["ЖКХ", "Дороги", "Свет"])), \
             patch.object(mod.settings_store, "set_value",
                          AsyncMock()) as set_value, \
             patch.object(mod.ops_svc, "write_audit", AsyncMock()) as audit, \
             patch.object(mod, "_show_list_card", AsyncMock()) as show:
            await mod._list_delete(event, 99, "topics:1")
        # Удалён индекс 1 ("Дороги"), список сохранён.
        set_value.assert_awaited_once()
        saved = set_value.await_args.args[2]
        assert saved == ["ЖКХ", "Свет"]
        # Audit: action setting_list_del, removed="Дороги", index=1.
        audit.assert_awaited_once()
        akw = audit.await_args.kwargs
        assert akw["action"] == "setting_list_del"
        assert akw["target"] == "topics"
        assert akw["details"]["removed"] == "Дороги"
        assert akw["details"]["index"] == 1
        assert akw["operator_max_user_id"] == 99
        # После — перерисовка карточки.
        show.assert_awaited_once_with(event, "topics")


class TestApplyListAdd:
    """`_apply_list_add` — добавление строки в список через intent."""

    @pytest.mark.asyncio
    async def test_empty_string_rejected(self) -> None:
        from aemr_bot.handlers import admin_settings_list as mod

        event = make_event()
        await mod._apply_list_add(event, 42, "topics", "")
        event.bot.send_message.assert_awaited()
        assert "Пустая строка" in event.bot.send_message.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_duplicate_rejected(self) -> None:
        from aemr_bot.handlers import admin_settings_list as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=["ЖКХ"])), \
             patch.object(mod.settings_store, "set_value",
                          AsyncMock()) as set_value:
            await mod._apply_list_add(event, 42, "topics", "ЖКХ")
        event.bot.send_message.assert_awaited()
        assert "уже есть" in event.bot.send_message.await_args.kwargs["text"]
        set_value.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_validation_failure_rejected(self) -> None:
        """topics SCHEMA: max_items=30. Добавление 31-го → validate
        отклоняет, ничего не сохраняется."""
        from aemr_bot.handlers import admin_settings_list as mod

        event = make_event()
        existing = [f"тема-{i}" for i in range(30)]
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=existing)), \
             patch.object(mod.settings_store, "set_value",
                          AsyncMock()) as set_value:
            await mod._apply_list_add(event, 42, "topics", "тема-31")
        event.bot.send_message.assert_awaited()
        # Сообщение начинается с ❌ + текст ошибки validate.
        assert "❌" in event.bot.send_message.await_args.kwargs["text"]
        set_value.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_successful_add_persists_and_audits(self) -> None:
        from aemr_bot.handlers import admin_settings_list as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=["ЖКХ"])), \
             patch.object(mod.settings_store, "set_value",
                          AsyncMock()) as set_value, \
             patch.object(mod.ops_svc, "write_audit", AsyncMock()) as audit, \
             patch.object(mod, "_show_list_card", AsyncMock()) as show:
            await mod._apply_list_add(event, 77, "topics", "Дороги")
        set_value.assert_awaited_once()
        assert set_value.await_args.args[2] == ["ЖКХ", "Дороги"]
        audit.assert_awaited_once()
        akw = audit.await_args.kwargs
        assert akw["action"] == "setting_list_add"
        assert akw["details"]["added"] == "Дороги"
        show.assert_awaited_once_with(event, "topics")


# ══════════════════════════════════════════════════════════════════════
# 3. Списки объектов: _show_obj_card, _show_obj_item, _start_obj_add,
#    _obj_delete, _apply_obj_add
# ══════════════════════════════════════════════════════════════════════


class TestShowObjCard:
    """`_show_obj_card` — рендер карточки списка объектов с подсказкой
    формата, зависящей от ключа."""

    @pytest.mark.asyncio
    async def test_emergency_renders_format_hint(self) -> None:
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        items = [{"name": "Пожарная", "phone": "01"}]
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=items)), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_obj_card(event, "emergency_contacts")
        text = send.await_args.kwargs["text"]
        assert "🆘 Экстренные службы (1)" in text
        # Format-hint для emergency упоминает «раздел».
        assert "раздел" in text
        assert "Пожарная" in text

    @pytest.mark.asyncio
    async def test_transport_renders_routes_hint(self) -> None:
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=[])), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_obj_card(event, "transport_dispatcher_contacts")
        text = send.await_args.kwargs["text"]
        assert "🚌 Диспетчерские транспорта (0)" in text
        assert "маршруты" in text


class TestShowObjItem:
    """`_show_obj_item` — карточка одного объекта (key:idx)."""

    @pytest.mark.asyncio
    async def test_malformed_suffix_returns_silently(self) -> None:
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        with patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_obj_item(event, "noколон")
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_int_index_returns_silently(self) -> None:
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        with patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_obj_item(event, "emergency_contacts:xx")
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_out_of_range_shows_not_found(self) -> None:
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=[{"name": "A", "phone": "01"}])), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_obj_item(event, "emergency_contacts:9")
        assert "Запись не найдена" in send.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_valid_item_renders_key_value_lines(self) -> None:
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        items = [{"name": "Пожарная служба", "phone": "01"}]
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=items)), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_obj_item(event, "emergency_contacts:0")
        text = send.await_args.kwargs["text"]
        assert "name: Пожарная служба" in text
        assert "phone: 01" in text


class TestStartObjAdd:
    """`_start_obj_add` — intent на добавление объекта + key-specific
    подсказка формата."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import admin_settings_obj as mod
        mod._edit_intents.clear()
        yield
        mod._edit_intents.clear()

    @pytest.mark.asyncio
    async def test_emergency_sets_intent_and_three_line_hint(self) -> None:
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        with patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._start_obj_add(event, 42, "emergency_contacts")
        intent = mod._intent_get(42)
        assert intent is not None
        assert intent["key"] == "emergency_contacts"
        assert intent["kind"] == "obj_add"
        text = send.await_args.kwargs["text"]
        assert "раздел" in text  # 3-я строка опциональна

    @pytest.mark.asyncio
    async def test_transport_sets_intent_two_line_hint(self) -> None:
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        with patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._start_obj_add(event, 42, "transport_dispatcher_contacts")
        intent = mod._intent_get(42)
        assert intent["kind"] == "obj_add"
        text = send.await_args.kwargs["text"]
        assert "маршруты" in text

    @pytest.mark.asyncio
    async def test_other_key_sets_intent_generic_hint(self) -> None:
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        with patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._start_obj_add(event, 42, "some_other_obj")
        # Intent ставится даже для незнакомого ключа (характеризуем как
        # есть — гард по ключу только в _apply_obj_add, не здесь).
        assert mod._intent_get(42) is not None
        text = send.await_args.kwargs["text"]
        assert "двумя строками" in text


class TestObjDelete:
    """`_obj_delete` — удаление объекта по index, валидация результата."""

    @pytest.mark.asyncio
    async def test_malformed_suffix_returns_silently(self) -> None:
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        with patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._obj_delete(event, 42, "justkey")
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_out_of_range_shows_not_found(self) -> None:
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=[{"name": "A", "phone": "01"}])), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._obj_delete(event, 42, "emergency_contacts:7")
        assert "Запись не найдена" in send.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_delete_rejected_by_min_items_validation(self) -> None:
        """emergency_contacts SCHEMA: min_items=1. Удаление последнего →
        validate отклоняет → «Удаление отменено»."""
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=[{"name": "A", "phone": "01"}])), \
             patch.object(mod.settings_store, "set_value",
                          AsyncMock()) as set_value, \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._obj_delete(event, 42, "emergency_contacts:0")
        assert "Удаление отменено" in send.await_args.kwargs["text"]
        set_value.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_successful_delete_persists_and_audits(self) -> None:
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        items = [
            {"name": "A", "phone": "01"},
            {"name": "B", "phone": "02"},
        ]
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=items)), \
             patch.object(mod.settings_store, "set_value",
                          AsyncMock()) as set_value, \
             patch.object(mod.ops_svc, "write_audit", AsyncMock()) as audit, \
             patch.object(mod, "_show_obj_card", AsyncMock()) as show:
            await mod._obj_delete(event, 55, "emergency_contacts:0")
        set_value.assert_awaited_once()
        assert set_value.await_args.args[2] == [{"name": "B", "phone": "02"}]
        audit.assert_awaited_once()
        akw = audit.await_args.kwargs
        assert akw["action"] == "setting_obj_del"
        assert akw["details"]["removed"] == {"name": "A", "phone": "01"}
        assert akw["details"]["index"] == 0
        show.assert_awaited_once_with(event, "emergency_contacts")


class TestApplyObjAdd:
    """`_apply_obj_add` — парсинг строк ввода в dict-item + валидация.
    Ключевая характеризация форматов emergency/transport."""

    @pytest.mark.asyncio
    async def test_less_than_two_lines_rejected(self) -> None:
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        await mod._apply_obj_add(event, 42, "emergency_contacts", "ТолькоИмя")
        event.bot.send_message.assert_awaited()
        assert "две строки" in event.bot.send_message.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_emergency_two_lines_no_section(self) -> None:
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=[])), \
             patch.object(mod.settings_store, "set_value",
                          AsyncMock()) as set_value, \
             patch.object(mod.ops_svc, "write_audit", AsyncMock()) as audit, \
             patch.object(mod, "_show_obj_card", AsyncMock()):
            await mod._apply_obj_add(
                event, 42, "emergency_contacts", "Полиция\n02"
            )
        set_value.assert_awaited_once()
        saved = set_value.await_args.args[2]
        # Item без section (только name+phone).
        assert saved == [{"name": "Полиция", "phone": "02"}]
        assert audit.await_args.kwargs["action"] == "setting_obj_add"

    @pytest.mark.asyncio
    async def test_emergency_three_lines_with_section(self) -> None:
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=[])), \
             patch.object(mod.settings_store, "set_value",
                          AsyncMock()) as set_value, \
             patch.object(mod.ops_svc, "write_audit", AsyncMock()), \
             patch.object(mod, "_show_obj_card", AsyncMock()):
            await mod._apply_obj_add(
                event, 42, "emergency_contacts",
                "Пожарная служба\n01\nЭкстренные службы",
            )
        saved = set_value.await_args.args[2]
        assert saved == [{
            "name": "Пожарная служба",
            "phone": "01",
            "section": "Экстренные службы",
        }]

    @pytest.mark.asyncio
    async def test_transport_two_lines_routes_phone(self) -> None:
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=[])), \
             patch.object(mod.settings_store, "set_value",
                          AsyncMock()) as set_value, \
             patch.object(mod.ops_svc, "write_audit", AsyncMock()), \
             patch.object(mod, "_show_obj_card", AsyncMock()):
            await mod._apply_obj_add(
                event, 42, "transport_dispatcher_contacts",
                "Автобусы 101, 102\n+7 (415-31) 7-25-29",
            )
        saved = set_value.await_args.args[2]
        assert saved == [{
            "routes": "Автобусы 101, 102",
            "phone": "+7 (415-31) 7-25-29",
        }]

    @pytest.mark.asyncio
    async def test_unsupported_key_rejected(self) -> None:
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        # Две строки есть, но ключ не emergency/transport → ветка else
        # шлёт отказ ДО session_scope.
        await mod._apply_obj_add(event, 42, "weird_key", "line1\nline2")
        event.bot.send_message.assert_awaited()
        assert "не поддерживает" in event.bot.send_message.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_invalid_phone_rejected_by_validation(self) -> None:
        """SECURITY_REVIEW M4: phone-валидация. Телефон-«ник» отклоняется
        validate, ничего не сохраняется."""
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=[])), \
             patch.object(mod.settings_store, "set_value",
                          AsyncMock()) as set_value:
            await mod._apply_obj_add(
                event, 42, "emergency_contacts",
                "Лжеслужба\n@telegram_nick",
            )
        event.bot.send_message.assert_awaited()
        assert "❌" in event.bot.send_message.await_args.kwargs["text"]
        set_value.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════
# 4. _apply_single_edit: валидация, audit before→after, author-vs-text
# ══════════════════════════════════════════════════════════════════════


class TestApplySingleEdit:
    """`_apply_single_edit` — применение нового значения текст/URL-ключа
    с записью полного before→after в audit_log."""

    @pytest.mark.asyncio
    async def test_validation_failure_sends_error_no_persist(self) -> None:
        from aemr_bot.handlers import admin_settings_text as mod

        event = make_event()
        # policy_url: невалидный (не в whitelist) → validate отклоняет.
        await mod._apply_single_edit(
            event, 42, "policy_url", "https://evil.example.com"
        )
        event.bot.send_message.assert_awaited()
        assert "❌" in event.bot.send_message.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_successful_edit_persists_and_audits_before_after(self) -> None:
        from aemr_bot.handlers import admin_settings_text as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value="старый приём")), \
             patch.object(mod.settings_store, "set_value",
                          AsyncMock()) as set_value, \
             patch.object(mod.ops_svc, "write_audit", AsyncMock()) as audit, \
             patch.object(mod, "_show_text_card", AsyncMock()) as show:
            await mod._apply_single_edit(
                event, 88, "appointment_text", "новое расписание приёма"
            )
        set_value.assert_awaited_once()
        assert set_value.await_args.args[2] == "новое расписание приёма"
        audit.assert_awaited_once()
        akw = audit.await_args.kwargs
        assert akw["action"] == "setting_update"
        assert akw["target"] == "appointment_text"
        # before/after clip + длина.
        assert akw["details"]["before"] == "старый приём"
        assert akw["details"]["after"] == "новое расписание приёма"
        assert akw["details"]["len"] == len("новое расписание приёма")
        # Не author-ключ → рисуется text-карточка.
        show.assert_awaited_once_with(event, "appointment_text")

    @pytest.mark.asyncio
    async def test_author_key_renders_author_card(self) -> None:
        from aemr_bot.handlers import admin_settings_text as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=None)), \
             patch.object(mod.settings_store, "set_value", AsyncMock()), \
             patch.object(mod.ops_svc, "write_audit", AsyncMock()), \
             patch.object(mod, "_show_author_card", AsyncMock()) as author, \
             patch.object(mod, "_show_text_card", AsyncMock()) as text_card:
            await mod._apply_single_edit(
                event, 42, "commit_author_name", "Иван Иванов"
            )
        # Для commit_author_* — author-карточка, НЕ text-карточка.
        author.assert_awaited_once_with(event)
        text_card.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════
# 5. _show_author_card
# ══════════════════════════════════════════════════════════════════════


class TestShowAuthorCard:
    """`_show_author_card` — карточка автора коммитов (name/email с
    placeholder'ом если не задано)."""

    @pytest.mark.asyncio
    async def test_renders_values(self) -> None:
        from aemr_bot.handlers import admin_settings_author as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)

        async def fake_get(_session, key):
            return {
                "commit_author_name": "Иван Иванов",
                "commit_author_email": "ivan@elizovomr.ru",
            }.get(key)

        with scope_patch, \
             patch.object(mod.settings_store, "get", side_effect=fake_get), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_author_card(event)
        text = send.await_args.kwargs["text"]
        assert "Иван Иванов" in text
        assert "ivan@elizovomr.ru" in text

    @pytest.mark.asyncio
    async def test_unset_shows_placeholder(self) -> None:
        from aemr_bot.handlers import admin_settings_author as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=None)), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_author_card(event)
        text = send.await_args.kwargs["text"]
        assert "(не задано)" in text


# ══════════════════════════════════════════════════════════════════════
# 6. _route_set_action — оставшиеся ветки (cat, expert, obj_view/add/del,
#    list_del, pr:diff)
# ══════════════════════════════════════════════════════════════════════


class TestRouteSetActionRemainingBranches:
    """Дополняем dispatch-покрытие непокрытыми в handlers-тесте ветками
    `_route_set_action`."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import admin_settings as mod
        mod._edit_intents.clear()
        yield
        mod._edit_intents.clear()

    @pytest.mark.asyncio
    async def test_cat_texts_renders_texts_submenu(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._route_set_action(event, 1, "cat:texts")
        assert "Тексты для жителей" in send.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_cat_urls_renders_urls_submenu(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._route_set_action(event, 1, "cat:urls")
        assert "Внешние ссылки" in send.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_cat_unknown_no_op(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        # cat:gibberish — ни texts, ни urls → функция падает в конец без
        # действия (характеризуем: send не зовётся).
        with patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._route_set_action(event, 1, "cat:gibberish")
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_expert_lists_keys(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "list_keys",
                          AsyncMock(return_value=["welcome_text", "topics"])), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._route_set_action(event, 1, "expert")
        assert "экспертный режим" in send.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_obj_view_dispatches(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "_show_obj_item", AsyncMock()) as show:
            await mod._route_set_action(
                event, 1, "obj_view:emergency_contacts:2"
            )
        show.assert_awaited_once_with(event, "emergency_contacts:2")

    @pytest.mark.asyncio
    async def test_obj_add_dispatches(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "_start_obj_add", AsyncMock()) as start:
            await mod._route_set_action(event, 42, "obj_add:emergency_contacts")
        start.assert_awaited_once_with(event, 42, "emergency_contacts")

    @pytest.mark.asyncio
    async def test_obj_del_dispatches(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "_obj_delete", AsyncMock()) as delete:
            await mod._route_set_action(
                event, 42, "obj_del:emergency_contacts:1"
            )
        delete.assert_awaited_once_with(event, 42, "emergency_contacts:1")

    @pytest.mark.asyncio
    async def test_list_del_dispatches(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "_list_delete", AsyncMock()) as delete:
            await mod._route_set_action(event, 42, "list_del:topics:0")
        delete.assert_awaited_once_with(event, 42, "topics:0")

    @pytest.mark.asyncio
    async def test_pr_diff_dispatches(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        with patch.object(mod, "_show_pr_diff", AsyncMock()) as show:
            await mod._route_set_action(event, 1, "pr:diff")
        show.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_cancel_quiet_hour_key_shows_text_card(self) -> None:
        """cancel:<key> для не-author ключа (включая quiet-hour ключи) →
        drop intent + text-карточка (характеризуем фактическую ветку)."""
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        mod._intent_set(42, key="admin_quiet_hours_start", kind="quiet_hour",
                        which="start")
        with patch.object(mod, "_show_text_card", AsyncMock()) as show:
            await mod._route_set_action(
                event, 42, "cancel:admin_quiet_hours_start"
            )
        assert mod._intent_get(42) is None
        show.assert_awaited_once_with(event, "admin_quiet_hours_start")


# ══════════════════════════════════════════════════════════════════════
# 7. PR-flow: _show_pr_confirm, _create_pr, _show_pr_diff
# ══════════════════════════════════════════════════════════════════════


class TestShowPrConfirm:
    """`_show_pr_confirm` — экран подтверждения PR: «нет изменений»,
    blockers (PAT/автор), happy-path."""

    @pytest.mark.asyncio
    async def test_no_dirty_keys_shows_nothing_to_sync(self) -> None:
        from aemr_bot.handlers import admin_settings_pr as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get_dirty_keys",
                          AsyncMock(return_value=[])), \
             patch.object(mod.settings_store, "get", AsyncMock(return_value="x")), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_pr_confirm(event)
        assert "Нет несинхронизированных изменений" in send.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_blockers_when_pat_and_author_missing(self) -> None:
        from aemr_bot.handlers import admin_settings_pr as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)

        async def fake_get(_session, key):
            # Автор не задан → name/email None.
            return None

        with scope_patch, \
             patch.object(mod.settings_store, "get_dirty_keys",
                          AsyncMock(return_value=["policy_url"])), \
             patch.object(mod.settings_store, "get", side_effect=fake_get), \
             patch.dict(mod.os.environ, {}, clear=False), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            # Гарантируем отсутствие GITHUB_PAT.
            mod.os.environ.pop("GITHUB_PAT", None)
            await mod._show_pr_confirm(event)
        text = send.await_args.kwargs["text"]
        assert "Нельзя создать PR" in text
        assert "GITHUB_PAT" in text
        assert "автор" in text.lower()

    @pytest.mark.asyncio
    async def test_happy_path_shows_confirm(self) -> None:
        from aemr_bot.handlers import admin_settings_pr as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)

        async def fake_get(_session, key):
            return {
                "commit_author_name": "Бот Елизово",
                "commit_author_email": "bot@elizovomr.ru",
            }.get(key)

        with scope_patch, \
             patch.object(mod.settings_store, "get_dirty_keys",
                          AsyncMock(return_value=["policy_url", "topics"])), \
             patch.object(mod.settings_store, "get", side_effect=fake_get), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            mod.os.environ["GITHUB_PAT"] = "ghp_dummy"
            try:
                await mod._show_pr_confirm(event)
            finally:
                mod.os.environ.pop("GITHUB_PAT", None)
        text = send.await_args.kwargs["text"]
        assert "Создать PR с изменениями" in text
        assert "Бот Елизово" in text
        assert "2 ключей" in text


class TestCreatePr:
    """`_create_pr` — создание PR через repo_sync: no-config, no-dirty,
    repo-fail, happy-path (mark_synced + audit).

    `_create_pr` делает `from aemr_bot.services import repo_sync` ВНУТРИ
    функции, поэтому патчим атрибуты РЕАЛЬНОГО модуля repo_sync (а не
    подменяем sys.modules — тот ненадёжен для `from pkg import sub`,
    т.к. submodule кешируется как атрибут пакета). Это прямой namespace
    реальной функции — устойчиво к порядку тестов (урок PR #139)."""

    @pytest.mark.asyncio
    async def test_no_repo_config_shows_error(self) -> None:
        from aemr_bot.handlers import admin_settings_pr as mod
        from aemr_bot.services import repo_sync

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get_dirty_keys",
                          AsyncMock(return_value=["policy_url"])), \
             patch.object(mod.settings_store, "export_synced",
                          AsyncMock(return_value={})), \
             patch.object(mod.settings_store, "get", AsyncMock(return_value="x")), \
             patch.object(mod.ops_svc, "get",
                          AsyncMock(return_value=SimpleNamespace(full_name="Оп"))), \
             patch.object(repo_sync, "load_config_from_env_and_settings",
                          MagicMock(return_value=None)), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._create_pr(event, 42)
        assert "Не настроено GitHub-подключение" in send.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_dirty_empty_after_config_shows_nothing(self) -> None:
        from aemr_bot.handlers import admin_settings_pr as mod
        from aemr_bot.services import repo_sync

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get_dirty_keys",
                          AsyncMock(return_value=[])), \
             patch.object(mod.settings_store, "export_synced",
                          AsyncMock(return_value={})), \
             patch.object(mod.settings_store, "get", AsyncMock(return_value="x")), \
             patch.object(mod.ops_svc, "get",
                          AsyncMock(return_value=SimpleNamespace(full_name="Оп"))), \
             patch.object(repo_sync, "load_config_from_env_and_settings",
                          MagicMock(return_value=SimpleNamespace(repo="o/r"))), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._create_pr(event, 42)
        assert "Нет несинхронизированных изменений" in send.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_repo_failure_shows_reason(self) -> None:
        from aemr_bot.handlers import admin_settings_pr as mod
        from aemr_bot.services import repo_sync

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        fail_result = SimpleNamespace(
            ok=False, reason="branch_failed", message="не смог создать ветку",
            pr_number=None, pr_url=None, branch=None,
        )
        with scope_patch, \
             patch.object(mod.settings_store, "get_dirty_keys",
                          AsyncMock(return_value=["policy_url"])), \
             patch.object(mod.settings_store, "export_synced",
                          AsyncMock(return_value={"policy_url": "x"})), \
             patch.object(mod.settings_store, "get", AsyncMock(return_value="x")), \
             patch.object(mod.ops_svc, "get",
                          AsyncMock(return_value=SimpleNamespace(full_name="Оп"))), \
             patch.object(repo_sync, "load_config_from_env_and_settings",
                          MagicMock(return_value=SimpleNamespace(repo="o/r"))), \
             patch.object(repo_sync, "create_settings_pr",
                          AsyncMock(return_value=fail_result)), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._create_pr(event, 42)
        text = send.await_args.kwargs["text"]
        assert "Не удалось создать PR" in text
        assert "branch_failed" in text

    @pytest.mark.asyncio
    async def test_happy_path_marks_synced_and_audits(self) -> None:
        from aemr_bot.handlers import admin_settings_pr as mod
        from aemr_bot.services import repo_sync

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        ok_result = SimpleNamespace(
            ok=True, reason="", message="PR #7 создан",
            pr_number=7, pr_url="https://github.com/o/r/pull/7",
            branch="settings/2026",
        )
        with scope_patch, \
             patch.object(mod.settings_store, "get_dirty_keys",
                          AsyncMock(return_value=["policy_url", "topics"])), \
             patch.object(mod.settings_store, "export_synced",
                          AsyncMock(return_value={"policy_url": "x"})), \
             patch.object(mod.settings_store, "get", AsyncMock(return_value="x")), \
             patch.object(mod.settings_store, "mark_synced",
                          AsyncMock()) as mark, \
             patch.object(mod.ops_svc, "get",
                          AsyncMock(return_value=SimpleNamespace(full_name="Оп Иванов"))), \
             patch.object(mod.ops_svc, "write_audit", AsyncMock()) as audit, \
             patch.object(repo_sync, "load_config_from_env_and_settings",
                          MagicMock(return_value=SimpleNamespace(repo="o/r"))), \
             patch.object(repo_sync, "create_settings_pr",
                          AsyncMock(return_value=ok_result)), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._create_pr(event, 42)
        # mark_synced вызван с dirty-ключами.
        mark.assert_awaited_once()
        assert mark.await_args.args[1] == ["policy_url", "topics"]
        # Audit settings_pr_created с pr_number/pr_url/branch/keys.
        audit.assert_awaited_once()
        akw = audit.await_args.kwargs
        assert akw["action"] == "settings_pr_created"
        assert akw["details"]["pr_number"] == 7
        assert akw["details"]["pr_url"] == "https://github.com/o/r/pull/7"
        assert akw["details"]["branch"] == "settings/2026"
        assert akw["details"]["keys"] == ["policy_url", "topics"]
        # Финальный success-экран с номером PR.
        assert "PR создан: #7" in send.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_operator_name_fallback_when_no_record(self) -> None:
        """`ops_svc.get` вернул None → operator_name = «id=<N>»."""
        from aemr_bot.handlers import admin_settings_pr as mod
        from aemr_bot.services import repo_sync

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        ok_result = SimpleNamespace(
            ok=True, reason="", message="ok",
            pr_number=1, pr_url="u", branch="b",
        )
        create_mock = AsyncMock(return_value=ok_result)
        with scope_patch, \
             patch.object(mod.settings_store, "get_dirty_keys",
                          AsyncMock(return_value=["policy_url"])), \
             patch.object(mod.settings_store, "export_synced",
                          AsyncMock(return_value={})), \
             patch.object(mod.settings_store, "get", AsyncMock(return_value="x")), \
             patch.object(mod.settings_store, "mark_synced", AsyncMock()), \
             patch.object(mod.ops_svc, "get", AsyncMock(return_value=None)), \
             patch.object(mod.ops_svc, "write_audit", AsyncMock()), \
             patch.object(repo_sync, "load_config_from_env_and_settings",
                          MagicMock(return_value=SimpleNamespace(repo="o/r"))), \
             patch.object(repo_sync, "create_settings_pr", create_mock), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()):
            await mod._create_pr(event, 12345)
        # operator_name проброшен в create_settings_pr как «id=12345».
        assert create_mock.await_args.kwargs["operator_name"] == "id=12345"


class TestShowPrDiff:
    """`_show_pr_diff` — сравнение локальных настроек с main в репо."""

    @pytest.mark.asyncio
    async def test_no_pat_shows_local_dirty_only(self) -> None:
        from aemr_bot.handlers import admin_settings_pr as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get_dirty_keys",
                          AsyncMock(return_value=["policy_url"])), \
             patch.object(mod.settings_store, "export_synced",
                          AsyncMock(return_value={})), \
             patch.object(mod.settings_store, "get", AsyncMock(return_value=None)), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            mod.os.environ.pop("GITHUB_PAT", None)
            await mod._show_pr_diff(event)
        text = send.await_args.kwargs["text"]
        assert "GITHUB_PAT не задан" in text
        assert "policy_url" in text

    @pytest.mark.asyncio
    async def test_remote_not_in_repo_first_pr_hint(self) -> None:
        from aemr_bot.handlers import admin_settings_pr as mod
        from aemr_bot.services import repo_sync

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get_dirty_keys",
                          AsyncMock(return_value=[])), \
             patch.object(mod.settings_store, "export_synced",
                          AsyncMock(return_value={})), \
             patch.object(mod.settings_store, "get", AsyncMock(return_value="x")), \
             patch.object(repo_sync, "load_config_from_env_and_settings",
                          MagicMock(return_value=SimpleNamespace(repo="o/r"))), \
             patch.object(repo_sync, "fetch_main_runtime_config",
                          AsyncMock(return_value=(None, "not_in_repo"))), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            mod.os.environ["GITHUB_PAT"] = "ghp_dummy"
            try:
                await mod._show_pr_diff(event)
            finally:
                mod.os.environ.pop("GITHUB_PAT", None)
        assert "Первый PR создаст его" in send.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_remote_fetch_error_shows_reason(self) -> None:
        from aemr_bot.handlers import admin_settings_pr as mod
        from aemr_bot.services import repo_sync

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get_dirty_keys",
                          AsyncMock(return_value=[])), \
             patch.object(mod.settings_store, "export_synced",
                          AsyncMock(return_value={})), \
             patch.object(mod.settings_store, "get", AsyncMock(return_value="x")), \
             patch.object(repo_sync, "load_config_from_env_and_settings",
                          MagicMock(return_value=SimpleNamespace(repo="o/r"))), \
             patch.object(repo_sync, "fetch_main_runtime_config",
                          AsyncMock(return_value=(None, "http_500"))), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            mod.os.environ["GITHUB_PAT"] = "ghp_dummy"
            try:
                await mod._show_pr_diff(event)
            finally:
                mod.os.environ.pop("GITHUB_PAT", None)
        assert "Не удалось скачать из репо" in send.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_diff_identical_reports_match(self) -> None:
        from aemr_bot.handlers import admin_settings_pr as mod
        from aemr_bot.services import repo_sync

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        # local и remote совпадают по всем SYNCED_KEYS → «всё одинаково».
        shared = {k: f"val-{k}" for k in mod.settings_store.SYNCED_KEYS}
        with scope_patch, \
             patch.object(mod.settings_store, "get_dirty_keys",
                          AsyncMock(return_value=[])), \
             patch.object(mod.settings_store, "export_synced",
                          AsyncMock(return_value=dict(shared))), \
             patch.object(mod.settings_store, "get", AsyncMock(return_value="x")), \
             patch.object(repo_sync, "load_config_from_env_and_settings",
                          MagicMock(return_value=SimpleNamespace(repo="o/r"))), \
             patch.object(repo_sync, "fetch_main_runtime_config",
                          AsyncMock(return_value=(dict(shared), None))), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            mod.os.environ["GITHUB_PAT"] = "ghp_dummy"
            try:
                await mod._show_pr_diff(event)
            finally:
                mod.os.environ.pop("GITHUB_PAT", None)
        assert "всё одинаково" in send.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_diff_mismatch_lists_keys(self) -> None:
        from aemr_bot.handlers import admin_settings_pr as mod
        from aemr_bot.services import repo_sync

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        local = {k: f"local-{k}" for k in mod.settings_store.SYNCED_KEYS}
        remote = {k: f"remote-{k}" for k in mod.settings_store.SYNCED_KEYS}
        with scope_patch, \
             patch.object(mod.settings_store, "get_dirty_keys",
                          AsyncMock(return_value=[])), \
             patch.object(mod.settings_store, "export_synced",
                          AsyncMock(return_value=local)), \
             patch.object(mod.settings_store, "get", AsyncMock(return_value="x")), \
             patch.object(repo_sync, "load_config_from_env_and_settings",
                          MagicMock(return_value=SimpleNamespace(repo="o/r"))), \
             patch.object(repo_sync, "fetch_main_runtime_config",
                          AsyncMock(return_value=(remote, None))), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            mod.os.environ["GITHUB_PAT"] = "ghp_dummy"
            try:
                await mod._show_pr_diff(event)
            finally:
                mod.os.environ.pop("GITHUB_PAT", None)
        text = send.await_args.kwargs["text"]
        assert "Различаются" in text
        # Все SYNCED_KEYS отличаются.
        assert "policy_url" in text


# ══════════════════════════════════════════════════════════════════════
# 8. _show_expert_key (legacy op:setkey:<key>)
# ══════════════════════════════════════════════════════════════════════


class TestShowExpertKey:
    """`_show_expert_key` — старая экспертная карточка ключа."""

    @pytest.mark.asyncio
    async def test_empty_key_acks_only(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        # payload без key после префикса → ack + return, без screen.
        with patch.object(mod, "ack_callback", AsyncMock()) as ack, \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_expert_key(event, "op:setkey:")
        ack.assert_awaited_once()
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_known_key_renders_value_and_type(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=["ЖКХ", "Дороги"])), \
             patch.object(mod, "ack_callback", AsyncMock()) as ack, \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_expert_key(event, "op:setkey:topics")
        ack.assert_awaited_once()
        text = send.await_args.kwargs["text"]
        assert "topics" in text
        assert "list" in text  # тип list из SCHEMA
        assert "ЖКХ" in text  # значение как JSON

    @pytest.mark.asyncio
    async def test_none_value_renders_dash(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=None)), \
             patch.object(mod, "ack_callback", AsyncMock()), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_expert_key(event, "op:setkey:welcome_text")
        assert "—" in send.await_args.kwargs["text"]
