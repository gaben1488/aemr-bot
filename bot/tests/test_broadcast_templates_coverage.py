"""Coverage-добор для кластера handlers/broadcast_templates.* .

Характеризационный файл (`test_broadcast_templates_characterization.py`)
уже плотно покрывает leaf-функции end-to-end (apply/save/step_*), а
`test_broadcast_templates_handlers.py` — часть разводки `handle_callback`
и happy-path wizard-текста. Этот файл — ДОБОР по непокрытым веткам,
найденным через `--cov-report=term-missing`. Ничего из перечисленного
ниже не дублирует существующие файлы:

  - **`_start_*`-входы** (`_start_new`, `_start_edit`, `_start_rename`,
    `_start_clone`, `_ask_delete`, `_start_search`): открывают wizard /
    рисуют prompt. В существующих файлах эти entry-points НЕ вызываются
    напрямую — только их `_step_*`-продолжения. Проверяем: роль-гард,
    not-found, посадку state в `_wizards` с верным шагом/target_id;
  - **dispatch-ветки фасада** `handle_callback`, не покрытые handler-
    тестом: `new`, `cancel`, `back_to_name`, `back_to_text_new`,
    `rename`, `edit` (handler-тест покрывал list/open/apply/delete/
    search/clone/save_new/save_edit/back_to_text_edit);
  - **`handle_wizard_text`-маршрутизация** для `rename_awaiting_name` и
    `edit_awaiting_text` (clone/search уже в characterization; new_name/
    new_text — в handler-тесте) + edge-гард `actor_id is None` и
    «неизвестный шаг → return False»;
  - **edge-ветки `_step_edit`** при пустом тексте: `target_id is None`
    (346->360) и «target есть, но шаблон удалён» (354->360) — обе ведут
    к reprompt с name="?";
  - **валидация-reject `_step_rename`/`_step_clone_name`** (пустое/
    длинное имя → ранний выход без БД) и `_step_rename` с `target_id
    is None` (возврат False);
  - **state-helpers** `_drop_expired` (GC протухших wizard'ов) и GC-ветка
    `_mark_apply` (чистка dedupe-словаря при >256 записях).

Стиль и патч-дисциплина — как в characterization: патчим символы ПО
МЕСТУ резолва в подмодуле, где определена leaf-функция (урок PR #139);
диспетчерные символы (`is_admin_chat`, `ack_callback`, `get_user_id`,
`get_operator`) — на фасаде `_BT`. Клавиатуры (pure-функции) не мокаем.
БД — через `fake_session_scope` + моки `templates_service.*`.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")

from aemr_bot.handlers import broadcast_templates as bt  # noqa: E402
from aemr_bot.handlers import broadcast_templates_crud as bt_crud  # noqa: E402

_BT = "aemr_bot.handlers.broadcast_templates"
_LIST = "aemr_bot.handlers.broadcast_templates_list"
_CRUD = "aemr_bot.handlers.broadcast_templates_crud"
_WIZ = "aemr_bot.handlers.broadcast_templates_wizard"
_STATE = "aemr_bot.handlers.broadcast_templates_state"


def _make_event(*, user_id: int = 7, chat_id: int = 123) -> SimpleNamespace:
    """MAX-событие как в characterization: chat_id=123 == ADMIN_GROUP_ID
    из conftest, отдельный AsyncMock на `message.answer`."""
    event = make_event(chat_id=chat_id, user_id=user_id, with_callback=True)
    event.message.answer = AsyncMock()
    return event


@pytest.fixture(autouse=True)
def _clean_state():
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
):
    return SimpleNamespace(
        id=tid,
        name=name,
        text=text,
        attachments=attachments if attachments is not None else [],
        use_count=0,
        last_used_at=None,
        created_at=None,
    )


# ══════════════════════════════════════════════════════════════════════
# 1. `_start_*`-входы: открытие wizard'а / отрисовка prompt.
#    Эти entry-points не вызывались напрямую ни в characterization
#    (там только _step_*), ни в handler-тесте.
# ══════════════════════════════════════════════════════════════════════


class TestStartNew:
    @pytest.mark.asyncio
    async def test_denied_does_not_open_wizard(self) -> None:
        event = _make_event(user_id=7)
        send = AsyncMock()
        with patch(f"{_WIZ}.ensure_role", AsyncMock(return_value=False)), \
             patch(f"{_WIZ}.get_user_id", return_value=7), \
             patch(f"{_WIZ}.send_or_edit_screen", send):
            await bt._start_new(event)
        assert 7 not in bt._wizards
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_opens_wizard_in_new_awaiting_name(self) -> None:
        """Успех: state посажен в new_awaiting_name, показан name-prompt."""
        event = _make_event(user_id=7)
        send = AsyncMock()
        with patch(f"{_WIZ}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_WIZ}.get_user_id", return_value=7), \
             patch(f"{_WIZ}.send_or_edit_screen", send):
            await bt._start_new(event)
        assert 7 in bt._wizards
        assert bt._wizards[7].step == "new_awaiting_name"
        send.assert_awaited_once()


class TestStartEdit:
    @pytest.mark.asyncio
    async def test_denied_returns_without_db(self) -> None:
        event = _make_event(user_id=7)
        get_by_id = AsyncMock()
        with patch(f"{_WIZ}.ensure_role", AsyncMock(return_value=False)), \
             patch(f"{_WIZ}.templates_service.get_by_id", get_by_id), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()):
            await bt._start_edit(event, 5)
        get_by_id.assert_not_awaited()
        assert 7 not in bt._wizards

    @pytest.mark.asyncio
    async def test_not_found_shows_not_found_no_wizard(self) -> None:
        from aemr_bot import texts

        event = _make_event(user_id=7)
        with patch(f"{_WIZ}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_WIZ}.get_user_id", return_value=7), \
             patch(f"{_WIZ}.session_scope", _fake_session_scope), \
             patch(f"{_WIZ}.templates_service.get_by_id",
                   AsyncMock(return_value=None)), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()) as send:
            await bt._start_edit(event, 999)
        assert send.await_args.kwargs["text"] == texts.OP_TMPL_NOT_FOUND
        # Шаблона нет → wizard не открываем.
        assert 7 not in bt._wizards

    @pytest.mark.asyncio
    async def test_opens_wizard_edit_awaiting_text_with_target(self) -> None:
        event = _make_event(user_id=7)
        with patch(f"{_WIZ}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_WIZ}.get_user_id", return_value=7), \
             patch(f"{_WIZ}.session_scope", _fake_session_scope), \
             patch(f"{_WIZ}.templates_service.get_by_id",
                   AsyncMock(return_value=_tmpl(tid=5, name="Док"))), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()) as send:
            await bt._start_edit(event, 5)
        assert bt._wizards[7].step == "edit_awaiting_text"
        assert bt._wizards[7].target_id == 5
        send.assert_awaited_once()


class TestStartRename:
    @pytest.mark.asyncio
    async def test_denied_returns_without_db(self) -> None:
        event = _make_event(user_id=7)
        get_by_id = AsyncMock()
        with patch(f"{_CRUD}.ensure_role", AsyncMock(return_value=False)), \
             patch(f"{_CRUD}.templates_service.get_by_id", get_by_id), \
             patch(f"{_CRUD}.send_or_edit_screen", AsyncMock()):
            await bt._start_rename(event, 5)
        get_by_id.assert_not_awaited()
        assert 7 not in bt._wizards

    @pytest.mark.asyncio
    async def test_not_found_no_wizard(self) -> None:
        from aemr_bot import texts

        event = _make_event(user_id=7)
        with patch(f"{_CRUD}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_CRUD}.get_user_id", return_value=7), \
             patch(f"{_CRUD}.session_scope", _fake_session_scope), \
             patch(f"{_CRUD}.templates_service.get_by_id",
                   AsyncMock(return_value=None)), \
             patch(f"{_CRUD}.send_or_edit_screen", AsyncMock()) as send:
            await bt._start_rename(event, 999)
        assert send.await_args.kwargs["text"] == texts.OP_TMPL_NOT_FOUND
        assert 7 not in bt._wizards

    @pytest.mark.asyncio
    async def test_opens_rename_wizard_with_target(self) -> None:
        """Prompt содержит старое имя; state — rename_awaiting_name +
        target_id."""
        event = _make_event(user_id=7)
        with patch(f"{_CRUD}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_CRUD}.get_user_id", return_value=7), \
             patch(f"{_CRUD}.session_scope", _fake_session_scope), \
             patch(f"{_CRUD}.templates_service.get_by_id",
                   AsyncMock(return_value=_tmpl(tid=5, name="Старое имя"))), \
             patch(f"{_CRUD}.send_or_edit_screen", AsyncMock()) as send:
            await bt._start_rename(event, 5)
        assert bt._wizards[7].step == "rename_awaiting_name"
        assert bt._wizards[7].target_id == 5
        assert "Старое имя" in send.await_args.kwargs["text"]


class TestStartClone:
    @pytest.mark.asyncio
    async def test_denied_returns_without_db(self) -> None:
        event = _make_event(user_id=7)
        get_by_id = AsyncMock()
        with patch(f"{_CRUD}.ensure_role", AsyncMock(return_value=False)), \
             patch(f"{_CRUD}.templates_service.get_by_id", get_by_id), \
             patch(f"{_CRUD}.send_or_edit_screen", AsyncMock()):
            await bt._start_clone(event, 5)
        get_by_id.assert_not_awaited()
        assert 7 not in bt._wizards

    @pytest.mark.asyncio
    async def test_not_found_no_wizard(self) -> None:
        from aemr_bot import texts

        event = _make_event(user_id=7)
        with patch(f"{_CRUD}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_CRUD}.get_user_id", return_value=7), \
             patch(f"{_CRUD}.session_scope", _fake_session_scope), \
             patch(f"{_CRUD}.templates_service.get_by_id",
                   AsyncMock(return_value=None)), \
             patch(f"{_CRUD}.send_or_edit_screen", AsyncMock()) as send:
            await bt._start_clone(event, 999)
        assert send.await_args.kwargs["text"] == texts.OP_TMPL_NOT_FOUND
        assert 7 not in bt._wizards

    @pytest.mark.asyncio
    async def test_opens_clone_wizard_preloads_source_text_and_atts(
        self,
    ) -> None:
        """clone предзаряжает pending_text/attachments источника, шаг —
        clone_awaiting_name, target_id = id источника, source_name."""
        event = _make_event(user_id=7)
        src = _tmpl(
            tid=3,
            name="Источник",
            text="текст источника",
            attachments=[{"img": 1}],
        )
        with patch(f"{_CRUD}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_CRUD}.get_user_id", return_value=7), \
             patch(f"{_CRUD}.session_scope", _fake_session_scope), \
             patch(f"{_CRUD}.templates_service.get_by_id",
                   AsyncMock(return_value=src)), \
             patch(f"{_CRUD}.send_or_edit_screen", AsyncMock()) as send:
            await bt._start_clone(event, 3)
        st = bt._wizards[7]
        assert st.step == "clone_awaiting_name"
        assert st.target_id == 3
        assert st.source_name == "Источник"
        assert st.pending_text == "текст источника"
        assert st.pending_attachments == [{"img": 1}]
        # Prompt упоминает имя источника.
        assert "Источник" in send.await_args.kwargs["text"]


class TestAskDelete:
    @pytest.mark.asyncio
    async def test_denied_returns_without_db(self) -> None:
        event = _make_event(user_id=7)
        get_by_id = AsyncMock()
        with patch(f"{_CRUD}.ensure_role", AsyncMock(return_value=False)), \
             patch(f"{_CRUD}.templates_service.get_by_id", get_by_id), \
             patch(f"{_CRUD}.send_or_edit_screen", AsyncMock()):
            await bt._ask_delete(event, 5)
        get_by_id.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_not_found_shows_not_found(self) -> None:
        from aemr_bot import texts

        event = _make_event(user_id=7)
        with patch(f"{_CRUD}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_CRUD}.session_scope", _fake_session_scope), \
             patch(f"{_CRUD}.templates_service.get_by_id",
                   AsyncMock(return_value=None)), \
             patch(f"{_CRUD}.send_or_edit_screen", AsyncMock()) as send:
            await bt._ask_delete(event, 999)
        assert send.await_args.kwargs["text"] == texts.OP_TMPL_NOT_FOUND

    @pytest.mark.asyncio
    async def test_shows_confirm_with_name(self) -> None:
        """Подтверждение удаления содержит имя шаблона (оператор видит,
        что именно архивирует)."""
        event = _make_event(user_id=7)
        with patch(f"{_CRUD}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_CRUD}.session_scope", _fake_session_scope), \
             patch(f"{_CRUD}.templates_service.get_by_id",
                   AsyncMock(return_value=_tmpl(tid=5, name="Удаляемый"))), \
             patch(f"{_CRUD}.send_or_edit_screen", AsyncMock()) as send:
            await bt._ask_delete(event, 5)
        assert "Удаляемый" in send.await_args.kwargs["text"]


class TestStartSearch:
    @pytest.mark.asyncio
    async def test_denied_does_not_open_wizard(self) -> None:
        event = _make_event(user_id=7)
        with patch(f"{_LIST}.ensure_role", AsyncMock(return_value=False)), \
             patch(f"{_LIST}.get_user_id", return_value=7), \
             patch(f"{_LIST}.send_or_edit_screen", AsyncMock()) as send:
            await bt._start_search(event)
        assert 7 not in bt._wizards
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_opens_search_wizard(self) -> None:
        from aemr_bot import texts

        event = _make_event(user_id=7)
        with patch(f"{_LIST}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_LIST}.get_user_id", return_value=7), \
             patch(f"{_LIST}.send_or_edit_screen", AsyncMock()) as send:
            await bt._start_search(event)
        assert bt._wizards[7].step == "search_awaiting_query"
        assert send.await_args.kwargs["text"] == texts.OP_TMPL_SEARCH_PROMPT


# ══════════════════════════════════════════════════════════════════════
# 2. handle_callback — dispatch-ветки, не покрытые handler-тестом:
#    new / cancel / back_to_name / back_to_text_new / rename / edit.
# ══════════════════════════════════════════════════════════════════════


class TestHandleCallbackRemainingBranches:
    @pytest.mark.asyncio
    async def test_new_exact_dispatched(self) -> None:
        event = _make_event(user_id=7)
        with patch(f"{_BT}.is_admin_chat", return_value=True), \
             patch(f"{_BT}.ack_callback", AsyncMock()) as ack, \
             patch(f"{_BT}._start_new", AsyncMock()) as start:
            result = await bt.handle_callback(event, "op:tmpl:new")
        assert result is True
        ack.assert_awaited_once()
        start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancel_exact_dispatched(self) -> None:
        event = _make_event(user_id=7)
        with patch(f"{_BT}.is_admin_chat", return_value=True), \
             patch(f"{_BT}.ack_callback", AsyncMock()), \
             patch(f"{_BT}._cancel", AsyncMock()) as cancel:
            result = await bt.handle_callback(event, "op:tmpl:cancel")
        assert result is True
        cancel.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_back_to_name_exact_dispatched(self) -> None:
        event = _make_event(user_id=7)
        with patch(f"{_BT}.is_admin_chat", return_value=True), \
             patch(f"{_BT}.ack_callback", AsyncMock()), \
             patch(f"{_BT}._back_to_name", AsyncMock()) as back:
            result = await bt.handle_callback(event, "op:tmpl:back_to_name")
        assert result is True
        back.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_back_to_text_new_exact_dispatched(self) -> None:
        event = _make_event(user_id=7)
        with patch(f"{_BT}.is_admin_chat", return_value=True), \
             patch(f"{_BT}.ack_callback", AsyncMock()), \
             patch(f"{_BT}._back_to_text_new", AsyncMock()) as back:
            result = await bt.handle_callback(
                event, "op:tmpl:back_to_text_new"
            )
        assert result is True
        back.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rename_verb_id_dispatched(self) -> None:
        event = _make_event(user_id=7)
        with patch(f"{_BT}.is_admin_chat", return_value=True), \
             patch(f"{_BT}.ack_callback", AsyncMock()), \
             patch(f"{_BT}._start_rename", AsyncMock()) as rename:
            result = await bt.handle_callback(event, "op:tmpl:rename:8")
        assert result is True
        rename.assert_awaited_once()
        assert rename.await_args.args[1] == 8

    @pytest.mark.asyncio
    async def test_edit_verb_id_dispatched(self) -> None:
        event = _make_event(user_id=7)
        with patch(f"{_BT}.is_admin_chat", return_value=True), \
             patch(f"{_BT}.ack_callback", AsyncMock()), \
             patch(f"{_BT}._start_edit", AsyncMock()) as edit:
            result = await bt.handle_callback(event, "op:tmpl:edit:8")
        assert result is True
        edit.assert_awaited_once()
        assert edit.await_args.args[1] == 8

    @pytest.mark.asyncio
    async def test_non_tmpl_prefix_returns_false(self) -> None:
        """payload не с `op:tmpl:` → ранний False (строка 124)."""
        event = _make_event(user_id=7)
        with patch(f"{_BT}.is_admin_chat", return_value=True):
            result = await bt.handle_callback(event, "op:broadcast:start")
        assert result is False


# ══════════════════════════════════════════════════════════════════════
# 3. handle_wizard_text — маршрутизация rename/edit + edge-гарды.
#    (clone/search покрыты в characterization; new_name/new_text — в
#    handler-тесте.)
# ══════════════════════════════════════════════════════════════════════


class TestWizardTextRouting:
    @pytest.mark.asyncio
    async def test_routes_rename_step(self) -> None:
        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(
            step="rename_awaiting_name", target_id=5
        )
        with patch(f"{_BT}.is_admin_chat", return_value=True), \
             patch(f"{_BT}.get_user_id", return_value=7), \
             patch(f"{_BT}.get_operator",
                   AsyncMock(return_value=SimpleNamespace(id=99))), \
             patch(f"{_BT}._step_rename",
                   AsyncMock(return_value=True)) as step:
            consumed = await bt.handle_wizard_text(event, "Новое имя")
        assert consumed is True
        step.assert_awaited_once()
        # Проброшены actor_id, text и op_id.
        assert step.await_args.args[1] == 7
        assert step.await_args.args[3] == "Новое имя"
        assert step.await_args.kwargs["op_id"] == 99

    @pytest.mark.asyncio
    async def test_routes_edit_step(self) -> None:
        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(
            step="edit_awaiting_text", target_id=5
        )
        with patch(f"{_BT}.is_admin_chat", return_value=True), \
             patch(f"{_BT}.get_user_id", return_value=7), \
             patch(f"{_BT}.get_operator", AsyncMock(return_value=None)), \
             patch(f"{_BT}._step_edit",
                   AsyncMock(return_value=True)) as step:
            consumed = await bt.handle_wizard_text(event, "новый текст")
        assert consumed is True
        step.assert_awaited_once()
        # get_operator вернул None → op_id=None.
        assert step.await_args.kwargs["op_id"] is None

    @pytest.mark.asyncio
    async def test_actor_id_none_returns_false(self) -> None:
        """get_user_id вернул None → ранний False (строка 221), state не
        ищется."""
        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(step="new_awaiting_name")
        with patch(f"{_BT}.is_admin_chat", return_value=True), \
             patch(f"{_BT}.get_user_id", return_value=None):
            consumed = await bt.handle_wizard_text(event, "что-то")
        assert consumed is False

    @pytest.mark.asyncio
    async def test_unknown_step_returns_false(self) -> None:
        """Шаг wizard'а не входит ни в одну ветку диспетчера → финальный
        return False (строка 249). new_preview не обрабатывается через
        текст (только через callback), поэтому это «неизвестный» шаг для
        handle_wizard_text."""
        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(
            step="new_preview", pending_name="X", pending_text="t"
        )
        with patch(f"{_BT}.is_admin_chat", return_value=True), \
             patch(f"{_BT}.get_user_id", return_value=7), \
             patch(f"{_BT}.get_operator", AsyncMock(return_value=None)):
            consumed = await bt.handle_wizard_text(event, "текст")
        assert consumed is False


# ══════════════════════════════════════════════════════════════════════
# 4. _step_edit — edge-ветки при ПУСТОМ тексте:
#    - target_id is None (346->360): сразу reprompt name="?";
#    - target есть, но шаблон удалён (354->360): reprompt name="?".
#    (Image-only-замена и too-long уже в characterization.)
# ══════════════════════════════════════════════════════════════════════


class TestStepEditEmptyTextEdges:
    @pytest.mark.asyncio
    async def test_empty_text_target_none_reprompts_question_mark(
        self,
    ) -> None:
        """Пустой текст + target_id is None → блок image-only пропущен,
        reprompt с именем «?» (нечего подставить). state не меняется."""
        event = _make_event(user_id=7)
        state = bt._TmplWizardState(
            step="edit_awaiting_text", target_id=None
        )
        get_by_id = AsyncMock()
        with patch(f"{_WIZ}.session_scope", _fake_session_scope), \
             patch(f"{_WIZ}.broadcast_handler._resolve_broadcast_max_images",
                   AsyncMock(return_value=5)), \
             patch(f"{_WIZ}.templates_service.get_by_id", get_by_id):
            consumed = await bt._step_edit(event, 7, state, "", op_id=99)
        assert consumed is True
        # target_id None → к БД не ходим вообще.
        get_by_id.assert_not_awaited()
        event.message.answer.assert_awaited()
        # Reprompt → шаг остаётся прежним.
        assert state.step == "edit_awaiting_text"

    @pytest.mark.asyncio
    async def test_empty_text_target_gone_reprompts(self) -> None:
        """Пустой текст, target задан, но шаблон удалён (get_by_id None) →
        new_attachments не собираем, reprompt name='?'. Характеризуем как
        есть: wizard НЕ дропается (в отличие от непустого пути)."""
        event = _make_event(user_id=7)
        state = bt._TmplWizardState(
            step="edit_awaiting_text", target_id=5
        )
        atts_from_event = AsyncMock()
        with patch(f"{_WIZ}.session_scope", _fake_session_scope), \
             patch(f"{_WIZ}.broadcast_handler._resolve_broadcast_max_images",
                   AsyncMock(return_value=5)), \
             patch(f"{_WIZ}.templates_service.get_by_id",
                   AsyncMock(return_value=None)), \
             patch(f"{_WIZ}._image_attachments.image_attachments_from_event",
                   atts_from_event):
            consumed = await bt._step_edit(event, 7, state, "", op_id=99)
        assert consumed is True
        # tmpl_before is None → картинки из события не разбираем.
        atts_from_event.assert_not_called()
        event.message.answer.assert_awaited()
        assert state.step == "edit_awaiting_text"


# ══════════════════════════════════════════════════════════════════════
# 5. Валидация-reject и target_id-None в crud-step'ах.
# ══════════════════════════════════════════════════════════════════════


class TestStepRenameGuards:
    @pytest.mark.asyncio
    async def test_target_id_none_returns_false(self) -> None:
        """Валидное имя, но target_id is None (битый state) → wizard
        снят, возврат False (caller делает fallthrough), к БД не ходим."""
        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(
            step="rename_awaiting_name", target_id=None
        )
        state = bt._wizards[7]
        get_by_id = AsyncMock()
        with patch(f"{_CRUD}.session_scope", _fake_session_scope), \
             patch(f"{_CRUD}.templates_service.get_by_id", get_by_id):
            consumed = await bt._step_rename(
                event, 7, state, "Корректное имя", op_id=99
            )
        assert consumed is False
        assert 7 not in bt._wizards
        get_by_id.assert_not_awaited()


class TestStepCloneNameValidation:
    @pytest.mark.asyncio
    async def test_empty_name_rejected_keeps_wizard(self) -> None:
        """Пустое имя клона → ранний reject через message.answer,
        create_template НЕ зовётся, wizard жив."""
        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(
            step="clone_awaiting_name",
            pending_text="t",
            pending_attachments=[],
            target_id=3,
            source_name="S",
        )
        state = bt._wizards[7]
        create = AsyncMock()
        with patch(f"{_CRUD}.session_scope", _fake_session_scope), \
             patch(f"{_CRUD}.templates_service.create_template", create):
            consumed = await bt._step_clone_name(
                event, 7, state, "", op_id=99
            )
        assert consumed is True
        create.assert_not_awaited()
        event.message.answer.assert_awaited()
        assert 7 in bt._wizards

    @pytest.mark.asyncio
    async def test_too_long_name_rejected(self) -> None:
        event = _make_event(user_id=7)
        state = bt._TmplWizardState(
            step="clone_awaiting_name",
            pending_text="t",
            pending_attachments=[],
            target_id=3,
            source_name="S",
        )
        long_name = "z" * (bt_crud.templates_service.MAX_NAME_LEN + 1)
        create = AsyncMock()
        with patch(f"{_CRUD}.session_scope", _fake_session_scope), \
             patch(f"{_CRUD}.templates_service.create_template", create):
            consumed = await bt._step_clone_name(
                event, 7, state, long_name, op_id=99
            )
        assert consumed is True
        create.assert_not_awaited()
        event.message.answer.assert_awaited()


# ══════════════════════════════════════════════════════════════════════
# 6. _start_new / _start_edit / _start_rename / _start_clone /
#    _start_search — guard `actor_id is None` (get_user_id вернул None).
#    Закрепляем: wizard НЕ открывается, но (для тех, что уже прошли
#    ensure_role+get_by_id) повторного обращения к БД нет.
# ══════════════════════════════════════════════════════════════════════


class TestStartActorNoneGuards:
    @pytest.mark.asyncio
    async def test_start_new_actor_none_no_wizard(self) -> None:
        event = _make_event(user_id=7)
        send = AsyncMock()
        with patch(f"{_WIZ}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_WIZ}.get_user_id", return_value=None), \
             patch(f"{_WIZ}.send_or_edit_screen", send):
            await bt._start_new(event)
        # actor_id None → return до посадки state и до prompt.
        assert bt._wizards == {}
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_search_actor_none_no_wizard(self) -> None:
        event = _make_event(user_id=7)
        send = AsyncMock()
        with patch(f"{_LIST}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_LIST}.get_user_id", return_value=None), \
             patch(f"{_LIST}.send_or_edit_screen", send):
            await bt._start_search(event)
        assert bt._wizards == {}
        send.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════
# 7. state-helpers: _drop_expired (GC протухших wizard'ов) и GC-ветка
#    _mark_apply (purge dedupe при >256 записях).
# ══════════════════════════════════════════════════════════════════════


class TestStateHelpers:
    def test_drop_expired_removes_only_stale(self) -> None:
        """_drop_expired чистит только wizard'ы с expired()==True, живые
        не трогает."""
        fresh = bt._TmplWizardState(step="new_awaiting_name")
        stale = bt._TmplWizardState(step="new_awaiting_name")
        stale.expires_at = 0.0  # давно протух
        bt._wizards[1] = fresh
        bt._wizards[2] = stale
        from aemr_bot.handlers.broadcast_templates_state import _drop_expired

        _drop_expired()
        assert 1 in bt._wizards
        assert 2 not in bt._wizards

    def test_mark_apply_gc_purges_old_entries_over_threshold(self) -> None:
        """_mark_apply при >256 записях запускает GC: записи старше 5 окон
        (3с*5=15с) удаляются, свежие остаются. Заполняем словарь >256
        «древними» ключами, затем mark → старьё вычищено."""
        now = time.monotonic()
        old_ts = now - 100.0  # >> 15с — под чистку
        # 300 древних записей напрямую в словарь (минуя mark, чтобы ts был
        # старым).
        for i in range(300):
            bt._apply_dedupe[(1000 + i, 1)] = old_ts
        assert len(bt._apply_dedupe) == 300
        from aemr_bot.handlers.broadcast_templates_state import _mark_apply

        # mark новой записи → len>256 → GC отрабатывает.
        _mark_apply(7, 5)
        # Все 300 древних вычищены; осталась только что помеченная.
        assert (7, 5) in bt._apply_dedupe
        assert (1000, 1) not in bt._apply_dedupe
        # GC удалил протухшие — словарь резко сократился.
        assert len(bt._apply_dedupe) < 300

    def test_mark_apply_keeps_recent_entries_during_gc(self) -> None:
        """GC при >256 записях НЕ удаляет свежие записи (моложе 15с)."""
        now = time.monotonic()
        old_ts = now - 100.0
        # 260 старых + несколько свежих.
        for i in range(260):
            bt._apply_dedupe[(2000 + i, 1)] = old_ts
        recent_key = (9999, 9)
        bt._apply_dedupe[recent_key] = now  # свежая
        from aemr_bot.handlers.broadcast_templates_state import _mark_apply

        _mark_apply(7, 5)
        # Свежая запись пережила GC.
        assert recent_key in bt._apply_dedupe
        assert (7, 5) in bt._apply_dedupe


# ══════════════════════════════════════════════════════════════════════
# 8. Защитные `actor_id is None`-гарды в callback-функциях, где
#    get_user_id отдаёт None (внутренний MAX-event без sender/user).
#    Закрепляем: побочек нет (ни pop wizard'а, ни запись в БД), либо
#    только финальный экран без чтения state.
# ══════════════════════════════════════════════════════════════════════


class TestActorNoneGuards:
    @pytest.mark.asyncio
    async def test_cancel_actor_none_still_shows_cancelled(self) -> None:
        """_cancel при actor_id None: pop wizard'а пропущен (нечего
        снимать), но экран CANCELLED всё равно рисуется."""
        from aemr_bot import texts

        event = _make_event(user_id=7)
        with patch(f"{_WIZ}.get_user_id", return_value=None), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()) as send:
            await bt._cancel(event)
        assert send.await_args.kwargs["text"] == texts.OP_TMPL_CANCELLED

    @pytest.mark.asyncio
    async def test_back_to_name_actor_none_noop(self) -> None:
        """_back_to_name при actor_id None → ранний return, экран не
        рисуется."""
        event = _make_event(user_id=7)
        with patch(f"{_WIZ}.get_user_id", return_value=None), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()) as send:
            await bt._back_to_name(event)
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_back_to_text_new_actor_none_noop(self) -> None:
        event = _make_event(user_id=7)
        with patch(f"{_WIZ}.get_user_id", return_value=None), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()) as send:
            await bt._back_to_text_new(event)
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_back_to_text_edit_actor_none_noop(self) -> None:
        event = _make_event(user_id=7)
        with patch(f"{_WIZ}.get_user_id", return_value=None), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()) as send:
            await bt._back_to_text_edit(event, 5)
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_back_to_text_edit_wrong_state_noop(self) -> None:
        """state не в edit_preview → ранний return без перерисовки
        (строка 519)."""
        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(step="edit_awaiting_text",
                                             target_id=5)
        with patch(f"{_WIZ}.get_user_id", return_value=7), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()) as send:
            await bt._back_to_text_edit(event, 5)
        send.assert_not_awaited()
        # Шаг не тронут.
        assert bt._wizards[7].step == "edit_awaiting_text"

    @pytest.mark.asyncio
    async def test_save_new_actor_none_noop(self) -> None:
        event = _make_event(user_id=7)
        create = AsyncMock()
        with patch(f"{_WIZ}.get_user_id", return_value=None), \
             patch(f"{_WIZ}.get_operator",
                   AsyncMock(return_value=SimpleNamespace(id=99))), \
             patch(f"{_WIZ}.templates_service.create_template", create), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()) as send:
            await bt._save_new(event)
        create.assert_not_awaited()
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_save_edit_actor_none_noop(self) -> None:
        event = _make_event(user_id=7)
        update = AsyncMock()
        with patch(f"{_WIZ}.get_user_id", return_value=None), \
             patch(f"{_WIZ}.get_operator",
                   AsyncMock(return_value=SimpleNamespace(id=99))), \
             patch(f"{_WIZ}.templates_service.update_text", update), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()) as send:
            await bt._save_edit(event, 5)
        update.assert_not_awaited()
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_apply_actor_none_returns_after_role(self) -> None:
        """_apply при actor_id None → return до dedupe-mark и до БД
        (строка 138). dedupe-словарь чист."""
        event = _make_event(user_id=7)
        get_by_id = AsyncMock()
        with patch(f"{_LIST}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_LIST}.get_user_id", return_value=None), \
             patch(f"{_LIST}.templates_service.get_by_id", get_by_id), \
             patch(f"{_LIST}.send_or_edit_screen", AsyncMock()):
            await bt._apply(event, 5)
        get_by_id.assert_not_awaited()
        assert bt._apply_dedupe == {}

    @pytest.mark.asyncio
    async def test_start_rename_actor_none_no_db(self) -> None:
        event = _make_event(user_id=7)
        get_by_id = AsyncMock()
        with patch(f"{_CRUD}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_CRUD}.get_user_id", return_value=None), \
             patch(f"{_CRUD}.templates_service.get_by_id", get_by_id), \
             patch(f"{_CRUD}.send_or_edit_screen", AsyncMock()):
            await bt._start_rename(event, 5)
        get_by_id.assert_not_awaited()
        assert bt._wizards == {}

    @pytest.mark.asyncio
    async def test_start_clone_actor_none_no_db(self) -> None:
        event = _make_event(user_id=7)
        get_by_id = AsyncMock()
        with patch(f"{_CRUD}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_CRUD}.get_user_id", return_value=None), \
             patch(f"{_CRUD}.templates_service.get_by_id", get_by_id), \
             patch(f"{_CRUD}.send_or_edit_screen", AsyncMock()):
            await bt._start_clone(event, 5)
        get_by_id.assert_not_awaited()
        assert bt._wizards == {}

    @pytest.mark.asyncio
    async def test_start_edit_actor_none_no_db(self) -> None:
        event = _make_event(user_id=7)
        get_by_id = AsyncMock()
        with patch(f"{_WIZ}.ensure_role", AsyncMock(return_value=True)), \
             patch(f"{_WIZ}.get_user_id", return_value=None), \
             patch(f"{_WIZ}.templates_service.get_by_id", get_by_id), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()):
            await bt._start_edit(event, 5)
        get_by_id.assert_not_awaited()
        assert bt._wizards == {}


# ══════════════════════════════════════════════════════════════════════
# 9. audit-skip при op_id is None в happy-path step'ах (ветка `if op_id
#    is not None`). Шаблон/правка применяются, но запись в audit_log
#    пропускается.
# ══════════════════════════════════════════════════════════════════════


class TestAuditSkipNoOperator:
    @pytest.mark.asyncio
    async def test_step_rename_no_operator_renames_skips_audit(self) -> None:
        """rename выполняется, но при op_id=None write_audit не зовётся
        (ветка 108->116)."""
        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(
            step="rename_awaiting_name", target_id=5
        )
        state = bt._wizards[7]
        rename = AsyncMock(return_value=_tmpl(tid=5, name="Новое"))
        write_audit = AsyncMock()
        with patch(f"{_CRUD}.session_scope", _fake_session_scope), \
             patch(f"{_CRUD}.templates_service.get_by_id",
                   AsyncMock(return_value=_tmpl(tid=5, name="Старое"))), \
             patch(f"{_CRUD}.templates_service.rename", rename), \
             patch(f"{_CRUD}.operators_service.write_audit", write_audit):
            consumed = await bt._step_rename(
                event, 7, state, "Новое", op_id=None
            )
        assert consumed is True
        rename.assert_awaited_once()
        write_audit.assert_not_awaited()
        assert 7 not in bt._wizards

    @pytest.mark.asyncio
    async def test_save_edit_no_operator_updates_skips_audit(self) -> None:
        """update_text выполняется, но при op_id=None write_audit
        пропускается (ветка 483->495)."""
        event = _make_event(user_id=7)
        st = bt._TmplWizardState(
            step="edit_preview", target_id=5,
            pending_text="новый текст", pending_name="Док",
            pending_attachments=[],
        )
        st._edit_image_replaced = False  # type: ignore[attr-defined]
        bt._wizards[7] = st
        update = AsyncMock()
        write_audit = AsyncMock()
        with patch(f"{_WIZ}.get_operator", AsyncMock(return_value=None)), \
             patch(f"{_WIZ}.session_scope", _fake_session_scope), \
             patch(f"{_WIZ}.templates_service.update_text", update), \
             patch(f"{_WIZ}.operators_service.write_audit", write_audit), \
             patch(f"{_WIZ}.send_or_edit_screen", AsyncMock()):
            await bt._save_edit(event, 5)
        update.assert_awaited_once()
        write_audit.assert_not_awaited()
        assert 7 not in bt._wizards


# ══════════════════════════════════════════════════════════════════════
# 10. handle_callback — fallthrough `":" not in rest` (неизвестный
#     exact-verb без id → итоговый return False, ветка 162->205).
# ══════════════════════════════════════════════════════════════════════


class TestHandleCallbackFallthrough:
    @pytest.mark.asyncio
    async def test_unknown_exact_no_colon_returns_false(self) -> None:
        """`op:tmpl:wat` — не совпал ни с одним exact-вариантом и в rest
        нет ':' → блок verb:id пропущен, финальный return False."""
        event = _make_event(user_id=7)
        with patch(f"{_BT}.is_admin_chat", return_value=True), \
             patch(f"{_BT}.ack_callback", AsyncMock()):
            result = await bt.handle_callback(event, "op:tmpl:wat")
        assert result is False


# ══════════════════════════════════════════════════════════════════════
# 11. handle_wizard_text — гард не-админ-чата (строка 218). Существующий
#     handler-тест проверяет этот гард только для handle_callback; для
#     handle_wizard_text — закрываем здесь.
# ══════════════════════════════════════════════════════════════════════


class TestWizardTextNonAdminChat:
    @pytest.mark.asyncio
    async def test_non_admin_chat_returns_false(self) -> None:
        """Сообщение пришло НЕ из админ-группы → wizard-перехват
        пропускается (return False), даже если для оператора есть активный
        wizard."""
        event = _make_event(user_id=7)
        bt._wizards[7] = bt._TmplWizardState(step="new_awaiting_name")
        get_user_id = AsyncMock()
        with patch(f"{_BT}.is_admin_chat", return_value=False), \
             patch(f"{_BT}.get_user_id", get_user_id):
            consumed = await bt.handle_wizard_text(event, "текст")
        assert consumed is False
        # До get_user_id не дошли — гард чата самый первый.
        get_user_id.assert_not_called()
