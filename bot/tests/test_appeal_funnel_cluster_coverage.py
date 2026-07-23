"""Прицельное покрытие непокрытых веток кластера воронки обращения.

Кластер: handlers/appeal_funnel.py, handlers/menu.py, handlers/start.py,
handlers/appeal.py — воронка приёма обращения, FSM-переходы, ветки меню,
гард /cancel и admin-vs-citizen разводка команд.

Дополняет (НЕ дублирует) существующие:
- test_handlers_funnel.py / test_handlers_appeal_funnel.py
- test_handlers_menu.py / test_handlers_menu_extra.py
- test_handlers_start.py / test_appeal_dispatcher.py

Цель — реальные ветки, по которым прежде не было ни одного теста:
* start_appeal_flow: запрос согласия (URL / PDF-token / конфиг-стоп)
* ask_contact_or_skip: три target_state (нет телефона / нет имени / готов)
* on_awaiting_contact: телефон из текста + извлечение имени из контакта
* on_awaiting_summary: happy-path (текст+вложение → finalize)
* finalize_appeal: rate-limited и пустой ввод
* on_awaiting_followup_text: rate-limit (min-interval, max-per-hour)
* menu.start_appeal_followup / start_appeal_repeat (CLOSED) / show_appeal
  happy-path; do_subscribe_confirm для заблокированного; show_appeal_attachments
* start.register(): bot_started/bot_stopped + admin-vs-citizen гарды команд
* appeal.register().on_message: диспетчеризация в state-handler
* appeal._ensure_funnel_callback_state: geo-кнопка в шаге адреса

Мокаем БД через SimpleNamespace + tests/_helpers (fake_session_scope,
fake_current_user, make_event) — паттерн существующих handler-тестов.

Локально skip без maxapi; в CI работает.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._helpers import fake_current_user
from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


def _funnel_event(
    *, chat_id: int = 100, user_id: int = 42, text: str = ""
) -> SimpleNamespace:
    """Событие для appeal_funnel: воронка зовёт bot.send_message И
    bot.edit_message (через progress-карту), send_message должен вернуть
    SendedMessage-like для extract_message_id."""
    return make_event(
        chat_id=chat_id, user_id=user_id, text=text, first_name="Иван",
        with_user=True, with_edit_message=True, send_returns_mid=True,
    )


# ============================================================================
# appeal_funnel.start_appeal_flow — запрос согласия (ветки 95-155)
# ============================================================================


class TestStartAppealFlowConsent:
    @pytest.mark.asyncio
    async def test_no_consent_with_policy_url_sends_consent_keyboard(self) -> None:
        """Нет consent_pdn_at + есть policy_url (без PDF-token) → запрос
        согласия с текстом из settings_store.get_consent_request_text."""
        from aemr_bot.handlers import appeal_funnel

        event = _funnel_event()
        user = SimpleNamespace(is_blocked=False, consent_pdn_at=None, id=1)
        set_state = AsyncMock()
        # settings_store.get: "policy_url" → URL, "policy_pdf_token" → None.
        get_mock = AsyncMock(side_effect=["https://policy.example/p", None])
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.count_recent_for_user",
                   AsyncMock(return_value=0)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.set_state",
                   set_state), \
             patch("aemr_bot.handlers.appeal_funnel.settings_store.get", get_mock), \
             patch("aemr_bot.handlers.appeal_funnel.settings_store.get_consent_request_text",
                   AsyncMock(return_value="Дайте согласие на обработку ПДн")):
            await appeal_funnel.start_appeal_flow(event, max_user_id=42)

        # Запрошено согласие: state переведён в AWAITING_CONSENT.
        from aemr_bot.db.models import DialogState
        set_state.assert_called_once()
        assert set_state.call_args.args[2] == DialogState.AWAITING_CONSENT
        event.bot.send_message.assert_called()
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "согласие" in text.lower()

    @pytest.mark.asyncio
    async def test_no_consent_with_pdf_token_attaches_file(self) -> None:
        """Нет согласия + есть policy_pdf_token → PDF прикладывается к
        запросу согласия через build_file_attachment, а сам текст согласия
        по-прежнему берётся из настроек.

        Регресс: раньше ветка с PDF подменяла текст заглушкой «Полный
        текст политики — в прикреплённом PDF», и consent_text не читался
        вовсе. На проде токен есть всегда, поэтому жила только заглушка —
        житель давал согласие, не видя ни перечня данных, ни целей
        (152-ФЗ ст. 9 ч. 1).
        """
        from aemr_bot.handlers import appeal_funnel

        event = _funnel_event()
        user = SimpleNamespace(is_blocked=False, consent_pdn_at=None, id=1)
        # 3-й вызов — внутренний get("consent_text") внутри
        # get_consent_request_text: патч settings_store.get ловит и его.
        # None → используется fallback texts.CONSENT_REQUEST.
        get_mock = AsyncMock(
            side_effect=["https://policy.example/p", "TOK-PDF", None]
        )
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.count_recent_for_user",
                   AsyncMock(return_value=0)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.set_state",
                   AsyncMock()), \
             patch("aemr_bot.handlers.appeal_funnel.settings_store.get", get_mock), \
             patch("aemr_bot.services.policy.build_file_attachment",
                   MagicMock(return_value={"type": "file", "token": "TOK-PDF"})) as bfa:
            await appeal_funnel.start_appeal_flow(event, max_user_id=42)

        bfa.assert_called_once_with("TOK-PDF")
        attachments = event.bot.send_message.call_args.kwargs.get("attachments")
        # PDF-вложение вставлено первым в список аттачей.
        assert {"type": "file", "token": "TOK-PDF"} in attachments
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        # Якорь против возврата заглушки: в тексте обязан быть перечень
        # обрабатываемых данных и ссылка на политику, а не одна отсылка к
        # вложению. Проверяем по существу, а не по вхождению слова «PDF».
        assert "телефон" in text
        assert "https://policy.example/p" in text

    @pytest.mark.asyncio
    async def test_pdf_without_policy_url_does_not_leak_none(self) -> None:
        """policy_url стёрли, PDF живой → в тексте не должно быть «None».

        consent_text — шаблон с обязательным {policy_url}; подставить
        нечего, поэтому вместо ссылки идёт отсылка к вложению. Без этого
        жителю уехало бы «Полная политика — None».
        """
        from aemr_bot.handlers import appeal_funnel

        event = _funnel_event()
        user = SimpleNamespace(is_blocked=False, consent_pdn_at=None, id=1)
        get_mock = AsyncMock(side_effect=[None, "TOK-PDF", None])
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.count_recent_for_user",
                   AsyncMock(return_value=0)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.set_state",
                   AsyncMock()), \
             patch("aemr_bot.handlers.appeal_funnel.settings_store.get", get_mock), \
             patch("aemr_bot.services.policy.build_file_attachment",
                   MagicMock(return_value={"type": "file", "token": "TOK-PDF"})):
            await appeal_funnel.start_appeal_flow(event, max_user_id=42)

        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "None" not in text
        assert "в прикреплённом файле" in text
        # Перечень данных на месте — согласие осталось информированным.
        assert "телефон" in text

    @pytest.mark.asyncio
    async def test_no_consent_no_policy_config_stop(self) -> None:
        """Нет согласия И ни URL, ни PDF-token — конфигурационный сбой:
        воронка останавливается, жителю «сервис недоступен», шаг согласия
        НЕ пропускается (152-ФЗ)."""
        from aemr_bot.handlers import appeal_funnel

        event = _funnel_event()
        user = SimpleNamespace(is_blocked=False, consent_pdn_at=None, id=1)
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.count_recent_for_user",
                   AsyncMock(return_value=0)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.set_state",
                   AsyncMock()), \
             patch("aemr_bot.handlers.appeal_funnel.settings_store.get",
                   AsyncMock(return_value=None)):
            await appeal_funnel.start_appeal_flow(event, max_user_id=42)

        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "недоступ" in text.lower()
        assert "политик" in text.lower()

    @pytest.mark.asyncio
    async def test_has_consent_proceeds_to_contact(self) -> None:
        """Согласие уже есть → воронка не запрашивает его повторно, идёт
        сразу в ask_contact_or_skip."""
        from aemr_bot.handlers import appeal_funnel

        event = _funnel_event()
        user = SimpleNamespace(
            is_blocked=False,
            consent_pdn_at=datetime.now(timezone.utc),
            id=1,
        )
        ask_contact = AsyncMock()
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.count_recent_for_user",
                   AsyncMock(return_value=0)), \
             patch("aemr_bot.handlers.appeal_funnel.ask_contact_or_skip",
                   ask_contact):
            await appeal_funnel.start_appeal_flow(event, max_user_id=42)

        ask_contact.assert_called_once()


# ============================================================================
# appeal_funnel.ask_contact_or_skip — три target_state (195-229)
# ============================================================================


class TestAskContactOrSkip:
    @pytest.mark.asyncio
    async def test_no_phone_asks_contact(self) -> None:
        """Нет телефона → AWAITING_CONTACT, показываем contact-клавиатуру."""
        from aemr_bot.db.models import DialogState
        from aemr_bot.handlers import appeal_funnel

        event = _funnel_event()
        user = SimpleNamespace(phone=None, first_name="Иван")
        set_state = AsyncMock()
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.set_state",
                   set_state):
            await appeal_funnel.ask_contact_or_skip(event, max_user_id=42)

        assert set_state.call_args.args[2] == DialogState.AWAITING_CONTACT
        from aemr_bot import texts
        assert event.bot.send_message.call_args.kwargs.get("text") == texts.CONTACT_REQUEST

    @pytest.mark.asyncio
    async def test_phone_but_no_name_asks_name(self) -> None:
        """Есть телефон, имя «Удалено» → AWAITING_NAME."""
        from aemr_bot.db.models import DialogState
        from aemr_bot.handlers import appeal_funnel

        event = _funnel_event()
        user = SimpleNamespace(phone="+79991234567", first_name="Удалено")
        set_state = AsyncMock()
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.set_state",
                   set_state):
            await appeal_funnel.ask_contact_or_skip(event, max_user_id=42)

        assert set_state.call_args.args[2] == DialogState.AWAITING_NAME
        from aemr_bot import texts
        assert event.bot.send_message.call_args.kwargs.get("text") == texts.CONTACT_RECEIVED

    @pytest.mark.asyncio
    async def test_phone_and_name_goes_to_locality_via_reuse(self) -> None:
        """Есть и телефон, и имя → AWAITING_LOCALITY. Если ask_address_or_reuse
        показал reuse-prompt (вернул True), ask_locality НЕ зовётся."""
        from aemr_bot.db.models import DialogState
        from aemr_bot.handlers import appeal_funnel

        event = _funnel_event()
        user = SimpleNamespace(phone="+79991234567", first_name="Иван")
        set_state = AsyncMock()
        reuse = AsyncMock(return_value=True)
        ask_locality = AsyncMock()
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.set_state",
                   set_state), \
             patch("aemr_bot.handlers.appeal_funnel.ask_address_or_reuse", reuse), \
             patch("aemr_bot.handlers.appeal_funnel.ask_locality", ask_locality):
            await appeal_funnel.ask_contact_or_skip(event, max_user_id=42)

        assert set_state.call_args.args[2] == DialogState.AWAITING_LOCALITY
        reuse.assert_called_once()
        ask_locality.assert_not_called()

    @pytest.mark.asyncio
    async def test_phone_and_name_no_reuse_falls_to_locality(self) -> None:
        """Готов, но прошлого адреса нет (reuse=False) → ask_locality."""
        from aemr_bot.handlers import appeal_funnel

        event = _funnel_event()
        user = SimpleNamespace(phone="+79991234567", first_name="Иван")
        ask_locality = AsyncMock()
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.set_state",
                   AsyncMock()), \
             patch("aemr_bot.handlers.appeal_funnel.ask_address_or_reuse",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.appeal_funnel.ask_locality", ask_locality):
            await appeal_funnel.ask_contact_or_skip(event, max_user_id=42)

        ask_locality.assert_called_once()


# ============================================================================
# appeal_funnel.on_awaiting_contact — телефон из текста + имя из контакта
# ============================================================================


class TestOnAwaitingContact:
    @pytest.mark.asyncio
    async def test_no_phone_anywhere_retries(self) -> None:
        """Ни в contact-вложении, ни в тексте нет телефона → CONTACT_RETRY."""
        from aemr_bot.handlers import appeal_funnel

        event = _funnel_event()
        with patch("aemr_bot.handlers.appeal_funnel.extract_phone",
                   return_value=None):
            await appeal_funnel.on_awaiting_contact(
                event, body=None, text_body="привет без цифр", max_user_id=42
            )
        from aemr_bot import texts
        event.message.answer.assert_called_once()
        assert event.message.answer.call_args.args[0] == texts.CONTACT_RETRY

    @pytest.mark.asyncio
    async def test_phone_from_text_and_name_from_contact_proceeds(self) -> None:
        """Телефон напечатан текстом (digits-fallback), имя пришло из
        contact-вложения → set_phone + set_first_name + ask_contact_or_skip."""
        from aemr_bot.handlers import appeal_funnel

        event = _funnel_event()
        # У жителя ещё нет имени → contact_name приклеится.
        user = SimpleNamespace(first_name="Удалено")
        set_phone = AsyncMock()
        set_first_name = AsyncMock()
        ask_contact = AsyncMock()
        with patch("aemr_bot.handlers.appeal_funnel.extract_phone",
                   return_value=None), \
             patch("aemr_bot.handlers.appeal_funnel.extract_contact_name",
                   return_value="Пётр Иванов"), \
             patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.set_phone",
                   set_phone), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.set_first_name",
                   set_first_name), \
             patch("aemr_bot.handlers.appeal_funnel.ask_contact_or_skip",
                   ask_contact):
            await appeal_funnel.on_awaiting_contact(
                event,
                body=SimpleNamespace(),
                text_body="Мой номер +7 999 123-45-67",
                max_user_id=42,
            )

        # Телефон извлечён из текста цифрами и сохранён.
        set_phone.assert_called_once()
        saved_phone = set_phone.call_args.args[2]
        assert "999" in saved_phone
        # Имя из контакта приклеено (есть буквы, was «Удалено»).
        set_first_name.assert_called_once()
        # После заполнения имени идём дальше force_new_message=True.
        ask_contact.assert_called_once()
        assert ask_contact.call_args.kwargs.get("force_new_message") is True

    @pytest.mark.asyncio
    async def test_phone_ok_but_name_still_empty_asks_name(self) -> None:
        """Телефон есть, но имени так и нет (contact без имени) →
        AWAITING_NAME + CONTACT_RECEIVED."""
        from aemr_bot.db.models import DialogState
        from aemr_bot.handlers import appeal_funnel

        event = _funnel_event()
        user = SimpleNamespace(first_name="Удалено")
        set_state = AsyncMock()
        with patch("aemr_bot.handlers.appeal_funnel.extract_phone",
                   return_value="+79991234567"), \
             patch("aemr_bot.handlers.appeal_funnel.extract_contact_name",
                   return_value=None), \
             patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.set_phone",
                   AsyncMock()), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.set_state",
                   set_state):
            await appeal_funnel.on_awaiting_contact(
                event, body=SimpleNamespace(), text_body="", max_user_id=42
            )

        assert set_state.call_args.args[2] == DialogState.AWAITING_NAME
        from aemr_bot import texts
        assert event.message.answer.call_args.args[0] == texts.CONTACT_RECEIVED


# ============================================================================
# appeal_funnel.on_awaiting_summary — happy-path (502-519) + finalize
# ============================================================================


class TestOnAwaitingSummaryHappy:
    @pytest.mark.asyncio
    async def test_text_and_attachment_appended_then_finalize(self) -> None:
        """Непустой текст + вложение → оба добавлены в dialog_data,
        вызван finalize_appeal."""
        from aemr_bot.handlers import appeal_funnel

        event = _funnel_event()
        session = AsyncMock()
        user = SimpleNamespace(dialog_data={})
        finalize = AsyncMock()
        with patch("aemr_bot.handlers.appeal_funnel.collect_attachments",
                   return_value=[{"type": "image", "token": "img1"}]), \
             patch("aemr_bot.handlers.appeal_funnel.current_user",
                   fake_current_user(user, session=session)), \
             patch("aemr_bot.handlers.appeal_funnel.finalize_appeal", finalize):
            await appeal_funnel.on_awaiting_summary(
                event, body=SimpleNamespace(), text_body="Яма во дворе",
                max_user_id=42,
            )

        # dialog_data обновлён обоими накоплениями.
        assert user.dialog_data["summary_chunks"] == ["Яма во дворе"]
        assert user.dialog_data["attachments"] == [{"type": "image", "token": "img1"}]
        session.flush.assert_awaited()
        finalize.assert_called_once()


class TestFinalizeAppeal:
    @pytest.mark.asyncio
    async def test_rate_limited_sends_limit_message(self) -> None:
        """persist вернул PERSIST_RATE_LIMITED → жителю сообщение про лимит."""
        from aemr_bot.handlers import appeal_funnel
        from aemr_bot.handlers.appeal_runtime import PERSIST_RATE_LIMITED

        event = _funnel_event()
        user = SimpleNamespace(id=1)
        with patch("aemr_bot.handlers.appeal_funnel.persist_and_dispatch_appeal",
                   AsyncMock(return_value=PERSIST_RATE_LIMITED)), \
             patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.find_active_for_user",
                   AsyncMock(return_value=SimpleNamespace(id=7))), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.earliest_recent_for_user",
                   AsyncMock(return_value=None)):
            await appeal_funnel.finalize_appeal(event, max_user_id=42)

        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "лимит" in text.lower()

    @pytest.mark.asyncio
    async def test_rate_limited_no_open_shows_reset_minutes(self) -> None:
        """Нет открытого обращения → сообщение содержит время до сброса
        лимита (было «дождитесь сброса» без указания сколько ждать)."""
        from datetime import datetime, timedelta, timezone
        from aemr_bot.handlers import appeal_funnel
        from aemr_bot.handlers.appeal_runtime import PERSIST_RATE_LIMITED

        event = _funnel_event()
        user = SimpleNamespace(id=1)
        # Самое старое обращение создано 40 минут назад → слот освободится
        # примерно через 20 минут.
        earliest = datetime.now(timezone.utc) - timedelta(minutes=40)
        with patch("aemr_bot.handlers.appeal_funnel.persist_and_dispatch_appeal",
                   AsyncMock(return_value=PERSIST_RATE_LIMITED)), \
             patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.find_active_for_user",
                   AsyncMock(return_value=None)), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.earliest_recent_for_user",
                   AsyncMock(return_value=earliest)):
            await appeal_funnel.finalize_appeal(event, max_user_id=42)

        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "освободится примерно через" in text
        assert "минут" in text

    @pytest.mark.asyncio
    async def test_empty_rejected_sends_hint(self) -> None:
        """persist вернул False (пустое обращение) → APPEAL_EMPTY_REJECTED."""
        from aemr_bot.handlers import appeal_funnel

        event = _funnel_event()
        with patch("aemr_bot.handlers.appeal_funnel.persist_and_dispatch_appeal",
                   AsyncMock(return_value=False)):
            await appeal_funnel.finalize_appeal(event, max_user_id=42)

        from aemr_bot import texts
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert text == texts.APPEAL_EMPTY_REJECTED

    @pytest.mark.asyncio
    async def test_persist_success_sends_nothing_extra(self) -> None:
        """persist вернул что-то truthy (успех) — finalize не шлёт ни
        rate-limit, ни empty-rejected (их шлёт persist-цепочка)."""
        from aemr_bot.handlers import appeal_funnel

        event = _funnel_event()
        with patch("aemr_bot.handlers.appeal_funnel.persist_and_dispatch_appeal",
                   AsyncMock(return_value=True)):
            await appeal_funnel.finalize_appeal(event, max_user_id=42)

        event.bot.send_message.assert_not_called()


# ============================================================================
# appeal_funnel.on_awaiting_followup_text — rate-limit (638-678)
# ============================================================================


def _followup_user_appeal():
    user = SimpleNamespace(
        id=1,
        dialog_data={"appeal_id": 5},
        consent_pdn_at=datetime.now(timezone.utc),
    )
    appeal = SimpleNamespace(id=5, user_id=1, status="new")
    return user, appeal


class TestFollowupRateLimit:
    @pytest.mark.asyncio
    async def test_min_interval_blocks_without_reset(self) -> None:
        """Дополнение чаще min-interval → отказ «Слишком часто», state НЕ
        сбрасывается (житель может повторить позже)."""
        from aemr_bot.handlers import appeal_funnel

        event = _funnel_event()
        user, appeal = _followup_user_appeal()
        reset = AsyncMock()
        # last_at = только что (1 секунду назад) — меньше дефолтных 30с.
        last_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.reset_state",
                   reset), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.get_by_id",
                   AsyncMock(return_value=appeal)), \
             patch("aemr_bot.handlers.appeal_funnel.collect_attachments",
                   return_value=[]), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.followup_rate_limit_stats",
                   AsyncMock(return_value=(0, last_at))):
            await appeal_funnel.on_awaiting_followup_text(
                event, body=SimpleNamespace(), text_body="ещё деталь",
                max_user_id=42,
            )

        reset.assert_not_called()
        text = event.message.answer.call_args.args[0]
        assert "Слишком часто" in text

    @pytest.mark.asyncio
    async def test_max_per_hour_blocks_and_resets(self) -> None:
        """recent_count >= max-per-hour → отказ + state сбрасывается
        (иначе житель «застрянет» в AWAITING_FOLLOWUP)."""
        from aemr_bot.handlers import appeal_funnel

        event = _funnel_event()
        user, appeal = _followup_user_appeal()
        reset = AsyncMock()
        # last_at=None → min-interval не срабатывает; recent_count=99 ≥ лимит.
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.reset_state",
                   reset), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.get_by_id",
                   AsyncMock(return_value=appeal)), \
             patch("aemr_bot.handlers.appeal_funnel.collect_attachments",
                   return_value=[]), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.followup_rate_limit_stats",
                   AsyncMock(return_value=(99, None))):
            await appeal_funnel.on_awaiting_followup_text(
                event, body=SimpleNamespace(), text_body="ещё деталь",
                max_user_id=42,
            )

        reset.assert_called_once()
        text = event.message.answer.call_args.args[0]
        assert "лимит" in text.lower()

    @pytest.mark.asyncio
    async def test_empty_followup_asks_to_describe(self) -> None:
        """Дополнение без текста и без вложений → просьба описать, без
        обращения к rate-limit."""
        from aemr_bot.handlers import appeal_funnel

        event = _funnel_event()
        user, appeal = _followup_user_appeal()
        rl = AsyncMock()
        with patch("aemr_bot.handlers.appeal_funnel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.appeal_funnel.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.get_by_id",
                   AsyncMock(return_value=appeal)), \
             patch("aemr_bot.handlers.appeal_funnel.collect_attachments",
                   return_value=[]), \
             patch("aemr_bot.handlers.appeal_funnel.appeals_service.followup_rate_limit_stats",
                   rl):
            await appeal_funnel.on_awaiting_followup_text(
                event, body=SimpleNamespace(), text_body="   ", max_user_id=42
            )

        # До rate-limit не дошло.
        rl.assert_not_called()
        text = event.message.answer.call_args.args[0]
        assert "Опишите дополнение" in text


# ============================================================================
# menu.start_appeal_followup / start_appeal_repeat / show_appeal happy-path
# ============================================================================


class TestMenuAppealHappyPaths:
    @pytest.mark.asyncio
    async def test_followup_open_appeal_sets_state(self) -> None:
        """Открытое (NEW) обращение жителя → set_state в AWAITING_FOLLOWUP_TEXT
        с appeal_id в data + просьба описать дополнение."""
        from aemr_bot.db.models import DialogState
        from aemr_bot.handlers import menu

        event = make_event(chat_id=100, user_id=42)
        appeal = SimpleNamespace(
            id=7,
            status="new",
            user=SimpleNamespace(max_user_id=42),
        )
        set_state = AsyncMock()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.appeals_service.get_by_id",
                   AsyncMock(return_value=appeal)), \
             patch("aemr_bot.handlers.menu.users_service.set_state", set_state):
            await menu.start_appeal_followup(event, appeal_id=7, max_user_id=42)

        set_state.assert_called_once()
        assert set_state.call_args.args[2] == DialogState.AWAITING_FOLLOWUP_TEXT
        assert set_state.call_args.kwargs["data"] == {"appeal_id": 7}
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "#7" in text

    @pytest.mark.asyncio
    async def test_repeat_closed_appeal_marks_source_and_context(self) -> None:
        """🔁 Подать похожее по CLOSED → новая воронка с repeat_source_*
        и контекстом «по закрытому вопросу»."""
        from aemr_bot.db.models import AppealStatus, DialogState
        from aemr_bot.handlers import menu

        event = make_event(chat_id=100, user_id=42)
        appeal = SimpleNamespace(
            id=9,
            status=AppealStatus.CLOSED.value,
            locality="Елизовское ГП",
            address="Ленина, 1",
            topic="ЖКХ",
            user=SimpleNamespace(max_user_id=42),
        )
        set_state = AsyncMock()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.appeals_service.get_by_id",
                   AsyncMock(return_value=appeal)), \
             patch("aemr_bot.handlers.menu.users_service.set_state", set_state):
            await menu.start_appeal_repeat(event, appeal_id=9, max_user_id=42)

        set_state.assert_called_once()
        assert set_state.call_args.args[2] == DialogState.AWAITING_SUMMARY
        data = set_state.call_args.kwargs["data"]
        assert data["repeat_source_appeal_id"] == 9
        assert data["repeat_source_status"] == AppealStatus.CLOSED.value
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "закрыт" in text.lower()

    @pytest.mark.asyncio
    async def test_show_appeal_renders_card_with_status(self) -> None:
        """show_appeal happy-path: карточка жителя через card_format.user_card
        + клавиатура по статусу + счётчик вложений."""
        from aemr_bot.handlers import menu

        event = make_event(chat_id=100, user_id=42)
        appeal = SimpleNamespace(
            id=3,
            status="new",
            user=SimpleNamespace(max_user_id=42),
        )
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.appeals_service.get_by_id_with_messages",
                   AsyncMock(return_value=appeal)), \
             patch("aemr_bot.handlers.menu.admin_relay._collect_all_user_attachments",
                   MagicMock(return_value=[1, 2])), \
             patch("aemr_bot.handlers.menu.card_format.user_card",
                   MagicMock(return_value="Карточка обращения #3")):
            await menu.show_appeal(event, appeal_id=3, max_user_id=42)

        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert text == "Карточка обращения #3"
        assert event.bot.send_message.call_args.kwargs.get("attachments")

    @pytest.mark.asyncio
    async def test_show_appeal_attachments_not_found(self) -> None:
        """show_appeal_attachments: чужое/несуществующее обращение → «не
        найдено», render не зовётся."""
        from aemr_bot.handlers import menu

        event = make_event(chat_id=100, user_id=42)
        render = AsyncMock()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.appeals_service.get_by_id_with_messages",
                   AsyncMock(return_value=None)), \
             patch("aemr_bot.handlers.menu.admin_relay.render_appeal_attachments",
                   render):
            await menu.show_appeal_attachments(event, appeal_id=1, max_user_id=42)

        render.assert_not_called()
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "не найдено" in text

    @pytest.mark.asyncio
    async def test_show_appeal_attachments_renders(self) -> None:
        """show_appeal_attachments happy-path: своё обращение → relay
        вложений жителю через render_appeal_attachments."""
        from aemr_bot.handlers import menu

        event = make_event(chat_id=100, user_id=42)
        appeal = SimpleNamespace(id=4, user=SimpleNamespace(max_user_id=42))
        render = AsyncMock()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.appeals_service.get_by_id_with_messages",
                   AsyncMock(return_value=appeal)), \
             patch("aemr_bot.handlers.menu.admin_relay.render_appeal_attachments",
                   render):
            await menu.show_appeal_attachments(event, appeal_id=4, max_user_id=42)

        render.assert_called_once()


class TestDoSubscribeConfirmBlocked:
    @pytest.mark.asyncio
    async def test_blocked_user_cannot_confirm_subscription(self) -> None:
        """do_subscribe_confirm для заблокированного → отказ, БД не
        пишется (session.execute не вызван)."""
        from aemr_bot.handlers import menu

        event = make_event(chat_id=100, user_id=42)
        session = AsyncMock()
        user = SimpleNamespace(is_blocked=True)
        with patch("aemr_bot.handlers.menu.current_user",
                   fake_current_user(user, session=session)):
            await menu.do_subscribe_confirm(event, max_user_id=42)

        session.execute.assert_not_called()
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "заблокирован" in text.lower()


# ============================================================================
# start.register() — bot_started / bot_stopped / admin-vs-citizen гарды
# ============================================================================


class _StartCapturingDispatcher:
    """Мок Dispatcher для start.register: сохраняет bot_started,
    bot_stopped и message_created(Command(...)) handler'ы. Command-
    handler'ы кладёт в dict по имени команды (Command.commands[0])."""

    def __init__(self) -> None:
        self.bot_started_handler = None
        self.bot_stopped_handler = None
        self.command_handlers: dict[str, object] = {}

    def bot_started(self):
        def deco(fn):
            self.bot_started_handler = fn
            return fn
        return deco

    def bot_stopped(self):
        def deco(fn):
            self.bot_stopped_handler = fn
            return fn
        return deco

    def message_created(self, *filters):
        # start.py всегда регистрирует команды через Command(...)-фильтр.
        cmd = filters[0].commands[0] if filters else None

        def deco(fn):
            if cmd is not None:
                self.command_handlers[cmd] = fn
            return fn
        return deco


