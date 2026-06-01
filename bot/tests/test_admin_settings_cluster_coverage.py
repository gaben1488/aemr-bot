"""Добивка покрытия кластера «⚙️ Настройки бота» — остаточные ветки.

Дополняет `test_admin_settings_handlers.py` (intent-lifecycle, dispatch,
quiet-wizard), `test_admin_settings_characterization.py` (карточки, CRUD,
apply-пути, PR-flow) и `test_admin_settings_audit.py` (`_clip_audit_value`).
Здесь — узкие ветки, не закрытые широким characterization-набором, по
факту term-missing на момент написания:

- `admin_settings.py`: preview «и ещё N» при >5 dirty (123); callback с
  `get_user_id is None` → ack без dispatch (155-156); усечение значения
  >1500 симв в `_show_expert_key` (318).
- `admin_settings_text.py`: `_show_text_card` для НЕ-str ключа (int) —
  блок constraints пропускается (65->70); `_start_edit_intent` для
  НЕ-url/НЕ-str ключа — подсказка пустая, но intent ставится (96->99).
- `admin_settings_list.py`: `_apply_list_add`, когда `settings_store.get`
  вернул не-list → приведение к [] (108).
- `admin_settings_obj.py`: `_show_obj_card` с не-list (37) и с ключом
  без key-specific hint (56->64); `_apply_obj_add` с не-list (201).
- `admin_settings_pr.py`: `_show_pr_confirm` preview «…и ещё N» при >10
  dirty (54); `_show_pr_diff`, когда PAT есть, но `cfg_repo is None`
  (196-201).
- `admin_settings_quiet.py`: `_show_quiet_card`, когда start/end не int
  → дефолты 18/9 (45, 47).

Методология та же, что у соседних characterization-файлов (Майкл Физерс):
закрепляем ФАКТИЧЕСКОЕ поведение узких веток, без тавтологий. Моки —
`SimpleNamespace`/`make_event` из `tests._helpers`, `session_scope`
подменяется локальным CM; `settings_store.*` / `ops_svc.*` /
`send_or_edit_screen` патчатся ПО МЕСТУ их использования в подмодуле, где
живёт leaf-функция (урок PR #139: фасадный re-export не перехватывается
patch'ем по старому namespace). Клавиатуры (pure-функции) не мокаем.

Ключи берутся из реальной `settings_store.SCHEMA` (источник истины):
`broadcast_max_images` / `admin_quiet_hours_start` — тип int (НЕ str,
НЕ url); `policy_url` — url-flag; `welcome_text` — str c max_len.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


pytest.importorskip("maxapi", reason="нужен maxapi для admin_settings импортов")

from tests._helpers import make_callback_event, make_event


def _patch_scope(mod, session=None):
    """patch-объект для `mod.session_scope`, чей `async with` отдаёт
    `session` (MagicMock по умолчанию). Та же форма, что в соседних
    characterization-файлах кластера."""
    sess = MagicMock() if session is None else session

    @asynccontextmanager
    async def _cm():
        yield sess

    return patch.object(mod, "session_scope", _cm), sess


# ══════════════════════════════════════════════════════════════════════
# 1. admin_settings.py (фасад): preview >5 dirty, callback no-user, clip
# ══════════════════════════════════════════════════════════════════════


class TestRunSettingsMenuManyDirty:
    """`run_settings_menu` — ветка `len(dirty) > 5` дорисовывает хвост
    «и ещё N» к keys_preview (строка 123)."""

    @pytest.mark.asyncio
    async def test_more_than_five_dirty_keys_appends_remainder(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        # 7 dirty-ключей: preview показывает первые 5 + «и ещё 2».
        dirty = [f"key_{i}" for i in range(7)]
        with patch.object(mod, "ensure_role", AsyncMock(return_value=True)), \
             patch.object(mod, "session_scope") as scope, \
             patch.object(mod.settings_store, "get_dirty_keys",
                          AsyncMock(return_value=dirty)), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            await mod.run_settings_menu(event)
        text = send.await_args.kwargs["text"]
        assert "Не выгружено в репо: 7" in text
        # Хвост «и ещё 2» (7 - 5).
        assert "и ещё 2" in text
        # 6-й и 7-й ключ НЕ показаны поимённо (только в счётчике).
        assert "key_6" not in text


class TestRunSettingsActionNoUser:
    """`run_settings_action` — `get_user_id` вернул None: ack + return,
    диспетчер `_route_set_action` не вызывается (строки 155-156)."""

    @pytest.mark.asyncio
    async def test_no_user_id_acks_and_skips_dispatch(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_callback_event(payload="op:set:author")
        with patch.object(mod, "ensure_role", AsyncMock(return_value=True)), \
             patch.object(mod, "get_user_id", return_value=None), \
             patch.object(mod, "ack_callback", AsyncMock()) as ack, \
             patch.object(mod, "_route_set_action", AsyncMock()) as route:
            await mod.run_settings_action(event, "op:set:author")
        # Ack отправлен (callback закрыт), но дальше не пошли.
        ack.assert_awaited_once()
        route.assert_not_awaited()


class TestShowExpertKeyClip:
    """`_show_expert_key` — значение длиннее 1500 симв усекается с
    маркером «…(значение обрезано)» (строки 317-318)."""

    @pytest.mark.asyncio
    async def test_long_value_truncated_with_marker(self) -> None:
        from aemr_bot.handlers import admin_settings as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        # Длинная строка > 1500: json.dumps добавит кавычки, итог >1500.
        long_value = "э" * 2000
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=long_value)), \
             patch.object(mod, "ack_callback", AsyncMock()), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_expert_key(event, "op:setkey:welcome_text")
        text = send.await_args.kwargs["text"]
        assert "…(значение обрезано)" in text


# ══════════════════════════════════════════════════════════════════════
# 2. admin_settings_text.py: НЕ-str ключ (skip constraints / skip hint)
# ══════════════════════════════════════════════════════════════════════


class TestShowTextCardNonStrKey:
    """`_show_text_card` для ключа c типом НЕ str (int): блок constraints
    (max_len / url-подсказка) пропускается — ветка `expected is str`
    ложна (65->70)."""

    @pytest.mark.asyncio
    async def test_int_key_skips_constraints_block(self) -> None:
        from aemr_bot.handlers import admin_settings_text as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        # admin_quiet_hours_start — тип int в SCHEMA → constraints не
        # добавляются (нет «Лимит:», нет «http://»).
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=18)), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_text_card(event, "admin_quiet_hours_start")
        text = send.await_args.kwargs["text"]
        # Тип int отрисован, значение есть, но constraints-строк нет.
        assert "int" in text
        assert "18" in text
        assert "Лимит:" not in text
        assert "http://" not in text


class TestStartEditIntentNonStrNonUrlKey:
    """`_start_edit_intent` для ключа из SCHEMA, который НЕ url и НЕ str
    (int): гард пройден, intent ставится, но подсказка пустая — обе
    elif-ветки ложны (96->99)."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import admin_settings_text as mod
        mod._edit_intents.clear()
        yield
        mod._edit_intents.clear()

    @pytest.mark.asyncio
    async def test_int_key_sets_intent_without_hint(self) -> None:
        from aemr_bot.handlers import admin_settings_text as mod

        event = make_event()
        with patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            # broadcast_max_images есть в SCHEMA (тип int) → гард пройден,
            # intent ставится; ни url-hint, ни max_len-hint не добавляются.
            await mod._start_edit_intent(event, 42, "broadcast_max_images")
        intent = mod._intent_get(42)
        assert intent is not None
        assert intent["key"] == "broadcast_max_images"
        assert intent["kind"] == "single"
        text = send.await_args.kwargs["text"]
        # Заголовок редактирования есть, но НЕТ url-подсказки и НЕТ
        # «до N симв» (max_len-hint только для str).
        assert "Редактирование" in text
        assert "URL" not in text
        assert "симв" not in text


