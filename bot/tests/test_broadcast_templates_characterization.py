"""Характеризационные тесты для handlers/broadcast_templates.py.

Фиксируют ТЕКУЩЕЕ поведение god-object'а (~1232 строки: wizard шаблонов
рассылок, CRUD, список с поиском, use_count) как страховочную сетку
перед декомпозицией. Методология — Майкл Физерс: тесты закрепляют
наблюдаемые контракты как есть, включая «странности» (например, что
`_open` при `use_count and not last_used_at` показывает ПУСТУЮ
last-used-строку; что `_step_edit` с пустым текстом + новыми картинками
запускает image-only-замену; что `_save_new` на занятом имени возвращает
wizard в шаг ввода имени, а на `ValueError` — дропает wizard целиком).
Прод-код не трогаем — правим только тест.

Существующий `test_broadcast_templates_handlers.py` покрывает разводку
`handle_callback` (через моки внутренних `_list`/`_open`/...) и пару
happy-path'ов wizard-текста. Здесь — ДОПОЛНЯЮЩАЯ сетка: реальные
внутренние функции end-to-end, роль-гарды, ошибки/not-found, валидация,
запись в audit_log (operators_service.write_audit).

Стиль — как существующие `test_*characterization.py`: SimpleNamespace-
фейки через `tests._helpers.make_event`, без реального Postgres; мокаем
`session_scope` (через `_fake_session_scope`), `templates_service.*`,
`operators_service.write_audit`, `ensure_role`, `get_operator`,
`send_or_edit_screen`. Клавиатуры (pure-функции из `aemr_bot.keyboards`)
НЕ мокаем — дёшевы, заодно ловят регрессии payload'ов.

ВАЖНО (урок PR #139): все патчи нацелены на символы ПО МЕСТУ
использования в `aemr_bot.handlers.broadcast_templates` (а не в исходном
модуле). После будущего извлечения функций эти patch-точки сместятся;
карта repoint — в плане декомпозиции, возвращаемом задачей, не в этом
файле.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")

# Модуль под тестом импортируем один раз — короткий алиас bt.
from aemr_bot.handlers import broadcast_templates as bt  # noqa: E402

# Подмодули как объекты: фасад `bt` после декомпозиции больше НЕ
# re-export'ит `templates_service` (модуль `aemr_bot.services.
# broadcast_templates`), поэтому исключения/константы (TemplateNameAlready
# Exists, TemplateNotFound, MAX_NAME_LEN) берём через тот подмодуль, где
# leaf-функция резолвит зависимость — `bt_crud` для crud-функций,
# `bt_wiz` для wizard-функций.
from aemr_bot.handlers import broadcast_templates_crud as bt_crud  # noqa: E402
from aemr_bot.handlers import broadcast_templates_wizard as bt_wiz  # noqa: E402

# Фасад: сюда нацелены патчи ДИСПЕТЧЕРНОГО уровня — символы, которые
# резолвят сами `handle_callback`/`handle_wizard_text` (is_admin_chat,
# ack_callback, get_user_id, get_operator) и заглушки leaf-функций
# (`_list`, `_step_search`, ...), вызываемых диспетчером по имени фасада.
_BT = "aemr_bot.handlers.broadcast_templates"

# Подмодули после декомпозиции (DDD tactical, 2026-06-01). Когда тест
# вызывает leaf-функцию НАПРЯМУЮ (`bt._open(...)`, `bt._save_new(...)`),
# её ВНУТРЕННИЕ зависимости (session_scope, templates_service,
# operators_service, _image_attachments, ensure_role, get_operator,
# send_or_edit_screen, broadcast_handler, ...) резолвятся в подмодуле, где
# функция определена — патчим ПО МЕСТУ их резолва (урок PR #139):
#   - list:  _list, _open, _apply, _start_search, _step_search
#   - crud:  _ask_delete, _do_delete, _start_rename, _step_rename,
#            _start_clone, _step_clone_name
#   - wiz:   _start_new, _start_edit, _step_new_name, _step_new_text,
#            _save_new, _save_edit, _render_preview_*, _back_to_*, _cancel
# Разделяемое состояние (_wizards, _apply_dedupe) — единый объект,
# re-export'ится фасадом, поэтому `bt._wizards` и подмодульный — одно и то
# же; обращаться можно через `bt.`.
_LIST = "aemr_bot.handlers.broadcast_templates_list"
_CRUD = "aemr_bot.handlers.broadcast_templates_crud"
_WIZ = "aemr_bot.handlers.broadcast_templates_wizard"


def _make_event(*, user_id: int = 7, chat_id: int = 123) -> SimpleNamespace:
    """MAX-событие для handler'ов шаблонов.

    chat_id=123 совпадает с ADMIN_GROUP_ID из conftest, но `is_admin_chat`
    мы и так мокаем в wizard-путях. `message.answer` — отдельный AsyncMock
    (часть путей шлёт через него, часть — через send_or_edit_screen).
    """
    event = make_event(chat_id=chat_id, user_id=user_id, with_callback=True)
    event.message.answer = AsyncMock()
    return event


@pytest.fixture(autouse=True)
def _clean_state():
    """Изоляция глобального состояния между тестами: in-memory wizard'ы и
    apply-dedupe — модульные синглтоны, утечка ломает соседние тесты."""
    bt._wizards.clear()
    bt._apply_dedupe.clear()
    yield
    bt._wizards.clear()
    bt._apply_dedupe.clear()


def _tmpl(
    *,
    tid: int = 1,
    name: str = "Отключение воды",
    text: str = "Уважаемые жители!",
    attachments=None,
    use_count: int = 0,
    last_used_at=None,
    created_at=None,
):
    """Фейк-строка BroadcastTemplate как SimpleNamespace.

    Все поля, которые читает handler: id, name, text, attachments,
    use_count, last_used_at, created_at.
    """
    return SimpleNamespace(
        id=tid,
        name=name,
        text=text,
        attachments=attachments if attachments is not None else [],
        use_count=use_count,
        last_used_at=last_used_at,
        created_at=created_at,
    )


# ══════════════════════════════════════════════════════════════════════
# 1. Роль-гарды: ensure_role(IT, COORDINATOR) на каждой точке входа.
#    Закрепляем: при отказе функция выходит ДО session_scope/побочек.
# ══════════════════════════════════════════════════════════════════════


class TestRoleGuards:
    """Все операторские входы закрыты `ensure_role`. Отказ → ранний
    выход без обращения к БД и без перерисовки экрана."""

    @pytest.mark.asyncio
    async def test_list_denied_returns_without_db(self) -> None:
        event = _make_event()
        get_active = AsyncMock()
        with patch(f"{_LIST}.ensure_role", AsyncMock(return_value=False)), \
             patch(f"{_LIST}.session_scope", _fake_session_scope), \
             patch(f"{_LIST}.templates_service.list_active", get_active), \
             patch(f"{_LIST}.send_or_edit_screen", AsyncMock()) as send:
            await bt._list(event)
        get_active.assert_not_awaited()
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_open_denied_returns_without_db(self) -> None:
        event = _make_event()
        get_by_id = AsyncMock()
        with patch(f"{_LIST}.ensure_role", AsyncMock(return_value=False)), \
             patch(f"{_LIST}.templates_service.get_by_id", get_by_id), \
             patch(f"{_LIST}.send_or_edit_screen", AsyncMock()) as send:
            await bt._open(event, 5)
        get_by_id.assert_not_awaited()
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_apply_denied_returns_without_dedupe_mark(self) -> None:
        """Отказ ДО `_mark_apply` — dedupe-словарь не засоряется
        попыткой неавторизованного оператора."""
        event = _make_event()
        with patch(f"{_LIST}.ensure_role", AsyncMock(return_value=False)), \
             patch(f"{_LIST}.send_or_edit_screen", AsyncMock()):
            await bt._apply(event, 5)
        assert bt._apply_dedupe == {}

    @pytest.mark.asyncio
    async def test_start_new_denied_does_not_open_wizard(self) -> None:
        event = _make_event(user_id=7)
        with patch(f"{_WIZ}.ensure_role", AsyncMock(return_value=False)), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()):
            await bt._start_new(event)
        assert 7 not in bt._wizards

    @pytest.mark.asyncio
    async def test_do_delete_denied_no_archive_no_audit(self) -> None:
        event = _make_event()
        archive = AsyncMock()
        write_audit = AsyncMock()
        with patch(f"{_CRUD}.ensure_role", AsyncMock(return_value=False)), \
             patch(f"{_CRUD}.session_scope", _fake_session_scope), \
             patch(f"{_CRUD}.templates_service.archive", archive), \
             patch(f"{_CRUD}.operators_service.write_audit", write_audit), \
             patch(f"{_CRUD}.send_or_edit_screen", AsyncMock()):
            await bt._do_delete(event, 5)
        archive.assert_not_awaited()
        write_audit.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════
# 2. _list — пустой список vs непустой.
# ══════════════════════════════════════════════════════════════════════


class TestList:
    @pytest.mark.asyncio
    async def test_empty_shows_empty_text(self) -> None:
        from aemr_bot import texts

        event = _make_event()
        with patch(f"{_LIST}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_LIST}.session_scope", _fake_session_scope), \
             patch(f"{_LIST}.templates_service.list_active",
                   AsyncMock(return_value=[])), \
             patch(f"{_LIST}.send_or_edit_screen", AsyncMock()) as send:
            await bt._list(event)
        send.assert_awaited_once()
        assert send.await_args.kwargs["text"] == texts.OP_TMPL_LIST_EMPTY
        # chat_id фиксируется на admin_group_id.
        from aemr_bot.config import settings as cfg
        assert send.await_args.kwargs["chat_id"] == cfg.admin_group_id

    @pytest.mark.asyncio
    async def test_non_empty_shows_header_with_count(self) -> None:
        event = _make_event()
        items = [_tmpl(tid=1, name="A"), _tmpl(tid=2, name="B")]
        with patch(f"{_LIST}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_LIST}.session_scope", _fake_session_scope), \
             patch(f"{_LIST}.templates_service.list_active",
                   AsyncMock(return_value=items)), \
             patch(f"{_LIST}.send_or_edit_screen", AsyncMock()) as send:
            await bt._list(event)
        # Заголовок с числом активных шаблонов (2).
        assert "2" in send.await_args.kwargs["text"]


# ══════════════════════════════════════════════════════════════════════
# 3. _open — карточка: not-found, never-used, last-used, «странная»
#    ветка use_count>0 но last_used_at=None (пустая строка).
# ══════════════════════════════════════════════════════════════════════


class TestOpen:
    @pytest.mark.asyncio
    async def test_not_found_shows_not_found_text(self) -> None:
        from aemr_bot import texts

        event = _make_event()
        with patch(f"{_LIST}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_LIST}.session_scope", _fake_session_scope), \
             patch(f"{_LIST}.templates_service.get_by_id",
                   AsyncMock(return_value=None)), \
             patch(f"{_LIST}.send_or_edit_screen", AsyncMock()) as send:
            await bt._open(event, 999)
        assert send.await_args.kwargs["text"] == texts.OP_TMPL_NOT_FOUND

    @pytest.mark.asyncio
    async def test_never_used_renders_never_used_line(self) -> None:
        """use_count=0 → блок «ещё ни разу не применялся»."""
        from aemr_bot import texts

        event = _make_event()
        tmpl = _tmpl(use_count=0, last_used_at=None)
        with patch(f"{_LIST}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_LIST}.session_scope", _fake_session_scope), \
             patch(f"{_LIST}.templates_service.get_by_id",
                   AsyncMock(return_value=tmpl)), \
             patch(f"{_LIST}._image_attachments.build_outbound_image_attachments",
                   return_value=[]), \
             patch(f"{_LIST}.send_or_edit_screen", AsyncMock()) as send:
            await bt._open(event, 1)
        text = send.await_args.kwargs["text"]
        # NEVER_USED-строка попала в тело карточки.
        assert texts.OP_TMPL_CARD_NEVER_USED in text

    @pytest.mark.asyncio
    async def test_used_with_timestamp_renders_last_used_line(self) -> None:
        """use_count>0 и last_used_at задан → строка «последнее
        применение: <дата>» с локальным форматированием."""
        import datetime as dt

        event = _make_event()
        when = dt.datetime(2026, 5, 1, 3, 0, tzinfo=dt.timezone.utc)
        tmpl = _tmpl(use_count=4, last_used_at=when)
        with patch(f"{_LIST}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_LIST}.session_scope", _fake_session_scope), \
             patch(f"{_LIST}.templates_service.get_by_id",
                   AsyncMock(return_value=tmpl)), \
             patch(f"{_LIST}._image_attachments.build_outbound_image_attachments",
                   return_value=[]), \
             patch(f"{_LIST}.send_or_edit_screen", AsyncMock()) as send:
            await bt._open(event, 1)
        text = send.await_args.kwargs["text"]
        # use_count=4 проброшен; дата отформатирована (год присутствует).
        assert "4" in text
        assert "2026" in text

    @pytest.mark.asyncio
    async def test_used_without_timestamp_renders_blank_last_used(self) -> None:
        """Характеризуем «странность»: use_count>0 НО last_used_at=None →
        last_used_line = "" (пустая). Карточка всё равно рендерится без
        падения; блок never-used НЕ показывается."""
        from aemr_bot import texts

        event = _make_event()
        tmpl = _tmpl(use_count=3, last_used_at=None)
        with patch(f"{_LIST}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_LIST}.session_scope", _fake_session_scope), \
             patch(f"{_LIST}.templates_service.get_by_id",
                   AsyncMock(return_value=tmpl)), \
             patch(f"{_LIST}._image_attachments.build_outbound_image_attachments",
                   return_value=[]), \
             patch(f"{_LIST}.send_or_edit_screen", AsyncMock()) as send:
            await bt._open(event, 1)
        text = send.await_args.kwargs["text"]
        # Никакого never-used-блока (use_count != 0).
        assert texts.OP_TMPL_CARD_NEVER_USED not in text


# ══════════════════════════════════════════════════════════════════════
# 4. _apply — pre-charge broadcast wizard: dedupe, not-found,
#    zero-subscribers, happy-path (record_usage + prefill + citation).
# ══════════════════════════════════════════════════════════════════════


class TestApply:
    @pytest.mark.asyncio
    async def test_double_tap_silent_ack_no_side_effects(self) -> None:
        """Повтор в 3-сек окне → тихий ack, без record_usage, без
        prefill, без перерисовки preview."""
        event = _make_event(user_id=7)
        bt._mark_apply(7, 5)  # предыдущий тап «только что»
        record_usage = AsyncMock()
        prefill = MagicMock()
        with patch(f"{_LIST}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_LIST}.get_user_id", return_value=7), \
             patch(f"{_LIST}.session_scope", _fake_session_scope), \
             patch(f"{_LIST}.templates_service.record_usage", record_usage), \
             patch(f"{_LIST}.broadcast_handler.prefill_wizard_from_template",
                   prefill), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()) as ack, \
             patch(f"{_LIST}.send_or_edit_screen", AsyncMock()) as send:
            await bt._apply(event, 5)
        record_usage.assert_not_awaited()
        prefill.assert_not_called()
        send.assert_not_awaited()
        ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_not_found_shows_not_found_and_marks_dedupe(self) -> None:
        """Шаблон удалён между списком и apply → NOT_FOUND текст. dedupe
        ВСЁ РАВНО помечен (mark идёт до session_scope)."""
        from aemr_bot import texts

        event = _make_event(user_id=7)
        with patch(f"{_LIST}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_LIST}.get_user_id", return_value=7), \
             patch(f"{_LIST}.session_scope", _fake_session_scope), \
             patch(f"{_LIST}.templates_service.get_by_id",
                   AsyncMock(return_value=None)), \
             patch(f"{_LIST}.send_or_edit_screen", AsyncMock()) as send:
            await bt._apply(event, 5)
        assert send.await_args.kwargs["text"] == texts.OP_TMPL_NOT_FOUND
        # mark_apply отработал до not-found return.
        assert (7, 5) in bt._apply_dedupe

    @pytest.mark.asyncio
    async def test_zero_subscribers_shows_no_subscribers(self) -> None:
        """count_subscribers==0 → экран «нет подписчиков». record_usage
        ВСЁ РАВНО инкрементируется (характеризуем как есть: usage
        пишется до проверки subscribers==0)."""
        from aemr_bot import texts

        event = _make_event(user_id=7)
        record_usage = AsyncMock()
        prefill = MagicMock()
        with patch(f"{_LIST}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_LIST}.get_user_id", return_value=7), \
             patch(f"{_LIST}.session_scope", _fake_session_scope), \
             patch(f"{_LIST}.templates_service.get_by_id",
                   AsyncMock(return_value=_tmpl())), \
             patch(f"{_LIST}.broadcasts_service.count_subscribers",
                   AsyncMock(return_value=0)), \
             patch(f"{_LIST}.templates_service.record_usage", record_usage), \
             patch(f"{_LIST}.broadcast_handler.prefill_wizard_from_template",
                   prefill), \
             patch(f"{_LIST}.send_or_edit_screen", AsyncMock()) as send:
            await bt._apply(event, 1)
        assert send.await_args.kwargs["text"] == texts.OP_BROADCAST_NO_SUBSCRIBERS
        record_usage.assert_awaited_once()
        # До prefill не дошли — рассылку не заряжаем при нуле подписчиков.
        prefill.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path_prefills_and_shows_citation_footer(self) -> None:
        """Успех: record_usage + prefill broadcast wizard + preview с
        citation-footer «Источник: шаблон «N»»."""
        event = _make_event(user_id=7)
        tmpl = _tmpl(tid=3, name="Паводок", text="Берегитесь",
                     attachments=[{"type": "image", "payload": {"token": "t"}}])
        record_usage = AsyncMock()
        prefill = MagicMock()
        with patch(f"{_LIST}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_LIST}.get_user_id", return_value=7), \
             patch(f"{_LIST}.session_scope", _fake_session_scope), \
             patch(f"{_LIST}.templates_service.get_by_id",
                   AsyncMock(return_value=tmpl)), \
             patch(f"{_LIST}.broadcasts_service.count_subscribers",
                   AsyncMock(return_value=42)), \
             patch(f"{_LIST}.templates_service.record_usage", record_usage), \
             patch(f"{_LIST}.broadcast_handler.prefill_wizard_from_template",
                   prefill), \
             patch(f"{_LIST}._image_attachments.build_outbound_image_attachments",
                   return_value=[]), \
             patch(f"{_LIST}.send_or_edit_screen", AsyncMock()) as send:
            await bt._apply(event, 3)
        record_usage.assert_awaited_once()
        # prefill получил text и копию attachments шаблона.
        prefill.assert_called_once()
        assert prefill.call_args.kwargs["text"] == "Берегитесь"
        assert prefill.call_args.kwargs["attachments"] == list(tmpl.attachments)
        # preview содержит citation-footer с именем шаблона.
        body = send.await_args.kwargs["text"]
        assert "Паводок" in body
        assert "Источник" in body


# ══════════════════════════════════════════════════════════════════════
# 5. _do_delete — soft-delete (archive) + запись в audit_log.
# ══════════════════════════════════════════════════════════════════════


class TestDoDelete:
    @pytest.mark.asyncio
    async def test_not_found_no_archive_no_audit(self) -> None:
        from aemr_bot import texts

        event = _make_event(user_id=7)
        archive = AsyncMock()
        write_audit = AsyncMock()
        with patch(f"{_CRUD}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_CRUD}.get_user_id", return_value=7), \
             patch(f"{_CRUD}.get_operator",
                   AsyncMock(return_value=SimpleNamespace(id=99))), \
             patch(f"{_CRUD}.session_scope", _fake_session_scope), \
             patch(f"{_CRUD}.templates_service.get_by_id",
                   AsyncMock(return_value=None)), \
             patch(f"{_CRUD}.templates_service.archive", archive), \
             patch(f"{_CRUD}.operators_service.write_audit", write_audit), \
             patch(f"{_CRUD}.send_or_edit_screen", AsyncMock()) as send:
            await bt._do_delete(event, 999)
        assert send.await_args.kwargs["text"] == texts.OP_TMPL_NOT_FOUND
        archive.assert_not_awaited()
        write_audit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_happy_path_archives_and_audits(self) -> None:
        """archive() вызывается, audit_log пишет action
        broadcast_template_delete с именем в details, экран «удалён»."""
        event = _make_event(user_id=7)
        archive = AsyncMock()
        write_audit = AsyncMock()
        with patch(f"{_CRUD}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_CRUD}.get_user_id", return_value=7), \
             patch(f"{_CRUD}.get_operator",
                   AsyncMock(return_value=SimpleNamespace(id=99))), \
             patch(f"{_CRUD}.session_scope", _fake_session_scope), \
             patch(f"{_CRUD}.templates_service.get_by_id",
                   AsyncMock(return_value=_tmpl(tid=5, name="Старый"))), \
             patch(f"{_CRUD}.templates_service.archive", archive), \
             patch(f"{_CRUD}.operators_service.write_audit", write_audit), \
             patch(f"{_CRUD}.send_or_edit_screen", AsyncMock()) as send:
            await bt._do_delete(event, 5)
        archive.assert_awaited_once()
        write_audit.assert_awaited_once()
        akw = write_audit.await_args.kwargs
        assert akw["action"] == "broadcast_template_delete"
        assert akw["operator_max_user_id"] == 7
        assert akw["target"] == "template #5"
        assert akw["details"] == {"name": "Старый"}
        # Финальный экран — «удалён <имя>».
        assert "Старый" in send.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_archive_without_operator_skips_audit(self) -> None:
        """get_operator вернул None → archive выполняется, но audit НЕ
        пишется (гард `op is not None`). Характеризуем как есть."""
        event = _make_event(user_id=7)
        archive = AsyncMock()
        write_audit = AsyncMock()
        with patch(f"{_CRUD}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_CRUD}.get_user_id", return_value=7), \
             patch(f"{_CRUD}.get_operator", AsyncMock(return_value=None)), \
             patch(f"{_CRUD}.session_scope", _fake_session_scope), \
             patch(f"{_CRUD}.templates_service.get_by_id",
                   AsyncMock(return_value=_tmpl(tid=5, name="Старый"))), \
             patch(f"{_CRUD}.templates_service.archive", archive), \
             patch(f"{_CRUD}.operators_service.write_audit", write_audit), \
             patch(f"{_CRUD}.send_or_edit_screen", AsyncMock()):
            await bt._do_delete(event, 5)
        archive.assert_awaited_once()
        write_audit.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════
# 6. _save_new — финальное сохранение: TemplateNameAlreadyExists (назад в
#    шаг имени), ValueError (drop wizard), wrong-state guard.
# ══════════════════════════════════════════════════════════════════════


class TestSaveNew:
    @pytest.fixture(autouse=True)
    def _allow_role(self):
        """SECURITY_REVIEW P3-3: `_save_new` теперь начинается с
        `ensure_role(IT, COORDINATOR)` — write-callback пере-проверяет роль
        (между стартом wizard'а и «Сохранить» оператора могли понизить).
        Эти характеризации проверяют поведение ПОСЛЕ ролевого гейта (сам
        отказ роли покрыт в TestRoleGuards), поэтому роль мокаем как
        разрешённую."""
        with patch(f"{_WIZ}.ensure_role", AsyncMock(return_value=True)):
            yield

    @pytest.mark.asyncio
    async def test_wrong_state_shows_cancelled(self) -> None:
        """state не в new_preview (устаревшая кнопка) → CANCELLED, create
        НЕ зовётся."""
        from aemr_bot import texts

        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(step="new_awaiting_name")
        create = AsyncMock()
        with patch(f"{_WIZ}.get_operator",
                   AsyncMock(return_value=SimpleNamespace(id=99))), \
             patch(f"{_WIZ}.templates_service.create_template", create), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()) as send:
            await bt._save_new(event)
        assert send.await_args.kwargs["text"] == texts.OP_TMPL_CANCELLED
        create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_name_taken_returns_to_name_step(self) -> None:
        """create бросает TemplateNameAlreadyExists (имя «заняли»
        параллельно) → wizard возвращается в new_awaiting_name, экран
        NAME_TAKEN, wizard НЕ дропается."""
        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(
            step="new_preview", pending_name="Дубль",
            pending_text="t", pending_attachments=[],
        )
        with patch(f"{_WIZ}.get_operator",
                   AsyncMock(return_value=SimpleNamespace(id=99))), \
             patch(f"{_WIZ}.session_scope", _fake_session_scope), \
             patch(f"{_WIZ}.templates_service.create_template",
                   AsyncMock(
                       side_effect=bt_wiz.templates_service.TemplateNameAlreadyExists(
                           "Дубль"))), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()) as send:
            await bt._save_new(event)
        # Wizard жив и откатан на шаг ввода имени.
        assert 7 in bt._wizards
        assert bt._wizards[7].step == "new_awaiting_name"
        assert "Дубль" in send.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_value_error_drops_wizard(self) -> None:
        """create бросает ValueError (например, пустой текст на уровне
        сервиса) → wizard ДРОПается, экран с текстом ошибки."""
        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(
            step="new_preview", pending_name="X",
            pending_text="t", pending_attachments=[],
        )
        with patch(f"{_WIZ}.get_operator",
                   AsyncMock(return_value=SimpleNamespace(id=99))), \
             patch(f"{_WIZ}.session_scope", _fake_session_scope), \
             patch(f"{_WIZ}.templates_service.create_template",
                   AsyncMock(side_effect=ValueError("текст пуст"))), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()) as send:
            await bt._save_new(event)
        assert 7 not in bt._wizards
        assert "Ошибка создания" in send.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_happy_path_creates_audits_and_clears_wizard(self) -> None:
        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(
            step="new_preview", pending_name="Новый",
            pending_text="Текст рассылки", pending_attachments=[],
        )
        write_audit = AsyncMock()
        with patch(f"{_WIZ}.get_operator",
                   AsyncMock(return_value=SimpleNamespace(id=99))), \
             patch(f"{_WIZ}.session_scope", _fake_session_scope), \
             patch(f"{_WIZ}.templates_service.create_template",
                   AsyncMock(return_value=_tmpl(tid=11, name="Новый"))), \
             patch(f"{_WIZ}.operators_service.write_audit", write_audit), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()) as send:
            await bt._save_new(event)
        assert 7 not in bt._wizards
        write_audit.assert_awaited_once()
        akw = write_audit.await_args.kwargs
        assert akw["action"] == "broadcast_template_create"
        assert akw["target"] == "template #11"
        assert akw["details"]["name"] == "Новый"
        assert akw["details"]["chars"] == len("Текст рассылки")
        # Экран «создан» с номером и именем.
        assert "Новый" in send.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_happy_path_no_operator_skips_audit(self) -> None:
        """op_id is None → шаблон создаётся, но audit пропускается."""
        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(
            step="new_preview", pending_name="Новый",
            pending_text="Текст", pending_attachments=[],
        )
        write_audit = AsyncMock()
        with patch(f"{_WIZ}.get_operator", AsyncMock(return_value=None)), \
             patch(f"{_WIZ}.session_scope", _fake_session_scope), \
             patch(f"{_WIZ}.templates_service.create_template",
                   AsyncMock(return_value=_tmpl(tid=12, name="Новый"))), \
             patch(f"{_WIZ}.operators_service.write_audit", write_audit), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()):
            await bt._save_new(event)
        write_audit.assert_not_awaited()
        assert 7 not in bt._wizards


# ══════════════════════════════════════════════════════════════════════
# 7. _step_rename — валидация (too-long), not-found mid-rename,
#    name-taken, happy-path + audit.
# ══════════════════════════════════════════════════════════════════════


class TestStepRename:
    def _state(self, target_id: int = 5):
        return bt._TmplWizardState(
            step="rename_awaiting_name", target_id=target_id
        )

    @pytest.mark.asyncio
    async def test_too_long_name_rejected_keeps_step(self) -> None:
        event = _make_event(user_id=7)
        state = self._state()
        long_name = "x" * (bt_crud.templates_service.MAX_NAME_LEN + 1)
        consumed = await bt._step_rename(
            event, 7, state, long_name, op_id=99
        )
        assert consumed is True
        # Сообщение об ошибке отправлено через message.answer.
        event.message.answer.assert_awaited()
        # Шаг не изменился — оператор может перевбить.
        assert state.step == "rename_awaiting_name"

    @pytest.mark.asyncio
    async def test_empty_name_rejected(self) -> None:
        event = _make_event(user_id=7)
        state = self._state()
        consumed = await bt._step_rename(event, 7, state, "", op_id=99)
        assert consumed is True
        event.message.answer.assert_awaited()

    @pytest.mark.asyncio
    async def test_target_gone_mid_rename_shows_not_found(self) -> None:
        """Между открытием rename-wizard и вводом имени шаблон удалён →
        NOT_FOUND, wizard дропается."""
        from aemr_bot import texts

        event = _make_event(user_id=7)
        bt._wizards[7] = self._state()
        state = bt._wizards[7]
        with patch(f"{_CRUD}.session_scope", _fake_session_scope), \
             patch(f"{_CRUD}.templates_service.get_by_id",
                   AsyncMock(return_value=None)):
            consumed = await bt._step_rename(
                event, 7, state, "Новое имя", op_id=99
            )
        assert consumed is True
        assert 7 not in bt._wizards
        assert event.message.answer.await_args.args[0] == texts.OP_TMPL_NOT_FOUND

    @pytest.mark.asyncio
    async def test_name_taken_keeps_wizard(self) -> None:
        """rename бросает TemplateNameAlreadyExists → NAME_TAKEN, wizard
        жив (оператор перевбивает имя)."""
        event = _make_event(user_id=7)
        bt._wizards[7] = self._state()
        state = bt._wizards[7]
        with patch(f"{_CRUD}.session_scope", _fake_session_scope), \
             patch(f"{_CRUD}.templates_service.get_by_id",
                   AsyncMock(return_value=_tmpl(tid=5, name="Старое"))), \
             patch(f"{_CRUD}.templates_service.rename",
                   AsyncMock(
                       side_effect=bt_crud.templates_service.TemplateNameAlreadyExists(
                           "Занятое"))):
            consumed = await bt._step_rename(
                event, 7, state, "Занятое", op_id=99
            )
        assert consumed is True
        # Wizard НЕ дропнут (в отличие от not-found).
        assert 7 in bt._wizards

    @pytest.mark.asyncio
    async def test_happy_path_renames_audits_and_drops_wizard(self) -> None:
        event = _make_event(user_id=7)
        bt._wizards[7] = self._state(target_id=5)
        state = bt._wizards[7]
        write_audit = AsyncMock()
        with patch(f"{_CRUD}.session_scope", _fake_session_scope), \
             patch(f"{_CRUD}.templates_service.get_by_id",
                   AsyncMock(return_value=_tmpl(tid=5, name="Старое"))), \
             patch(f"{_CRUD}.templates_service.rename",
                   AsyncMock(return_value=_tmpl(tid=5, name="Новое"))), \
             patch(f"{_CRUD}.operators_service.write_audit", write_audit):
            consumed = await bt._step_rename(
                event, 7, state, "Новое", op_id=99
            )
        assert consumed is True
        assert 7 not in bt._wizards
        write_audit.assert_awaited_once()
        akw = write_audit.await_args.kwargs
        assert akw["action"] == "broadcast_template_rename"
        assert akw["target"] == "template #5"
        assert akw["details"] == {"old_name": "Старое", "new_name": "Новое"}


# ══════════════════════════════════════════════════════════════════════
# 8. _step_edit — image-only-замена (пустой текст + картинки),
#    too-long, happy-path text-only (переход в preview).
# ══════════════════════════════════════════════════════════════════════


class TestStepEdit:
    def _state(self, target_id: int = 5):
        return bt._TmplWizardState(
            step="edit_awaiting_text", target_id=target_id
        )

    @pytest.mark.asyncio
    async def test_empty_text_with_images_starts_image_only_replace(self) -> None:
        """audit 2026-05-28: пустой текст + НОВЫЕ картинки → image-only
        замена. state переходит в edit_preview, pending_text = СТАРЫЙ
        текст шаблона, _edit_image_replaced=True."""
        event = _make_event(user_id=7)
        state = self._state(target_id=5)
        before = _tmpl(tid=5, name="Док", text="прежний текст")
        new_atts = [{"type": "image", "payload": {"token": "x"}}]
        with patch(f"{_WIZ}.session_scope", _fake_session_scope), \
             patch(f"{_WIZ}.broadcast_handler._resolve_broadcast_max_images",
                   AsyncMock(return_value=5)), \
             patch(f"{_WIZ}.templates_service.get_by_id",
                   AsyncMock(return_value=before)), \
             patch(f"{_WIZ}._image_attachments.image_attachments_from_event",
                   return_value=new_atts), \
             patch(f"{_WIZ}._image_attachments.build_outbound_image_attachments",
                   return_value=[]):
            consumed = await bt._step_edit(event, 7, state, "", op_id=99)
        assert consumed is True
        assert state.step == "edit_preview"
        # Текст оставлен прежним (замена только картинок).
        assert state.pending_text == "прежний текст"
        assert state.pending_attachments == new_atts
        assert getattr(state, "_edit_image_replaced", None) is True

    @pytest.mark.asyncio
    async def test_empty_text_no_images_reprompts(self) -> None:
        """Пустой текст и НЕТ картинок → просто повторяем prompt, state
        остаётся в edit_awaiting_text."""
        event = _make_event(user_id=7)
        state = self._state(target_id=5)
        before = _tmpl(tid=5, name="Док", text="прежний")
        with patch(f"{_WIZ}.session_scope", _fake_session_scope), \
             patch(f"{_WIZ}.broadcast_handler._resolve_broadcast_max_images",
                   AsyncMock(return_value=5)), \
             patch(f"{_WIZ}.templates_service.get_by_id",
                   AsyncMock(return_value=before)), \
             patch(f"{_WIZ}._image_attachments.image_attachments_from_event",
                   return_value=[]):
            consumed = await bt._step_edit(event, 7, state, "", op_id=99)
        assert consumed is True
        assert state.step == "edit_awaiting_text"
        event.message.answer.assert_awaited()

    @pytest.mark.asyncio
    async def test_too_long_text_rejected(self) -> None:
        from aemr_bot.config import settings as cfg

        event = _make_event(user_id=7)
        state = self._state(target_id=5)
        consumed = await bt._step_edit(
            event, 7, state, "y" * (cfg.broadcast_max_chars + 1), op_id=99
        )
        assert consumed is True
        event.message.answer.assert_awaited()
        # Шаг не изменился.
        assert state.step == "edit_awaiting_text"

    @pytest.mark.asyncio
    async def test_text_only_moves_to_preview_no_image_replace(self) -> None:
        """Текст без картинок → preview; старые картинки шаблона остаются,
        _edit_image_replaced=False (effective_atts = старые)."""
        event = _make_event(user_id=7)
        state = self._state(target_id=5)
        before = _tmpl(tid=5, name="Док", text="старый",
                       attachments=[{"old": True}])
        with patch(f"{_WIZ}.session_scope", _fake_session_scope), \
             patch(f"{_WIZ}.broadcast_handler._resolve_broadcast_max_images",
                   AsyncMock(return_value=5)), \
             patch(f"{_WIZ}.templates_service.get_by_id",
                   AsyncMock(return_value=before)), \
             patch(f"{_WIZ}._image_attachments.image_attachments_from_event",
                   return_value=[]), \
             patch(f"{_WIZ}._image_attachments.build_outbound_image_attachments",
                   return_value=[]):
            consumed = await bt._step_edit(
                event, 7, state, "новый текст", op_id=99
            )
        assert consumed is True
        assert state.step == "edit_preview"
        assert state.pending_text == "новый текст"
        # Не приложили картинки → старые остаются как effective.
        assert state.pending_attachments == [{"old": True}]
        assert getattr(state, "_edit_image_replaced", None) is False

    @pytest.mark.asyncio
    async def test_target_gone_mid_edit_shows_not_found(self) -> None:
        from aemr_bot import texts

        event = _make_event(user_id=7)
        bt._wizards[7] = self._state(target_id=5)
        state = bt._wizards[7]
        with patch(f"{_WIZ}.session_scope", _fake_session_scope), \
             patch(f"{_WIZ}.broadcast_handler._resolve_broadcast_max_images",
                   AsyncMock(return_value=5)), \
             patch(f"{_WIZ}.templates_service.get_by_id",
                   AsyncMock(return_value=None)):
            consumed = await bt._step_edit(
                event, 7, state, "новый текст", op_id=99
            )
        assert consumed is True
        assert 7 not in bt._wizards
        assert event.message.answer.await_args.args[0] == texts.OP_TMPL_NOT_FOUND

    @pytest.mark.asyncio
    async def test_target_id_none_with_text_returns_false_drops_wizard(
        self,
    ) -> None:
        """Граничный кейс: непустой валидный текст, но `state.target_id`
        is None (битый/устаревший state). `_step_edit` дропает wizard и
        возвращает False — caller (handle_wizard_text) делает
        fallthrough, сообщение НЕ поглощается. Закрепляем текущий
        контракт: ни session_scope, ни message.answer не дёргаются —
        ранний guard ДО обращения к БД."""
        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(
            step="edit_awaiting_text", target_id=None
        )
        state = bt._wizards[7]
        get_by_id = AsyncMock()
        with patch(f"{_WIZ}.session_scope", _fake_session_scope), \
             patch(f"{_WIZ}.broadcast_handler._resolve_broadcast_max_images",
                   AsyncMock(return_value=5)), \
             patch(f"{_WIZ}.templates_service.get_by_id", get_by_id):
            consumed = await bt._step_edit(
                event, 7, state, "какой-то текст", op_id=99
            )
        assert consumed is False
        # Wizard снят, к БД не обращались, оператору ничего не ответили.
        assert 7 not in bt._wizards
        get_by_id.assert_not_awaited()
        event.message.answer.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════
# 9. _save_edit — wrong-state guard, TemplateNotFound, happy-path
#    (text-only vs image-replaced audit details).
# ══════════════════════════════════════════════════════════════════════


class TestSaveEdit:
    @pytest.fixture(autouse=True)
    def _allow_role(self):
        """SECURITY_REVIEW P3-3: `_save_edit` теперь начинается с
        `ensure_role(IT, COORDINATOR)` (как `_save_new`). Характеризации
        проверяют поведение ПОСЛЕ ролевого гейта; отказ роли — в
        TestRoleGuards. Роль мокаем как разрешённую."""
        with patch(f"{_WIZ}.ensure_role", AsyncMock(return_value=True)):
            yield

    def _preview_state(self, *, replaced: bool, target_id: int = 5):
        st = bt._TmplWizardState(
            step="edit_preview", target_id=target_id,
            pending_text="новый текст", pending_name="Док",
            pending_attachments=([{"img": 1}] if replaced else []),
        )
        st._edit_image_replaced = replaced  # type: ignore[attr-defined]
        return st

    @pytest.mark.asyncio
    async def test_wrong_template_id_shows_cancelled(self) -> None:
        """save_edit для id, не совпадающего с target_id wizard'а
        (устаревшая кнопка) → CANCELLED, update_text не зовётся."""
        from aemr_bot import texts

        event = _make_event(user_id=7)
        bt._wizards[7] = self._preview_state(replaced=False, target_id=5)
        update = AsyncMock()
        with patch(f"{_WIZ}.get_operator",
                   AsyncMock(return_value=SimpleNamespace(id=99))), \
             patch(f"{_WIZ}.templates_service.update_text", update), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()) as send:
            await bt._save_edit(event, 999)  # mismatch
        assert send.await_args.kwargs["text"] == texts.OP_TMPL_CANCELLED
        update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_template_not_found_drops_wizard(self) -> None:
        from aemr_bot import texts

        event = _make_event(user_id=7)
        bt._wizards[7] = self._preview_state(replaced=False, target_id=5)
        with patch(f"{_WIZ}.get_operator",
                   AsyncMock(return_value=SimpleNamespace(id=99))), \
             patch(f"{_WIZ}.session_scope", _fake_session_scope), \
             patch(f"{_WIZ}.templates_service.update_text",
                   AsyncMock(
                       side_effect=bt_wiz.templates_service.TemplateNotFound(5))), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()) as send:
            await bt._save_edit(event, 5)
        assert 7 not in bt._wizards
        assert send.await_args.kwargs["text"] == texts.OP_TMPL_NOT_FOUND

    @pytest.mark.asyncio
    async def test_happy_text_only_audits_replaced_false(self) -> None:
        """text-only edit: update_text c attachments=None, audit
        image_replaced=False, экран EDITED_TEXT_ONLY."""
        event = _make_event(user_id=7)
        bt._wizards[7] = self._preview_state(replaced=False, target_id=5)
        update = AsyncMock()
        write_audit = AsyncMock()
        with patch(f"{_WIZ}.get_operator",
                   AsyncMock(return_value=SimpleNamespace(id=99))), \
             patch(f"{_WIZ}.session_scope", _fake_session_scope), \
             patch(f"{_WIZ}.templates_service.update_text", update), \
             patch(f"{_WIZ}.operators_service.write_audit", write_audit), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()) as send:
            await bt._save_edit(event, 5)
        assert 7 not in bt._wizards
        # attachments=None — картинки не трогаем.
        assert update.await_args.kwargs["attachments"] is None
        akw = write_audit.await_args.kwargs
        assert akw["action"] == "broadcast_template_update"
        assert akw["details"]["image_replaced"] is False
        assert akw["details"]["chars"] == len("новый текст")
        # Текст экрана — text-only вариант (имя «Док» внутри).
        assert "Док" in send.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_happy_image_replaced_passes_attachments(self) -> None:
        """image-replaced edit: update_text получает pending_attachments
        (не None), audit image_replaced=True."""
        event = _make_event(user_id=7)
        bt._wizards[7] = self._preview_state(replaced=True, target_id=5)
        update = AsyncMock()
        write_audit = AsyncMock()
        with patch(f"{_WIZ}.get_operator",
                   AsyncMock(return_value=SimpleNamespace(id=99))), \
             patch(f"{_WIZ}.session_scope", _fake_session_scope), \
             patch(f"{_WIZ}.templates_service.update_text", update), \
             patch(f"{_WIZ}.operators_service.write_audit", write_audit), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()):
            await bt._save_edit(event, 5)
        # attachments проброшены — картинки заменяются.
        assert update.await_args.kwargs["attachments"] == [{"img": 1}]
        assert write_audit.await_args.kwargs["details"]["image_replaced"] is True


# ══════════════════════════════════════════════════════════════════════
# 10. _step_clone_name — name-taken, happy-path (create + clone audit).
# ══════════════════════════════════════════════════════════════════════


class TestStepCloneName:
    def _state(self):
        return bt._TmplWizardState(
            step="clone_awaiting_name",
            pending_text="скопированный текст",
            pending_attachments=[{"img": 1}],
            target_id=3,
            source_name="Источник",
        )

    @pytest.mark.asyncio
    async def test_name_taken_keeps_wizard(self) -> None:
        event = _make_event(user_id=7)
        bt._wizards[7] = self._state()
        state = bt._wizards[7]
        with patch(f"{_CRUD}.session_scope", _fake_session_scope), \
             patch(f"{_CRUD}.templates_service.create_template",
                   AsyncMock(
                       side_effect=bt_crud.templates_service.TemplateNameAlreadyExists(
                           "Занято"))):
            consumed = await bt._step_clone_name(
                event, 7, state, "Занято", op_id=99
            )
        assert consumed is True
        assert 7 in bt._wizards  # wizard жив

    @pytest.mark.asyncio
    async def test_happy_path_clones_audits_and_drops_wizard(self) -> None:
        """create_template с pending_text/attachments источника, audit
        action broadcast_template_clone с source_id/source_name."""
        event = _make_event(user_id=7)
        bt._wizards[7] = self._state()
        state = bt._wizards[7]
        create = AsyncMock(return_value=_tmpl(tid=20, name="Копия"))
        write_audit = AsyncMock()
        with patch(f"{_CRUD}.session_scope", _fake_session_scope), \
             patch(f"{_CRUD}.templates_service.create_template", create), \
             patch(f"{_CRUD}.operators_service.write_audit", write_audit):
            consumed = await bt._step_clone_name(
                event, 7, state, "Копия", op_id=99
            )
        assert consumed is True
        assert 7 not in bt._wizards
        # create получил текст и картинки источника.
        assert create.await_args.kwargs["text"] == "скопированный текст"
        assert create.await_args.kwargs["attachments"] == [{"img": 1}]
        akw = write_audit.await_args.kwargs
        assert akw["action"] == "broadcast_template_clone"
        assert akw["details"]["source_id"] == 3
        assert akw["details"]["source_name"] == "Источник"
        assert akw["details"]["new_name"] == "Копия"

    @pytest.mark.asyncio
    async def test_no_operator_skips_audit(self) -> None:
        event = _make_event(user_id=7)
        bt._wizards[7] = self._state()
        state = bt._wizards[7]
        write_audit = AsyncMock()
        with patch(f"{_CRUD}.session_scope", _fake_session_scope), \
             patch(f"{_CRUD}.templates_service.create_template",
                   AsyncMock(return_value=_tmpl(tid=21, name="Копия"))), \
             patch(f"{_CRUD}.operators_service.write_audit", write_audit):
            await bt._step_clone_name(event, 7, state, "Копия", op_id=None)
        write_audit.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════
# 11. _step_search — nothing-found vs results, empty-query reprompt.
# ══════════════════════════════════════════════════════════════════════


class TestStepSearch:
    def _state(self):
        return bt._TmplWizardState(step="search_awaiting_query")

    @pytest.mark.asyncio
    async def test_empty_query_reprompts_keeps_wizard(self) -> None:
        event = _make_event(user_id=7)
        bt._wizards[7] = self._state()
        state = bt._wizards[7]
        consumed = await bt._step_search(event, 7, state, "")
        assert consumed is True
        event.message.answer.assert_awaited()
        # Пустой запрос — wizard остаётся (не дропается).
        assert 7 in bt._wizards

    @pytest.mark.asyncio
    async def test_nothing_found_shows_empty_results(self) -> None:
        event = _make_event(user_id=7)
        bt._wizards[7] = self._state()
        state = bt._wizards[7]
        with patch(f"{_LIST}.session_scope", _fake_session_scope), \
             patch(f"{_LIST}.templates_service.search",
                   AsyncMock(return_value=[])):
            consumed = await bt._step_search(event, 7, state, "паводок")
        assert consumed is True
        # После поиска wizard дропается (одноразовый сценарий).
        assert 7 not in bt._wizards
        # Текст содержит сам запрос.
        assert "паводок" in event.message.answer.await_args.args[0]

    @pytest.mark.asyncio
    async def test_results_found_shows_header_with_count(self) -> None:
        event = _make_event(user_id=7)
        bt._wizards[7] = self._state()
        state = bt._wizards[7]
        results = [_tmpl(tid=1, name="A"), _tmpl(tid=2, name="B")]
        with patch(f"{_LIST}.session_scope", _fake_session_scope), \
             patch(f"{_LIST}.templates_service.search",
                   AsyncMock(return_value=results)):
            consumed = await bt._step_search(event, 7, state, "вода")
        assert consumed is True
        assert 7 not in bt._wizards
        text = event.message.answer.await_args.args[0]
        assert "вода" in text
        assert "2" in text  # число найденных


# ══════════════════════════════════════════════════════════════════════
# 12. _step_new_text — too-long, happy-path (переход в preview).
#    Дополняет handler-тест (там happy-path есть, тут — длина).
# ══════════════════════════════════════════════════════════════════════


class TestStepNewText:
    @pytest.mark.asyncio
    async def test_too_long_text_rejected_keeps_step(self) -> None:
        from aemr_bot.config import settings as cfg

        event = _make_event(user_id=7)
        state = bt._TmplWizardState(
            step="new_awaiting_text", pending_name="X"
        )
        consumed = await bt._step_new_text(
            event, 7, state, "z" * (cfg.broadcast_max_chars + 1), op_id=99
        )
        assert consumed is True
        event.message.answer.assert_awaited()
        # Шаг не изменился — текст не принят.
        assert state.step == "new_awaiting_text"

    @pytest.mark.asyncio
    async def test_empty_text_reprompts_keeps_step(self) -> None:
        # `handle_wizard_text` подаёт сюда уже .strip()'нутый текст; пустой
        # ввод приходит как "" и отбивается до session_scope (re-prompt).
        event = _make_event(user_id=7)
        state = bt._TmplWizardState(
            step="new_awaiting_text", pending_name="X"
        )
        consumed = await bt._step_new_text(event, 7, state, "", op_id=99)
        assert consumed is True
        event.message.answer.assert_awaited()
        assert state.step == "new_awaiting_text"


# ══════════════════════════════════════════════════════════════════════
# 13. Навигация назад: _back_to_name, _back_to_text_new,
#     _back_to_text_edit — переходы + stale-guard'ы.
# ══════════════════════════════════════════════════════════════════════


class TestBackNavigation:
    @pytest.mark.asyncio
    async def test_back_to_name_from_text_resets_step(self) -> None:
        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(
            step="new_awaiting_text", pending_name="X"
        )
        with patch(f"{_BT}.get_user_id", return_value=7), \
             patch(f"{_BT}.send_or_edit_screen", AsyncMock()):
            await bt._back_to_name(event)
        assert bt._wizards[7].step == "new_awaiting_name"

    @pytest.mark.asyncio
    async def test_back_to_name_stale_shows_cancelled(self) -> None:
        """Кнопка «назад к имени», но wizard уже в другом шаге/закрыт →
        CANCELLED (защита от устаревшей кнопки)."""
        from aemr_bot import texts

        event = _make_event(user_id=7)
        # wizard отсутствует.
        with patch(f"{_WIZ}.get_user_id", return_value=7), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()) as send:
            await bt._back_to_name(event)
        assert send.await_args.kwargs["text"] == texts.OP_TMPL_CANCELLED

    @pytest.mark.asyncio
    async def test_back_to_text_new_from_preview_resets_step(self) -> None:
        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(
            step="new_preview", pending_name="X", pending_text="t"
        )
        with patch(f"{_BT}.get_user_id", return_value=7), \
             patch(f"{_BT}.send_or_edit_screen", AsyncMock()):
            await bt._back_to_text_new(event)
        assert bt._wizards[7].step == "new_awaiting_text"

    @pytest.mark.asyncio
    async def test_back_to_text_new_wrong_state_noop(self) -> None:
        """state не в new_preview → ранний return без перерисовки."""
        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(step="new_awaiting_name")
        with patch(f"{_BT}.get_user_id", return_value=7), \
             patch(f"{_BT}.send_or_edit_screen", AsyncMock()) as send:
            await bt._back_to_text_new(event)
        send.assert_not_awaited()
        # Шаг не тронут.
        assert bt._wizards[7].step == "new_awaiting_name"

    @pytest.mark.asyncio
    async def test_back_to_text_edit_from_preview_resets_step(self) -> None:
        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(
            step="edit_preview", target_id=5,
            pending_name="Док", pending_text="t",
        )
        with patch(f"{_BT}.get_user_id", return_value=7), \
             patch(f"{_BT}.send_or_edit_screen", AsyncMock()):
            await bt._back_to_text_edit(event, 5)
        assert bt._wizards[7].step == "edit_awaiting_text"


# ══════════════════════════════════════════════════════════════════════
# 14. _cancel + handle_wizard_text dispatch по шагам (clone/search).
#     Дополняет handler-тест (там new_name/new_text/cancel есть).
# ══════════════════════════════════════════════════════════════════════


class TestCancelAndDispatch:
    @pytest.mark.asyncio
    async def test_cancel_drops_wizard_and_shows_cancelled(self) -> None:
        from aemr_bot import texts

        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(step="new_awaiting_name")
        with patch(f"{_WIZ}.get_user_id", return_value=7), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()) as send:
            await bt._cancel(event)
        assert 7 not in bt._wizards
        assert send.await_args.kwargs["text"] == texts.OP_TMPL_CANCELLED

    @pytest.mark.asyncio
    async def test_wizard_text_routes_clone_step(self) -> None:
        """handle_wizard_text при step=clone_awaiting_name делегирует в
        _step_clone_name."""
        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(
            step="clone_awaiting_name", pending_text="t",
            pending_attachments=[], target_id=3, source_name="S",
        )
        with patch(f"{_BT}.is_admin_chat", return_value=True), \
             patch(f"{_BT}.get_user_id", return_value=7), \
             patch(f"{_BT}.get_operator",
                   AsyncMock(return_value=SimpleNamespace(id=99))), \
             patch(f"{_BT}._step_clone_name",
                   AsyncMock(return_value=True)) as step:
            consumed = await bt.handle_wizard_text(event, "Новое имя")
        assert consumed is True
        step.assert_awaited_once()
        # Прокинуты actor_id, state, text, op_id.
        assert step.await_args.args[1] == 7
        assert step.await_args.args[3] == "Новое имя"

    @pytest.mark.asyncio
    async def test_wizard_text_routes_search_step(self) -> None:
        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(step="search_awaiting_query")
        with patch(f"{_BT}.is_admin_chat", return_value=True), \
             patch(f"{_BT}.get_user_id", return_value=7), \
             patch(f"{_BT}.get_operator", AsyncMock(return_value=None)), \
             patch(f"{_BT}._step_search",
                   AsyncMock(return_value=True)) as step:
            consumed = await bt.handle_wizard_text(event, "запрос")
        assert consumed is True
        step.assert_awaited_once()


# ══════════════════════════════════════════════════════════════════════
# 15. handle_callback — новые exact/verb:id ветки (search, clone,
#     save_new, save_edit, back_to_text_*). Дополняет handler-тест.
# ══════════════════════════════════════════════════════════════════════


class TestHandleCallbackNewBranches:
    @pytest.mark.asyncio
    async def test_search_exact_dispatched(self) -> None:
        event = _make_event(user_id=7)
        with patch(f"{_BT}.is_admin_chat", return_value=True), \
             patch(f"{_BT}.ack_callback", AsyncMock()), \
             patch(f"{_BT}._start_search", AsyncMock()) as start:
            result = await bt.handle_callback(event, "op:tmpl:search")
        assert result is True
        start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_save_new_exact_dispatched(self) -> None:
        event = _make_event(user_id=7)
        with patch(f"{_BT}.is_admin_chat", return_value=True), \
             patch(f"{_BT}.ack_callback", AsyncMock()), \
             patch(f"{_BT}._save_new", AsyncMock()) as save:
            result = await bt.handle_callback(event, "op:tmpl:save_new")
        assert result is True
        save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clone_verb_id_dispatched(self) -> None:
        event = _make_event(user_id=7)
        with patch(f"{_BT}.is_admin_chat", return_value=True), \
             patch(f"{_BT}.ack_callback", AsyncMock()), \
             patch(f"{_BT}._start_clone", AsyncMock()) as clone:
            result = await bt.handle_callback(event, "op:tmpl:clone:8")
        assert result is True
        clone.assert_awaited_once()
        assert clone.await_args.args[1] == 8

    @pytest.mark.asyncio
    async def test_save_edit_verb_id_dispatched(self) -> None:
        event = _make_event(user_id=7)
        with patch(f"{_BT}.is_admin_chat", return_value=True), \
             patch(f"{_BT}.ack_callback", AsyncMock()), \
             patch(f"{_BT}._save_edit", AsyncMock()) as save:
            result = await bt.handle_callback(event, "op:tmpl:save_edit:8")
        assert result is True
        save.assert_awaited_once()
        assert save.await_args.args[1] == 8

    @pytest.mark.asyncio
    async def test_back_to_text_edit_verb_id_dispatched(self) -> None:
        event = _make_event(user_id=7)
        with patch(f"{_BT}.is_admin_chat", return_value=True), \
             patch(f"{_BT}.ack_callback", AsyncMock()), \
             patch(f"{_BT}._back_to_text_edit", AsyncMock()) as back:
            result = await bt.handle_callback(
                event, "op:tmpl:back_to_text_edit:8"
            )
        assert result is True
        back.assert_awaited_once()
        assert back.await_args.args[1] == 8

    @pytest.mark.asyncio
    async def test_unknown_verb_with_id_returns_false(self) -> None:
        """Неизвестный verb с валидным id → False (caller делает
        fallthrough). Характеризуем: int(raw_id) OK, но verb не из
        списка."""
        event = _make_event(user_id=7)
        with patch(f"{_BT}.is_admin_chat", return_value=True), \
             patch(f"{_BT}.ack_callback", AsyncMock()):
            result = await bt.handle_callback(event, "op:tmpl:frobnicate:8")
        assert result is False