@pytest.fixture
def start_dp():
    from aemr_bot.handlers import start

    dp = _StartCapturingDispatcher()
    start.register(dp)
    return dp


def _msg_event(*, chat_id: int = 100, user_id: int = 42, text: str = "") -> SimpleNamespace:
    return make_event(chat_id=chat_id, user_id=user_id, text=text,
                      first_name="Иван", with_user=True)


class TestStartRegisterDispatcher:
    def test_registers_all_handlers(self, start_dp) -> None:
        """register() повесил bot_started/bot_stopped и команды жителя."""
        assert start_dp.bot_started_handler is not None
        assert start_dp.bot_stopped_handler is not None
        for cmd in ("start", "help", "menu", "forget", "cancel", "policy", "whoami"):
            assert cmd in start_dp.command_handlers, f"команда /{cmd} не зарегистрирована"

    @pytest.mark.asyncio
    async def test_bot_started_in_admin_chat_is_ignored(self, start_dp) -> None:
        """BotStarted в админ-группе → cmd_start НЕ зовётся."""
        event = _msg_event()
        with patch("aemr_bot.handlers.start._is_admin_chat", return_value=True), \
             patch("aemr_bot.handlers.start.cmd_start", AsyncMock()) as cmd_start:
            await start_dp.bot_started_handler(event)
        cmd_start.assert_not_called()

    @pytest.mark.asyncio
    async def test_bot_started_in_personal_chat_runs_cmd_start(self, start_dp) -> None:
        """BotStarted в личке → cmd_start."""
        event = _msg_event()
        with patch("aemr_bot.handlers.start._is_admin_chat", return_value=False), \
             patch("aemr_bot.handlers.start.cmd_start", AsyncMock()) as cmd_start:
            await start_dp.bot_started_handler(event)
        cmd_start.assert_called_once_with(event)

    @pytest.mark.asyncio
    async def test_bot_stopped_unsubscribes_subscribed_user(self, start_dp) -> None:
        """BotStopped (житель остановил бота) → снимаем с рассылки, если
        был подписан (MAXAPI_DEEP_DIVE §17)."""
        event = SimpleNamespace(
            user=SimpleNamespace(user_id=42),
        )
        user = SimpleNamespace(subscribed_broadcast=True)
        set_sub = AsyncMock()
        with patch("aemr_bot.handlers.start._is_admin_chat", return_value=False), \
             patch("aemr_bot.handlers.start.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.start.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.start.broadcasts_service.set_subscription",
                   set_sub):
            await start_dp.bot_stopped_handler(event)
        set_sub.assert_called_once()
        # subscribed=False передан явно (kwarg).
        assert set_sub.call_args.kwargs.get("subscribed") is False

    @pytest.mark.asyncio
    async def test_bot_stopped_skips_when_not_subscribed(self, start_dp) -> None:
        """Не подписанный житель остановил бота → set_subscription не зовём."""
        event = SimpleNamespace(user=SimpleNamespace(user_id=42))
        user = SimpleNamespace(subscribed_broadcast=False)
        set_sub = AsyncMock()
        with patch("aemr_bot.handlers.start._is_admin_chat", return_value=False), \
             patch("aemr_bot.handlers.start.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.start.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.start.broadcasts_service.set_subscription",
                   set_sub):
            await start_dp.bot_stopped_handler(event)
        set_sub.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_command_in_admin_chat_shows_op_menu(self, start_dp) -> None:
        """/start в админ-группе → памятка оператора (show_op_menu), НЕ
        welcome-меню жителя."""
        from aemr_bot.handlers import admin_commands

        event = _msg_event()
        with patch("aemr_bot.handlers.start._is_admin_chat", return_value=True), \
             patch.object(admin_commands, "show_op_menu", AsyncMock()) as op_menu, \
             patch("aemr_bot.handlers.start.cmd_start", AsyncMock()) as cmd_start:
            await start_dp.command_handlers["start"](event)
        op_menu.assert_called_once()
        cmd_start.assert_not_called()

    @pytest.mark.asyncio
    async def test_forget_command_in_admin_chat_warns(self, start_dp) -> None:
        """/forget в админ-группе → подсказка «команда жителя», cmd_forget
        не зовётся."""
        event = _msg_event()
        with patch("aemr_bot.handlers.start._is_admin_chat", return_value=True), \
             patch("aemr_bot.handlers.start.reply", AsyncMock()) as reply_mock, \
             patch("aemr_bot.handlers.start.cmd_forget", AsyncMock()) as cmd_forget:
            await start_dp.command_handlers["forget"](event)
        cmd_forget.assert_not_called()
        reply_mock.assert_called_once()
        from aemr_bot import texts
        assert reply_mock.call_args.args[1] == texts.CITIZEN_COMMAND_IN_ADMIN_CHAT

    @pytest.mark.asyncio
    async def test_cancel_command_personal_runs_cmd_cancel(self, start_dp) -> None:
        """/cancel в личке → cmd_cancel."""
        event = _msg_event()
        with patch("aemr_bot.handlers.start._is_admin_chat", return_value=False), \
             patch("aemr_bot.handlers.start.cmd_cancel", AsyncMock()) as cmd_cancel:
            await start_dp.command_handlers["cancel"](event)
        cmd_cancel.assert_called_once_with(event)

    @pytest.mark.asyncio
    async def test_whoami_in_personal_chat_is_silent(self, start_dp) -> None:
        """/whoami в личке → тихо игнорируем (команда только для админ-чата)."""
        event = _msg_event()
        with patch("aemr_bot.handlers.start._is_admin_chat", return_value=False):
            await start_dp.command_handlers["whoami"](event)
        # Никакого ответа в личку.
        event.bot.send_message.assert_not_called()
        event.message.answer.assert_not_called()

    @pytest.mark.asyncio
    async def test_whoami_in_admin_chat_reports_ids(self, start_dp) -> None:
        """/whoami в админ-группе → выводит max_user_id/first_name/chat_id."""
        event = _msg_event(chat_id=123, user_id=555)
        with patch("aemr_bot.handlers.start._is_admin_chat", return_value=True), \
             patch("aemr_bot.handlers.start.reply", AsyncMock()) as reply_mock:
            await start_dp.command_handlers["whoami"](event)
        reply_mock.assert_called_once()
        text = reply_mock.call_args.args[1]
        assert "555" in text and "123" in text

    # help/menu в админ-чате ведут к памятке оператора (show_op_menu),
    # как /start. Остальные жильцовые команды — к подсказке-предупреждению.
    @pytest.mark.parametrize("cmd", ["help", "menu"])
    @pytest.mark.asyncio
    async def test_help_menu_in_admin_chat_show_op_menu(self, start_dp, cmd) -> None:
        from aemr_bot.handlers import admin_commands

        event = _msg_event()
        with patch("aemr_bot.handlers.start._is_admin_chat", return_value=True), \
             patch.object(admin_commands, "show_op_menu", AsyncMock()) as op_menu:
            await start_dp.command_handlers[cmd](event)
        op_menu.assert_called_once()

    @pytest.mark.parametrize(
        "cmd", ["policy", "rules", "subscribe", "unsubscribe", "export"]
    )
    @pytest.mark.asyncio
    async def test_citizen_commands_in_admin_chat_warn(self, start_dp, cmd) -> None:
        """policy/rules/subscribe/unsubscribe/export в админ-чате →
        CITIZEN_COMMAND_IN_ADMIN_CHAT, реальный обработчик не зовётся."""
        from aemr_bot import texts

        event = _msg_event()
        with patch("aemr_bot.handlers.start._is_admin_chat", return_value=True), \
             patch("aemr_bot.handlers.start.reply", AsyncMock()) as reply_mock:
            await start_dp.command_handlers[cmd](event)
        reply_mock.assert_called_once()
        assert reply_mock.call_args.args[1] == texts.CITIZEN_COMMAND_IN_ADMIN_CHAT

    @pytest.mark.parametrize(
        "cmd,target",
        [
            ("help", "cmd_help"),
            ("menu", "cmd_menu"),
            ("policy", "cmd_policy"),
            ("rules", "cmd_rules"),
            ("subscribe", "cmd_subscribe"),
            ("unsubscribe", "cmd_unsubscribe"),
            ("export", "cmd_export"),
        ],
    )
    @pytest.mark.asyncio
    async def test_citizen_commands_in_personal_chat_delegate(
        self, start_dp, cmd, target
    ) -> None:
        """В личке команда жителя делегирует в соответствующий cmd_*."""
        event = _msg_event()
        with patch("aemr_bot.handlers.start._is_admin_chat", return_value=False), \
             patch(f"aemr_bot.handlers.start.{target}", AsyncMock()) as fn:
            await start_dp.command_handlers[cmd](event)
        fn.assert_called_once()