# ══════════════════════════════════════════════════════════════════════
# 3. admin_settings_list.py: _apply_list_add c не-list из store (108)
# ══════════════════════════════════════════════════════════════════════


class TestApplyListAddNonListCoercion:
    """`_apply_list_add` — `settings_store.get` вернул не-list (мусор в
    БД): handler приводит к [] и добавляет первый элемент (строка 108)."""

    @pytest.mark.asyncio
    async def test_non_list_value_coerced_then_added(self) -> None:
        from aemr_bot.handlers import admin_settings_list as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value="мусор-не-список")), \
             patch.object(mod.settings_store, "set_value",
                          AsyncMock()) as set_value, \
             patch.object(mod.ops_svc, "write_audit", AsyncMock()) as audit, \
             patch.object(mod, "_show_list_card", AsyncMock()) as show:
            await mod._apply_list_add(event, 42, "topics", "ЖКХ")
        # Старое (не-list) отброшено → сохранён список ровно с новым.
        set_value.assert_awaited_once()
        assert set_value.await_args.args[2] == ["ЖКХ"]
        audit.assert_awaited_once()
        assert audit.await_args.kwargs["details"]["added"] == "ЖКХ"
        show.assert_awaited_once_with(event, "topics")


# ══════════════════════════════════════════════════════════════════════
# 4. admin_settings_obj.py: не-list (37, 201) + ключ без hint (56->64)
# ══════════════════════════════════════════════════════════════════════


