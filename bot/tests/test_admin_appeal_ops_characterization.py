"""Характеризационные тесты для handlers/admin_appeal_ops.

Фиксируют ТЕКУЩЕЕ поведение операций оператора над обращением как
страховочную сетку перед декомпозицией god-object'ов. Прод-код не
меняем — только закрепляем наблюдаемые контракты.

Базовый набор веток уже покрыт в ``test_admin_appeal_ops.py``
(not-operator / not-it / not-found / closed / blocked / happy-path /
race-warning / intermediate-reply hint). Здесь — ДОПОЛНИТЕЛЬНЫЕ ветки,
которых там нет:

- ``_show_appeal_card_or_result`` — ядро «обновление статуса +
  admin-карточка»: happy (render force_new=True), appeal=None,
  user=None, get_by_id_with_messages бросает, render бросает.
- ``run_reopen`` исход ``reopened`` — реально публикует обновлённую
  карточку через admin_card (а не fallback-сообщение).
- ``run_close`` ok → публикует карточку; not-found (ok=False) → текст
  «не найдено», без подсказки про промежуточный ответ.
- ``run_reply_intent`` — operator_id is None (get_user_id→None);
  предупреждение о перезаписи intent падает (event.bot.send_message
  бросает) — handler не валится, intent всё равно ставится.
- ``run_reply_cancel`` — operator_id is None → тихий ack без сообщения.
- ``run_show_attachments`` — not-operator / not-found / happy-path.

Все вызовы service-слоя замоканы; session_scope — через
tests._helpers.fake_session_scope. Локально skip без maxapi, в CI идёт.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


def _make_event(*, chat_id: int = 555, user_id: int = 7) -> SimpleNamespace:
    # Тонкая обёртка над tests/_helpers.make_event — сохраняет файловые
    # дефолты; callback нужен admin-action handler'ам (ack кнопки).
    return make_event(chat_id=chat_id, user_id=user_id, with_callback=True)


# --- _show_appeal_card_or_result ----------------------------------------------


class TestShowAppealCardOrResult:
    """Ядро «обновление статуса и admin-карточки» после оп-действия.

    Sacred event log: при наличии appeal+user карточка ВСЕГДА
    публикуется новой (force_new=True), иначе fallback-сообщение.
    """

    @pytest.mark.asyncio
    async def test_renders_card_force_new_when_appeal_and_user_present(self) -> None:
        """Фиксирует: appeal найден и user не None → admin_card.render
        вызывается с force_new=True, fallback-сообщение НЕ шлётся."""
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        appeal = SimpleNamespace(id=5, user=SimpleNamespace(max_user_id=42))
        render = AsyncMock(return_value="card-mid")
        send_screen = AsyncMock()
        with patch(
            "aemr_bot.handlers.admin_appeal_ops.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service."
            "get_by_id_with_messages",
            AsyncMock(return_value=appeal),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.admin_card_service.render",
            render,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.send_or_edit_screen",
            send_screen,
        ):
            await admin_appeal_ops._show_appeal_card_or_result(
                event, 5, "fallback текст"
            )
        render.assert_awaited_once()
        # force_new=True — sacred-инвариант event log.
        assert render.await_args.kwargs.get("force_new") is True
        # первым позиционным идёт event.bot, вторым — сам appeal
        assert render.await_args.args[0] is event.bot
        assert render.await_args.args[1] is appeal
        # fallback не использован — карточка перекрыла его
        send_screen.assert_not_called()

    @pytest.mark.asyncio
    async def test_appeal_none_falls_back_to_message(self) -> None:
        """Фиксирует: appeal не найден (None) → render не зовём,
        шлём fallback-сообщение оператору force_new_message=True."""
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        render = AsyncMock()
        send_screen = AsyncMock()
        with patch(
            "aemr_bot.handlers.admin_appeal_ops.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service."
            "get_by_id_with_messages",
            AsyncMock(return_value=None),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.admin_card_service.render",
            render,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.send_or_edit_screen",
            send_screen,
        ):
            await admin_appeal_ops._show_appeal_card_or_result(
                event, 5, "сообщение-fallback"
            )
        render.assert_not_called()
        send_screen.assert_awaited_once()
        assert send_screen.await_args.kwargs.get("text") == "сообщение-fallback"
        assert send_screen.await_args.kwargs.get("force_new_message") is True

    @pytest.mark.asyncio
    async def test_user_none_falls_back_to_message(self) -> None:
        """Фиксирует: appeal есть, но appeal.user is None → карточку
        не рисуем (нечего показать жителю), уходим в fallback."""
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        appeal = SimpleNamespace(id=5, user=None)
        render = AsyncMock()
        send_screen = AsyncMock()
        with patch(
            "aemr_bot.handlers.admin_appeal_ops.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service."
            "get_by_id_with_messages",
            AsyncMock(return_value=appeal),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.admin_card_service.render",
            render,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.send_or_edit_screen",
            send_screen,
        ):
            await admin_appeal_ops._show_appeal_card_or_result(
                event, 5, "fallback-user-none"
            )
        render.assert_not_called()
        send_screen.assert_awaited_once()
        assert send_screen.await_args.kwargs.get("text") == "fallback-user-none"

    @pytest.mark.asyncio
    async def test_get_by_id_raises_swallowed_then_fallback(self) -> None:
        """Фиксирует: get_by_id_with_messages бросает → исключение
        проглатывается (appeal=None), handler не падает, идёт fallback."""
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        send_screen = AsyncMock()
        with patch(
            "aemr_bot.handlers.admin_appeal_ops.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service."
            "get_by_id_with_messages",
            AsyncMock(side_effect=RuntimeError("db down")),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.send_or_edit_screen",
            send_screen,
        ):
            # Не должно бросить наружу.
            await admin_appeal_ops._show_appeal_card_or_result(
                event, 5, "fallback-после-сбоя"
            )
        send_screen.assert_awaited_once()
        assert send_screen.await_args.kwargs.get("text") == "fallback-после-сбоя"

    @pytest.mark.asyncio
    async def test_render_raises_swallowed_then_fallback(self) -> None:
        """Фиксирует: render() бросает → исключение проглатывается, после
        чего handler уходит в fallback-сообщение (не падает)."""
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        appeal = SimpleNamespace(id=5, user=SimpleNamespace(max_user_id=42))
        send_screen = AsyncMock()
        with patch(
            "aemr_bot.handlers.admin_appeal_ops.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service."
            "get_by_id_with_messages",
            AsyncMock(return_value=appeal),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.admin_card_service.render",
            AsyncMock(side_effect=RuntimeError("MAX down")),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.send_or_edit_screen",
            send_screen,
        ):
            await admin_appeal_ops._show_appeal_card_or_result(
                event, 5, "fallback-render-упал"
            )
        # render упал, но handler доехал до fallback.
        send_screen.assert_awaited_once()
        assert send_screen.await_args.kwargs.get("text") == "fallback-render-упал"


# --- run_reopen: исход reopened публикует карточку ----------------------------


class TestRunReopenRendersCard:
    @pytest.mark.asyncio
    async def test_reopened_publishes_updated_card(self) -> None:
        """Реоткрытие закрытого: result='reopened' → обновлённая
        admin-карточка публикуется через admin_card.render (force_new),
        а не просто текст-fallback. Audit пишется."""
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        appeal = SimpleNamespace(id=5, user=SimpleNamespace(max_user_id=42))
        render = AsyncMock(return_value="card-mid")
        write_audit = AsyncMock()
        with patch(
            "aemr_bot.handlers.admin_appeal_ops.ensure_operator",
            AsyncMock(return_value=True),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service.reopen",
            AsyncMock(return_value="reopened"),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
            write_audit,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service."
            "get_by_id_with_messages",
            AsyncMock(return_value=appeal),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.admin_card_service.render",
            render,
        ), patch(
            "aemr_bot.utils.event.ack_callback", AsyncMock()
        ):
            await admin_appeal_ops.run_reopen(event, 5)
        write_audit.assert_awaited_once()
        assert write_audit.await_args.kwargs.get("action") == "reopen"
        # На «reopened» карточка перерисовывается (sacred force_new).
        render.assert_awaited_once()
        assert render.await_args.kwargs.get("force_new") is True


# --- run_close: рендер карточки + ветка not-found -----------------------------


class TestRunCloseBranches:
    @pytest.mark.asyncio
    async def test_close_ok_publishes_card(self) -> None:
        """close ok=True → admin-карточка с актуальным CLOSED-статусом
        публикуется (force_new). Без intermediate-reply подсказки про
        «Ответить и закрыть» в fallback нет (но карточка перекрывает
        fallback в принципе)."""
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        appeal = SimpleNamespace(id=5, user=SimpleNamespace(max_user_id=42))
        render = AsyncMock(return_value="card-mid")
        with patch(
            "aemr_bot.handlers.admin_appeal_ops.ensure_operator",
            AsyncMock(return_value=True),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service."
            "has_operator_message",
            AsyncMock(return_value=False),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service.close",
            AsyncMock(return_value=True),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
            AsyncMock(),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service."
            "get_by_id_with_messages",
            AsyncMock(return_value=appeal),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.admin_card_service.render",
            render,
        ), patch(
            "aemr_bot.utils.event.ack_callback", AsyncMock()
        ):
            await admin_appeal_ops.run_close(event, 5)
        render.assert_awaited_once()
        assert render.await_args.kwargs.get("force_new") is True

    @pytest.mark.asyncio
    async def test_close_not_found_uses_not_found_text_no_hint(self) -> None:
        """close ok=False (обращение не найдено) → fallback-текст
        «#N не найдено», без подсказки про промежуточный ответ; audit
        НЕ пишется. Карточку не рисуем (appeal=None в card-render)."""
        from aemr_bot import texts
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        write_audit = AsyncMock()
        send_screen = AsyncMock()
        with patch(
            "aemr_bot.handlers.admin_appeal_ops.ensure_operator",
            AsyncMock(return_value=True),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service."
            "has_operator_message",
            AsyncMock(return_value=False),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service.close",
            AsyncMock(return_value=False),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
            write_audit,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service."
            "get_by_id_with_messages",
            AsyncMock(return_value=None),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.send_or_edit_screen",
            send_screen,
        ), patch(
            "aemr_bot.utils.event.ack_callback", AsyncMock()
        ):
            await admin_appeal_ops.run_close(event, 999)
        # ok=False → audit не пишем.
        write_audit.assert_not_awaited()
        # fallback-сообщение = NOT_FOUND, без warning про промежуточный.
        send_screen.assert_awaited_once()
        sent = send_screen.await_args.kwargs.get("text", "")
        assert sent == texts.OP_APPEAL_NOT_FOUND.format(number=999)
        assert "промежуточный ответ" not in sent.lower()

    @pytest.mark.asyncio
    async def test_close_audit_details_none_when_no_intermediate(self) -> None:
        """close ok=True без промежуточного ответа: audit пишется с
        details=None (а не {'after_intermediate_reply': True})."""
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        write_audit = AsyncMock()
        with patch(
            "aemr_bot.handlers.admin_appeal_ops.ensure_operator",
            AsyncMock(return_value=True),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service."
            "has_operator_message",
            AsyncMock(return_value=False),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service.close",
            AsyncMock(return_value=True),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
            write_audit,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service."
            "get_by_id_with_messages",
            AsyncMock(return_value=None),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.send_or_edit_screen",
            AsyncMock(),
        ), patch(
            "aemr_bot.utils.event.ack_callback", AsyncMock()
        ):
            await admin_appeal_ops.run_close(event, 5)
        write_audit.assert_awaited_once()
        assert write_audit.await_args.kwargs.get("action") == "close"
        assert write_audit.await_args.kwargs.get("details") is None


# --- run_reply_intent: дополнительные ветки -----------------------------------


class TestRunReplyIntentEdges:
    @pytest.mark.asyncio
    async def test_no_operator_id_silently_returns(self) -> None:
        """Фиксирует: автор-чат админский и оператор валиден, но
        get_user_id вернул None (нет sender id) → тихий ack, БД не
        трогаем, сообщение-подсказку не шлём."""
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        get_by_id = AsyncMock()
        with patch(
            "aemr_bot.handlers.admin_appeal_ops.is_admin_chat",
            return_value=True,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.ensure_operator",
            AsyncMock(return_value=True),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.get_user_id",
            return_value=None,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service.get_by_id",
            get_by_id,
        ), patch(
            "aemr_bot.utils.event.ack_callback", AsyncMock()
        ):
            await admin_appeal_ops.run_reply_intent(event, 5)
        # До работы с обращением не дошли.
        get_by_id.assert_not_called()
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_overwrite_warning_send_failure_does_not_break(self) -> None:
        """P2 #22 robustness: если попытка предупредить о перезаписи
        intent (event.bot.send_message) бросает — handler глотает это и
        всё равно ставит новый reply-intent + шлёт prompt-подсказку."""
        from aemr_bot.db.models import AppealStatus
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        # send_message на warning бросает; но prompt идёт через
        # send_or_edit_screen, который мы мокаем отдельно — он должен
        # отработать. remember_reply_intent должен быть вызван.
        event.bot.send_message = AsyncMock(side_effect=RuntimeError("MAX 429"))
        appeal = SimpleNamespace(
            id=20,
            status=AppealStatus.NEW.value,
            user=SimpleNamespace(is_blocked=False),
        )
        remember = MagicMock()
        send_screen = AsyncMock()
        with patch(
            "aemr_bot.handlers.admin_appeal_ops.is_admin_chat",
            return_value=True,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.ensure_operator",
            AsyncMock(return_value=True),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.get_user_id",
            return_value=7,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service.get_by_id",
            AsyncMock(return_value=appeal),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service."
            "mark_in_progress",
            AsyncMock(return_value=True),
        ), patch(
            "aemr_bot.handlers.operator_reply.remember_reply_intent",
            remember,
        ), patch(
            "aemr_bot.services.wizard_registry.get_reply_intent",
            # existing intent на ДРУГОЕ обращение → ветка warning
            return_value=(11, True, 1.0),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.send_or_edit_screen",
            send_screen,
        ), patch(
            "aemr_bot.utils.event.ack_callback", AsyncMock()
        ):
            # Несмотря на брошенный warning-send, не падаем.
            await admin_appeal_ops.run_reply_intent(event, 20)
        # warning попытались отправить (и он бросил)
        event.bot.send_message.assert_awaited()
        # но intent всё равно поставлен
        remember.assert_called_once()
        # и prompt-подсказка ушла через send_or_edit_screen (force_new)
        send_screen.assert_awaited_once()
        assert send_screen.await_args.kwargs.get("force_new_message") is True


# --- run_reply_cancel: operator_id is None ------------------------------------


class TestRunReplyCancelEdges:
    @pytest.mark.asyncio
    async def test_no_operator_id_acks_without_message(self) -> None:
        """Фиксирует: get_user_id вернул None → тихий ack, drop-intent
        не зовём, сообщение об отмене не шлём."""
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        drop = MagicMock()
        with patch(
            "aemr_bot.handlers.admin_appeal_ops.get_user_id",
            return_value=None,
        ), patch(
            "aemr_bot.handlers.operator_reply.drop_reply_intent", drop
        ), patch(
            "aemr_bot.utils.event.ack_callback", AsyncMock()
        ):
            await admin_appeal_ops.run_reply_cancel(event)
        drop.assert_not_called()
        event.bot.send_message.assert_not_called()


# --- run_show_attachments -----------------------------------------------------


class TestRunShowAttachments:
    @pytest.mark.asyncio
    async def test_not_operator_returns_without_relay(self) -> None:
        """Фиксирует: не оператор → ack и выход, вложения не
        переотправляются."""
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        relay = AsyncMock()
        with patch(
            "aemr_bot.handlers.admin_appeal_ops.ensure_operator",
            AsyncMock(return_value=False),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.admin_relay."
            "render_appeal_attachments",
            relay,
        ), patch(
            "aemr_bot.utils.event.ack_callback", AsyncMock()
        ):
            await admin_appeal_ops.run_show_attachments(event, 5)
        relay.assert_not_called()
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_appeal_not_found_message_no_relay(self) -> None:
        """Фиксирует: обращение не найдено → текст «#N не найдено», relay
        вложений НЕ запускается (нечего показывать)."""
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        relay = AsyncMock()
        send_screen = AsyncMock()
        with patch(
            "aemr_bot.handlers.admin_appeal_ops.ensure_operator",
            AsyncMock(return_value=True),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service."
            "get_by_id_with_messages",
            AsyncMock(return_value=None),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.admin_relay."
            "render_appeal_attachments",
            relay,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.send_or_edit_screen",
            send_screen,
        ), patch(
            "aemr_bot.utils.event.ack_callback", AsyncMock()
        ):
            await admin_appeal_ops.run_show_attachments(event, 999)
        relay.assert_not_called()
        send_screen.assert_awaited_once()
        text = send_screen.await_args.kwargs.get("text", "")
        assert "999" in text or "не найдено" in text.lower()

    @pytest.mark.asyncio
    async def test_happy_path_renders_attachments(self) -> None:
        """Фиксирует happy-path: обращение найдено → render_appeal_
        attachments вызван с шапкой #{appeal_id} и reply_to_mid из
        appeal.admin_message_id."""
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        appeal = SimpleNamespace(
            id=5,
            admin_message_id="adm-77",
            user=SimpleNamespace(max_user_id=42),
        )
        relay = AsyncMock()
        with patch(
            "aemr_bot.handlers.admin_appeal_ops.ensure_operator",
            AsyncMock(return_value=True),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.appeals_service."
            "get_by_id_with_messages",
            AsyncMock(return_value=appeal),
        ), patch(
            "aemr_bot.handlers.admin_appeal_ops.admin_relay."
            "render_appeal_attachments",
            relay,
        ), patch(
            "aemr_bot.utils.event.ack_callback", AsyncMock()
        ):
            await admin_appeal_ops.run_show_attachments(event, 5)
        relay.assert_awaited_once()
        kwargs = relay.await_args.kwargs
        # appeal проброшен как есть
        assert kwargs.get("appeal") is appeal
        # reply_to_mid взят из admin_message_id обращения
        assert kwargs.get("reply_to_mid") == "adm-77"
        # шапка содержит плейсхолдер под номер обращения
        assert "{appeal_id}" in kwargs.get("header_template", "")