# ============================================================================
# start.cmd_policy — fallback на URL при ошибке доставки PDF (167-177)
# ============================================================================


class TestCmdPolicyFallbacks:
    @pytest.mark.asyncio
    async def test_token_delivery_fails_falls_back_to_url(self) -> None:
        """Токен есть, но send_or_edit_screen с PDF упал → fallback на URL."""
        from aemr_bot.handlers import start

        event = make_event(chat_id=100, user_id=42, with_user=True)
        # Первый send_or_edit (с PDF) бросает; второй (URL) проходит.
        calls = {"n": 0}

        async def _send(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("delivery boom")

        with patch("aemr_bot.handlers.start.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.start.settings_store.get",
                   AsyncMock(side_effect=["TOK-X", "https://policy.example/p"])), \
             patch("aemr_bot.handlers.start.policy_service.build_file_attachment",
                   MagicMock(return_value={"type": "file"})), \
             patch("aemr_bot.handlers.start.send_or_edit_screen",
                   AsyncMock(side_effect=_send)) as soe:
            await start.cmd_policy(event)

        # Две попытки: PDF (упала) и URL-fallback.
        assert soe.await_count == 2
        url_text = soe.await_args_list[1].kwargs.get("text", "")
        assert "policy.example" in url_text

    @pytest.mark.asyncio
    async def test_no_token_no_url_reports_unavailable(self) -> None:
        """Нет токена и нет URL → POLICY_UNAVAILABLE."""
        from aemr_bot.handlers import start

        event = make_event(chat_id=100, user_id=42, with_user=True)
        with patch("aemr_bot.handlers.start.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.start.settings_store.get",
                   AsyncMock(side_effect=[None, None])), \
             patch("aemr_bot.handlers.start.policy_service.ensure_uploaded",
                   AsyncMock(return_value=None)), \
             patch("aemr_bot.handlers.start.send_or_edit_screen",
                   AsyncMock()) as soe:
            await start.cmd_policy(event)

        from aemr_bot import texts
        assert soe.await_args.kwargs.get("text") == texts.POLICY_UNAVAILABLE


# ============================================================================
# appeal.register().on_message — диспетчеризация в state-handler (674-685)
# ============================================================================


class _AppealCapturingDispatcher:
    def __init__(self) -> None:
        self.callback_handler = None
        self.message_handler = None

    def message_callback(self):
        def deco(fn):
            self.callback_handler = fn
            return fn
        return deco

    def message_created(self):
        def deco(fn):
            self.message_handler = fn
            return fn
        return deco


@pytest.fixture
def appeal_handlers():
    from aemr_bot.handlers import appeal

    dp = _AppealCapturingDispatcher()
    appeal.register(dp)
    return dp.callback_handler, dp.message_handler


class TestAppealOnMessageStateDispatch:
    @pytest.mark.asyncio
    async def test_plain_text_routed_to_state_handler(self, appeal_handlers) -> None:
        """Личное непустое сообщение жителя (без слэша) → handler текущего
        DialogState из _STATE_HANDLERS вызван с (event, body, text, uid)."""
        from aemr_bot.db.models import DialogState
        from aemr_bot.handlers import appeal

        _, on_message = appeal_handlers
        event = make_event(chat_id=42, user_id=7, text="Ленина, 5")
        user = SimpleNamespace(dialog_state=DialogState.AWAITING_ADDRESS.value)
        state_handler = AsyncMock()
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), \
             patch("aemr_bot.handlers.appeal.current_user",
                   fake_current_user(user)), \
             patch.dict(
                 appeal._STATE_HANDLERS,
                 {DialogState.AWAITING_ADDRESS: state_handler},
             ):
            await on_message(event)

        state_handler.assert_called_once()
        args = state_handler.call_args.args
        assert args[0] is event
        assert args[2] == "Ленина, 5"
        assert args[3] == 7

    @pytest.mark.asyncio
    async def test_no_user_id_returns_without_dispatch(self, appeal_handlers) -> None:
        """Личное сообщение без user_id → не лезем в state-handler."""
        _, on_message = appeal_handlers
        event = make_event(chat_id=42, user_id=7, text="привет")
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), \
             patch("aemr_bot.handlers.appeal.get_user_id", return_value=None), \
             patch("aemr_bot.handlers.appeal.current_user") as cu:
            await on_message(event)
        cu.assert_not_called()