class TestShowObjCardEdgeBranches:
    """`_show_obj_card` — не-list из store → [] (37); ключ, не входящий
    ни в emergency, ни в transport → hint остаётся пустым (56->64)."""

    @pytest.mark.asyncio
    async def test_non_list_value_coerced_to_empty(self) -> None:
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value={"not": "a list"})), \
             patch.object(mod.settings_store, "format_obj_list",
                          MagicMock(return_value="(пусто)")) as fmt, \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_obj_card(event, "emergency_contacts")
        # format_obj_list получил [] (не-list приведён).
        fmt.assert_called_once_with([])
        text = send.await_args.kwargs["text"]
        assert "(0)" in text

    @pytest.mark.asyncio
    async def test_unknown_key_no_format_hint(self) -> None:
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        # Ключ не emergency/transport → ни «раздел», ни «маршруты» в hint;
        # заголовок падает на сам key (нет в title_map).
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value=[])), \
             patch.object(mod.settings_store, "format_obj_list",
                          MagicMock(return_value="(пусто)")), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_obj_card(event, "some_unknown_obj_key")
        text = send.await_args.kwargs["text"]
        assert "some_unknown_obj_key" in text
        assert "Формат добавления" not in text


class TestApplyObjAddNonListCoercion:
    """`_apply_obj_add` — `settings_store.get` вернул не-list: приводим
    к [] перед append (строка 201)."""

    @pytest.mark.asyncio
    async def test_non_list_value_coerced_then_item_added(self) -> None:
        from aemr_bot.handlers import admin_settings_obj as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch.object(mod.settings_store, "get",
                          AsyncMock(return_value="garbage")), \
             patch.object(mod.settings_store, "validate",
                          MagicMock(return_value=(True, ""))), \
             patch.object(mod.settings_store, "set_value",
                          AsyncMock()) as set_value, \
             patch.object(mod.ops_svc, "write_audit", AsyncMock()), \
             patch.object(mod, "_show_obj_card", AsyncMock()):
            await mod._apply_obj_add(
                event, 42, "transport_dispatcher_contacts",
                "Автобус 1\n+7 (415) 000-00-00",
            )
        set_value.assert_awaited_once()
        # Старый мусор отброшен → ровно один новый item.
        assert set_value.await_args.args[2] == [
            {"routes": "Автобус 1", "phone": "+7 (415) 000-00-00"}
        ]


# ══════════════════════════════════════════════════════════════════════
# 5. admin_settings_pr.py: >10 dirty preview (54) + diff cfg None (196-201)
# ══════════════════════════════════════════════════════════════════════


class TestShowPrConfirmManyDirty:
    """`_show_pr_confirm` — >10 dirty-ключей: preview обрезается до 10 +
    хвост «…и ещё N» (строки 53-54). PAT и автор заданы → happy-path
    (без blockers), чтобы добраться до строки с хвостом."""

    @pytest.mark.asyncio
    async def test_more_than_ten_dirty_keys_truncates_preview(self) -> None:
        from aemr_bot.handlers import admin_settings_pr as mod

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        dirty = [f"k_{i}" for i in range(13)]

        async def fake_get(_session, key):
            return {
                "commit_author_name": "Бот",
                "commit_author_email": "bot@elizovomr.ru",
            }.get(key)

        with scope_patch, \
             patch.object(mod.settings_store, "get_dirty_keys",
                          AsyncMock(return_value=dirty)), \
             patch.object(mod.settings_store, "get", side_effect=fake_get), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            mod.os.environ["GITHUB_PAT"] = "ghp_dummy"
            try:
                await mod._show_pr_confirm(event)
            finally:
                mod.os.environ.pop("GITHUB_PAT", None)
        text = send.await_args.kwargs["text"]
        # Хвост «…и ещё 3» (13 - 10).
        assert "…и ещё 3" in text
        # 13-й ключ (индекс 12) не показан поимённо.
        assert "k_12" not in text


class TestShowPrDiffConfigNone:
    """`_show_pr_diff` — PAT есть, но `load_config_from_env_and_settings`
    вернул None: ранний выход с «Не настроено GitHub-подключение»
    (строки 195-201), ДО fetch_main_runtime_config."""

    @pytest.mark.asyncio
    async def test_pat_present_but_config_none_shows_error(self) -> None:
        from aemr_bot.handlers import admin_settings_pr as mod
        from aemr_bot.services import repo_sync

        event = make_event()
        scope_patch, _ = _patch_scope(mod)
        fetch_mock = AsyncMock(return_value=(None, "unused"))
        with scope_patch, \
             patch.object(mod.settings_store, "get_dirty_keys",
                          AsyncMock(return_value=["policy_url"])), \
             patch.object(mod.settings_store, "export_synced",
                          AsyncMock(return_value={})), \
             patch.object(mod.settings_store, "get", AsyncMock(return_value=None)), \
             patch.object(repo_sync, "load_config_from_env_and_settings",
                          MagicMock(return_value=None)), \
             patch.object(repo_sync, "fetch_main_runtime_config", fetch_mock), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            mod.os.environ["GITHUB_PAT"] = "ghp_dummy"
            try:
                await mod._show_pr_diff(event)
            finally:
                mod.os.environ.pop("GITHUB_PAT", None)
        assert "Не настроено GitHub-подключение" in send.await_args.kwargs["text"]
        # До скачивания из репо не дошли — config был None.
        fetch_mock.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════
# 6. admin_settings_quiet.py: start/end не int → дефолты 18/9 (45, 47)
# ══════════════════════════════════════════════════════════════════════


class TestShowQuietCardNonIntHours:
    """`_show_quiet_card` — start/end в БД не int (None/мусор): handler
    подставляет дефолты 18 и 9 (строки 44-47), окно рисуется как
    18:00–09:00."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.services import quiet_hours
        quiet_hours.reset_cache_for_tests()
        yield
        quiet_hours.reset_cache_for_tests()

    @pytest.mark.asyncio
    async def test_non_int_start_end_fall_back_to_defaults(self) -> None:
        from aemr_bot.handlers import admin_settings_quiet as mod

        event = make_event()

        async def fake_get(_session, key):
            # enabled=True, но start/end — не int (None) → дефолты 18/9.
            return {"admin_quiet_hours_enabled": True}.get(key)

        scope_patch, _ = _patch_scope(mod)
        with scope_patch, \
             patch("aemr_bot.services.quiet_hours.refresh_cache_from_db",
                   AsyncMock()), \
             patch.object(mod.settings_store, "get", side_effect=fake_get), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._show_quiet_card(event)
        text = send.await_args.kwargs["text"]
        # Дефолтное окно 18:00–09:00.
        assert "18:00" in text
        assert "09:00" in text
        assert "включён" in text