# ============================================================================
# appeal._ensure_funnel_callback_state — geo-кнопка в шаге адреса (158-168)
# ============================================================================


class TestEnsureFunnelCallbackStateGeo:
    @pytest.mark.asyncio
    async def test_geo_callback_in_address_state_sends_notice(self) -> None:
        """geo:* пришёл, когда житель уже в AWAITING_ADDRESS → отдельное
        уведомление «я уже жду адрес текстом», возврат False."""
        from aemr_bot.db.models import DialogState
        from aemr_bot.handlers import appeal

        event = make_event(chat_id=100, user_id=42, with_edit_message=True)
        event.ack = AsyncMock()
        user = SimpleNamespace(dialog_state=DialogState.AWAITING_ADDRESS.value)
        send_citizen = AsyncMock()
        with patch("aemr_bot.handlers.appeal.current_user",
                   fake_current_user(user)), \
             patch("aemr_bot.handlers.appeal.ack_callback", AsyncMock()) as ack, \
             patch("aemr_bot.handlers.appeal._send_to_citizen", send_citizen):
            ok = await appeal._ensure_funnel_callback_state(
                event, max_user_id=42, payload="geo:confirm"
            )

        assert ok is False
        ack.assert_called_once()
        # Жителю отправлена подсказка про ввод адреса текстом.
        send_citizen.assert_called_once()
        assert "адрес" in send_citizen.call_args.kwargs.get("text", "").lower()

    @pytest.mark.asyncio
    async def test_topic_callback_wrong_state_sends_stale_notice(self) -> None:
        """topic:* в чужом состоянии (AWAITING_SUMMARY) → общий stale-notice,
        возврат False."""
        from aemr_bot.db.models import DialogState
        from aemr_bot.handlers import appeal

        event = make_event(chat_id=100, user_id=42)
        event.ack = AsyncMock()
        user = SimpleNamespace(dialog_state=DialogState.AWAITING_SUMMARY.value)
        with patch("aemr_bot.handlers.appeal.current_user",
                   fake_current_user(user)), \
             patch("aemr_bot.handlers.appeal.ack_callback", AsyncMock()) as ack:
            ok = await appeal._ensure_funnel_callback_state(
                event, max_user_id=42, payload="topic:0"
            )

        assert ok is False
        ack.assert_called_once()

    @pytest.mark.asyncio
    async def test_matching_state_returns_true(self) -> None:
        """Кнопка из своего состояния (topic в AWAITING_TOPIC) → True,
        основной handler продолжит работу."""
        from aemr_bot.db.models import DialogState
        from aemr_bot.handlers import appeal

        event = make_event(chat_id=100, user_id=42)
        event.ack = AsyncMock()
        user = SimpleNamespace(dialog_state=DialogState.AWAITING_TOPIC.value)
        with patch("aemr_bot.handlers.appeal.current_user",
                   fake_current_user(user)), \
             patch("aemr_bot.handlers.appeal.ack_callback", AsyncMock()) as ack:
            ok = await appeal._ensure_funnel_callback_state(
                event, max_user_id=42, payload="topic:0"
            )

        assert ok is True
        ack.assert_not_called()
