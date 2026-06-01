"""Характеризационные тесты для handlers/operator_reply.py.

Фиксируют ТЕКУЩЕЕ поведение ответа оператора жителю как страховку
перед декомпозицией god-объектов. Прод-код не меняется — только новые
ветки поверх уже существующих наборов:
  - test_handlers_operator_reply.py (intent/dedupe/mid/_deliver happy+sad);
  - test_operator_reply_with_image.py (relay одной картинки, text-only);
  - test_operator_reply_closed_guard.py (CLOSED-обращение).

Что добирается здесь (не дублируя существующее):
  - handle_operator_reply целиком: kbd-intent dispatch, swipe-reply через
    get_by_admin_message_id, fallback на маркер «🆔 №N», защита от
    spoofing маркера в НЕ-bot сообщении, no-link → False, operator
    не найден → False, appeal не найден → ADMIN_REPLY_NO_APPEAL;
  - SEC #6: деактивированный/исчезнувший оператор не доставляет ответ;
  - блокировка исходящих не-whitelisted URL в ответе оператора;
  - уведомление оператору про >1 приложенную картинку (ушла одна);
  - идемпотентность по source-update (_reply_success_key и хелперы);
  - LRU-эвикция _remember_successful_reply при росте >256;
  - _safe_admin_notice глушит своё исключение;
  - confirm-текст финального vs промежуточного в cmd_reply (is_final);
  - reply-intent перезапись при двойном нажатии (последнее побеждает).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


# --- фабрики ------------------------------------------------------------------


def _make_event(*, chat_id: int = 100, user_id: int = 7) -> SimpleNamespace:
    """Обёртка над make_event для operator_reply.

    Handler читает event.message.link и редактирует сообщения, поэтому
    добавляем link=None и bot.edit_message поверх базовой фабрики.
    chat_id по умолчанию 100 (не admin-группа) — handle_operator_reply
    не гейтит по admin-чату; cmd_reply гейтит и переопределяет chat_id.
    """
    event = make_event(chat_id=chat_id, user_id=user_id, with_edit_message=True)
    event.message.link = None
    return event


def _fresh_appeal(*, user=None, appeal_id: int = 1) -> SimpleNamespace:
    """Здоровое обращение: активный житель с действующим согласием."""
    if user is None:
        user = SimpleNamespace(
            is_blocked=False,
            first_name="Иван",
            phone="+79991234567",
            subscribed_broadcast=False,
            consent_pdn_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            consent_revoked_at=None,
            max_user_id=42,
        )
    appeal = SimpleNamespace(
        id=appeal_id,
        user=user,
        created_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        topic="Дороги",
        locality="Елизово",
        address="ул. Ленина, д. 1",
        status="new",
        summary="яма",
        attachments=[],
    )
    appeal.__dict__["messages"] = []
    return appeal


@pytest.fixture(autouse=True)
def _clean_reply_state():
    """Изоляция module-level state между тестами: recent-replies guard
    и reply-intent registry. Без очистки порядок тестов влиял бы на
    дедуп и intent-консьюм."""
    from aemr_bot.handlers import operator_reply as opr
    from aemr_bot.services import wizard_registry as _wr

    opr._recent_replies.clear()
    _wr._reply_intent.clear()
    yield
    opr._recent_replies.clear()
    _wr._reply_intent.clear()


# --- _safe_admin_notice -------------------------------------------------------


class TestSafeAdminNotice:
    @pytest.mark.asyncio
    async def test_sends_to_event_chat(self) -> None:
        """Фиксирует: уведомление уходит в chat_id события."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event(chat_id=321)
        await opr._safe_admin_notice(event, "тест")
        event.bot.send_message.assert_awaited_once()
        assert event.bot.send_message.await_args.kwargs["chat_id"] == 321
        assert event.bot.send_message.await_args.kwargs["text"] == "тест"

    @pytest.mark.asyncio
    async def test_swallows_send_exception(self) -> None:
        """Фиксирует: сбой отправки в админ-чат НЕ пробрасывается —
        аварийная ветка не должна падать второй раз поверх первопричины."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        event.bot.send_message = AsyncMock(side_effect=RuntimeError("MAX down"))
        # Не должно бросить.
        await opr._safe_admin_notice(event, "тест")


# --- идемпотентность по source-update -----------------------------------------


class TestReplySuccessKey:
    def test_none_when_no_source_key(self) -> None:
        """Фиксирует: если build_idempotency_key вернул None (нет mid/
        update) — ключ успеха тоже None, идемпотентность по источнику
        отключена для этого события."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        with patch(
            "aemr_bot.handlers.operator_reply.idempotency.build_idempotency_key",
            return_value=None,
        ):
            key = opr._reply_success_key(
                event, operator_id=7, appeal_id=1, text="hi"
            )
        assert key is None

    def test_key_embeds_operator_appeal_and_digest(self) -> None:
        """Фиксирует форму ключа: reply_ok:<op>:<appeal>:<digest>:<src>,
        обрезанную до MAX_KEY_LENGTH. Тот же текст даёт тот же digest."""
        from aemr_bot.handlers import operator_reply as opr
        from aemr_bot.services import idempotency

        event = _make_event()
        with patch(
            "aemr_bot.handlers.operator_reply.idempotency.build_idempotency_key",
            return_value="SRC-1",
        ):
            key = opr._reply_success_key(
                event, operator_id=7, appeal_id=99, text="ответ"
            )
        assert key is not None
        assert key.startswith("reply_ok:7:99:")
        assert key.endswith(":SRC-1")
        assert len(key) <= idempotency.MAX_KEY_LENGTH

    @pytest.mark.asyncio
    async def test_recorded_helpers_noop_on_none_key(self) -> None:
        """Фиксирует: is/mark-хелперы с key=None не трогают idempotency-
        стор (None — «идемпотентность недоступна», не ошибка)."""
        from aemr_bot.handlers import operator_reply as opr

        assert await opr._is_reply_success_recorded(None) is False
        # mark с None просто ничего не делает и не падает.
        await opr._mark_reply_success_recorded(None)

    @pytest.mark.asyncio
    async def test_recorded_helpers_delegate_to_idempotency(self) -> None:
        """Фиксирует делегирование: is → has_processed_raw, mark →
        try_mark_processed_raw с kind='reply_success'."""
        from aemr_bot.handlers import operator_reply as opr

        with patch(
            "aemr_bot.handlers.operator_reply.idempotency.has_processed_raw",
            AsyncMock(return_value=True),
        ) as has_raw, patch(
            "aemr_bot.handlers.operator_reply.idempotency.try_mark_processed_raw",
            AsyncMock(),
        ) as mark_raw:
            assert await opr._is_reply_success_recorded("K") is True
            await opr._mark_reply_success_recorded("K")
        has_raw.assert_awaited_once_with("K")
        mark_raw.assert_awaited_once_with("K", "reply_success")


# --- LRU-эвикция recent-replies -----------------------------------------------


class TestRecentRepliesEviction:
    def test_grows_past_threshold_then_prunes_old(self) -> None:
        """Фиксирует: при росте словаря >256 старые (за пределами окна×6)
        записи выметаются, свежая остаётся. Защита от безграничного роста
        in-memory guard'а на долгоживущем процессе."""
        from aemr_bot.handlers import operator_reply as opr

        opr._recent_replies.clear()
        # 300 старых записей с «древним» временем — далеко за cutoff.
        old_t = 0.0
        for i in range(300):
            opr._recent_replies[(i, i)] = ("old", old_t)
        # Свежая запись триггерит prune (len > 256).
        opr._remember_successful_reply(9999, 9999, "fresh")
        # Древние выметены, свежая жива.
        assert (9999, 9999) in opr._recent_replies
        assert (0, 0) not in opr._recent_replies


# --- SEC #6: деактивация оператора перед доставкой ----------------------------


class TestDeactivatedOperatorBlocked:
    @pytest.mark.asyncio
    async def test_deactivated_operator_does_not_deliver(self) -> None:
        """SEC #6: оператора деактивировали между intent/swipe и
        доставкой. Фиксирует: live_operator.is_active=False → ответ НЕ
        уходит жителю, оператору летит предупреждение о деактивации."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        appeal = MagicMock(id=5)
        operator = MagicMock(id=7, max_user_id=42)
        dead_op = SimpleNamespace(id=7, max_user_id=42, is_active=False)

        with patch.object(opr.cfg, "answer_max_chars", 1000), patch(
            "aemr_bot.handlers.operator_reply.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.operator_reply.operators_service.get",
            AsyncMock(return_value=dead_op),
        ), patch(
            "aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
            AsyncMock(return_value=_fresh_appeal()),
        ) as get_appeal:
            handled = await opr._deliver_operator_reply(
                event, appeal=appeal, operator=operator,
                text="ответ", audit_action="reply",
            )

        assert handled is True
        # До перечитки обращения не дошли — заблокированы на operator-чеке.
        get_appeal.assert_not_called()
        text = event.bot.send_message.await_args.kwargs.get("text", "")
        assert "деактивирована" in text.lower()
        assert "#5" in text

    @pytest.mark.asyncio
    async def test_vanished_operator_does_not_deliver(self) -> None:
        """SEC #6 граница: operators_service.get вернул None (оператора
        удалили из таблицы). Фиксирует тот же отказ, что и для is_active=
        False — None трактуется как «нет права отвечать»."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        appeal = MagicMock(id=5)
        operator = MagicMock(id=7, max_user_id=42)

        with patch.object(opr.cfg, "answer_max_chars", 1000), patch(
            "aemr_bot.handlers.operator_reply.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.operator_reply.operators_service.get",
            AsyncMock(return_value=None),
        ):
            handled = await opr._deliver_operator_reply(
                event, appeal=appeal, operator=operator,
                text="ответ", audit_action="reply",
            )

        assert handled is True
        text = event.bot.send_message.await_args.kwargs.get("text", "")
        assert "деактивирована" in text.lower()


# --- блокировка исходящих не-whitelisted URL ----------------------------------


class TestOutgoingUrlBlock:
    @pytest.mark.asyncio
    async def test_non_whitelisted_url_blocks_delivery(self) -> None:
        """SECURITY_REVIEW M3: фиксирует, что ответ оператора с ссылкой на
        сторонний сайт НЕ доставляется жителю. Оператору летит причина с
        перечнем разрешённых гос-доменов; add_operator_message не зовётся."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        appeal = MagicMock(id=7)
        operator = MagicMock(id=7, max_user_id=42)
        live_op = SimpleNamespace(id=7, max_user_id=42, is_active=True)

        with patch.object(opr.cfg, "answer_max_chars", 1000), patch(
            "aemr_bot.handlers.operator_reply.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.operator_reply.operators_service.get",
            AsyncMock(return_value=live_op),
        ), patch(
            "aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
            AsyncMock(return_value=_fresh_appeal()),
        ), patch(
            "aemr_bot.handlers.operator_reply._is_reply_success_recorded",
            AsyncMock(return_value=False),
        ), patch(
            "aemr_bot.services.settings_store.find_non_whitelisted_urls",
            return_value=["http://evil.example"],
        ), patch(
            "aemr_bot.handlers.operator_reply.appeals_service.add_operator_message",
            AsyncMock(),
        ) as add_message:
            handled = await opr._deliver_operator_reply(
                event, appeal=appeal, operator=operator,
                text="смотри http://evil.example", audit_action="reply",
            )

        assert handled is True
        add_message.assert_not_called()
        # Ни одного send с user_id (жителю) — только админ-уведомление.
        for call in event.bot.send_message.await_args_list:
            assert "user_id" not in call.kwargs
        text = event.bot.send_message.await_args.kwargs.get("text", "")
        assert "сторонн" in text.lower()
        assert "evil.example" in text


# --- уведомление о множественных картинках ------------------------------------


class TestMultipleImagesNotice:
    @pytest.mark.asyncio
    async def test_more_than_one_image_notifies_operator(self) -> None:
        """Фиксирует UX: оператор приложил >1 картинки → жителю уходит
        только первая, оператору отдельным сообщением уведомление «ушла
        только первая». Без этого молчаливая обрезка путала оператора."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        # Доставка жителю → SendedMessage; затем уведомление о картинках;
        # затем confirm-сообщение оператору. Все возвращают что-нибудь.
        event.bot.send_message = AsyncMock(
            side_effect=[
                SimpleNamespace(body=SimpleNamespace(mid="out-1")),
                None,
                None,
            ]
        )
        appeal = MagicMock(id=3)
        operator = MagicMock(id=7, max_user_id=42)
        live_op = SimpleNamespace(id=7, max_user_id=42, is_active=True)
        fresh = _fresh_appeal(appeal_id=3)
        fresh.admin_message_id = None  # карточку не перерисовываем

        with patch.object(opr.cfg, "answer_max_chars", 1000), patch(
            "aemr_bot.handlers.operator_reply.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.operator_reply.operators_service.get",
            AsyncMock(return_value=live_op),
        ), patch(
            "aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
            AsyncMock(side_effect=[fresh, fresh]),
        ), patch(
            "aemr_bot.handlers.operator_reply.appeals_service.get_by_id_with_messages",
            AsyncMock(return_value=fresh),
        ), patch(
            "aemr_bot.handlers.operator_reply.appeals_service.add_operator_message",
            AsyncMock(),
        ), patch(
            "aemr_bot.handlers.operator_reply.operators_service.write_audit",
            AsyncMock(),
        ), patch(
            "aemr_bot.handlers.operator_reply._is_reply_success_recorded",
            AsyncMock(return_value=False),
        ), patch(
            "aemr_bot.handlers.operator_reply._mark_reply_success_recorded",
            AsyncMock(),
        ), patch(
            "aemr_bot.services.settings_store.find_non_whitelisted_urls",
            return_value=[],
        ), patch(
            "aemr_bot.utils.image_attachments.image_attachments_from_event",
            return_value=[{"type": "image"}, {"type": "image"}],
        ), patch(
            "aemr_bot.utils.image_attachments.build_outbound_image_attachments",
            return_value=[],
        ), patch(
            "aemr_bot.services.admin_card.render",
            AsyncMock(return_value="new-mid"),
        ):
            handled = await opr._deliver_operator_reply(
                event, appeal=appeal, operator=operator,
                text="ответ с двумя картинками", audit_action="reply",
            )

        assert handled is True
        notice_texts = [
            c.kwargs.get("text", "")
            for c in event.bot.send_message.await_args_list
        ]
        # Должно быть уведомление про две картинки.
        assert any("2 картинок" in t for t in notice_texts), notice_texts
        assert any("только" in t and "перв" in t.lower() for t in notice_texts)


# --- handle_operator_reply: kbd-intent dispatch -------------------------------


class TestHandleOperatorReplyIntent:
    @pytest.mark.asyncio
    async def test_kbd_intent_routes_to_command_reply(self) -> None:
        """Фиксирует третий путь ответа (кнопка «✉️ Ответить»): если у
        оператора есть свежее reply-intent, следующий текст в админ-группе
        уходит через handle_command_reply на запомненный appeal_id, с тем
        же is_final. Свайп/маркер при этом не нужны."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event(user_id=7)
        opr.remember_reply_intent(operator_id=7, appeal_id=55, is_final=False)

        cmd_reply = AsyncMock()
        with patch(
            "aemr_bot.handlers.operator_reply.handle_command_reply", cmd_reply
        ):
            result = await opr.handle_operator_reply(
                event, body=None, text="текст ответа"
            )

        assert result is True
        cmd_reply.assert_awaited_once()
        assert cmd_reply.await_args.args[1] == 55  # appeal_id
        assert cmd_reply.await_args.args[2] == "текст ответа"
        assert cmd_reply.await_args.kwargs["is_final"] is False
        # intent одноразовое — консьюмнут.
        assert opr.consume_reply_intent(7) is None

    @pytest.mark.asyncio
    async def test_double_tap_intent_last_one_wins(self) -> None:
        """Гонка двойного нажатия: оператор тапнул «✉️ Ответить» на #10,
        потом «💬 Промежуточный» на #20. Фиксирует: перезапись intent —
        consume отдаёт ПОСЛЕДНЕЕ намерение (#20, is_final=False), первое
        затёрто (одна запись на оператора в registry)."""
        from aemr_bot.handlers import operator_reply as opr

        opr.remember_reply_intent(operator_id=7, appeal_id=10, is_final=True)
        opr.remember_reply_intent(operator_id=7, appeal_id=20, is_final=False)
        assert opr.consume_reply_intent(7) == (20, False)


# --- handle_operator_reply: swipe + marker ------------------------------------


class TestHandleOperatorReplySwipe:
    @pytest.mark.asyncio
    async def test_no_link_and_no_marker_returns_false(self) -> None:
        """Фиксирует: сообщение без ссылки-ответа и без маркера → handler
        возвращает False (диспетчер волен передать дальше). Оператор
        просто написал в группу без свайпа."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event(user_id=7)
        event.message.link = None
        result = await opr.handle_operator_reply(event, body=None, text="привет")
        assert result is False
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_swipe_by_admin_message_id_delivers(self) -> None:
        """Happy-path свайпа: link.type=reply с mid, который найден через
        get_by_admin_message_id. Фиксирует: обращение найдено →
        _deliver_operator_reply вызван с audit_action='reply'."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event(user_id=7)
        event.message.link = SimpleNamespace(
            type="reply", message=SimpleNamespace(mid="MID-1")
        )
        operator = SimpleNamespace(id=7, max_user_id=42)
        appeal = _fresh_appeal(appeal_id=8)

        deliver = AsyncMock(return_value=True)
        with patch(
            "aemr_bot.handlers.operator_reply.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.operator_reply.operators_service.get",
            AsyncMock(return_value=operator),
        ), patch(
            "aemr_bot.handlers.operator_reply.appeals_service.get_by_admin_message_id",
            AsyncMock(return_value=appeal),
        ) as by_admin, patch(
            "aemr_bot.handlers.operator_reply._deliver_operator_reply", deliver
        ):
            result = await opr.handle_operator_reply(
                event, body=None, text="ответ свайпом"
            )

        assert result is True
        by_admin.assert_awaited_once()
        deliver.assert_awaited_once()
        assert deliver.await_args.kwargs["appeal"] is appeal
        assert deliver.await_args.kwargs["audit_action"] == "reply"

    @pytest.mark.asyncio
    async def test_marker_in_bot_message_used_as_fallback(self) -> None:
        """Запасной путь /open_tickets: у карточки нет admin_message_id,
        но в тексте bot-сообщения есть «🆔 №42». Фиксирует: маркер парсится,
        обращение поднимается через get_by_id (admin-mid не дал результата)."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event(user_id=7)
        # link без type=reply, но с bot-sender и маркером в тексте.
        event.message.link = SimpleNamespace(
            type="forward",
            message=SimpleNamespace(
                mid=None,
                text="Карточка обращения\n🆔 №42",
                sender=SimpleNamespace(is_bot=True),
            ),
        )
        operator = SimpleNamespace(id=7, max_user_id=42)
        appeal = _fresh_appeal(appeal_id=42)

        deliver = AsyncMock(return_value=True)
        with patch(
            "aemr_bot.handlers.operator_reply.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.operator_reply.operators_service.get",
            AsyncMock(return_value=operator),
        ), patch(
            "aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
            AsyncMock(return_value=appeal),
        ) as by_id, patch(
            "aemr_bot.handlers.operator_reply._deliver_operator_reply", deliver
        ):
            result = await opr.handle_operator_reply(
                event, body=None, text="ответ по маркеру"
            )

        assert result is True
        # appeal_id=42 пришёл именно из маркера, через get_by_id.
        by_id.assert_awaited_once()
        assert by_id.await_args.args[1] == 42
        deliver.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_marker_in_non_bot_message_is_ignored(self) -> None:
        """SEC #3 (spoofing): тот же маркер «🆔 №99», но автор реплая — НЕ
        бот (другой оператор/житель вставил текст). Фиксирует защиту:
        маркер игнорируется, target_mid тоже нет → handler возвращает
        False, ничего не доставляется. Иначе свайп ушёл бы чужому жителю."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event(user_id=7)
        event.message.link = SimpleNamespace(
            type="forward",
            message=SimpleNamespace(
                mid=None,
                text="Я по обращению 🆔 №99 не согласен",
                sender=SimpleNamespace(is_bot=False),
            ),
        )

        deliver = AsyncMock(return_value=True)
        get_by_id = AsyncMock()
        with patch(
            "aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
            get_by_id,
        ), patch(
            "aemr_bot.handlers.operator_reply._deliver_operator_reply", deliver
        ):
            result = await opr.handle_operator_reply(
                event, body=None, text="попытка спуфинга"
            )

        assert result is False
        get_by_id.assert_not_called()
        deliver.assert_not_called()

    @pytest.mark.asyncio
    async def test_marker_dict_link_bot_sender_used(self) -> None:
        """Фиксирует dict-форму link (fallback без pydantic): message как
        словарь с sender.is_bot=True и маркером → маркер принят, обращение
        поднимается через get_by_id."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event(user_id=7)
        event.message.link = {
            "type": "forward",
            "message": {
                "text": "Карточка\n🆔 №7",
                "sender": {"is_bot": True},
            },
        }
        operator = SimpleNamespace(id=7, max_user_id=42)
        appeal = _fresh_appeal(appeal_id=7)

        deliver = AsyncMock(return_value=True)
        with patch(
            "aemr_bot.handlers.operator_reply.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.operator_reply.operators_service.get",
            AsyncMock(return_value=operator),
        ), patch(
            "aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
            AsyncMock(return_value=appeal),
        ) as by_id, patch(
            "aemr_bot.handlers.operator_reply._deliver_operator_reply", deliver
        ):
            result = await opr.handle_operator_reply(
                event, body=None, text="ответ"
            )

        assert result is True
        assert by_id.await_args.args[1] == 7
        deliver.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_swipe_but_operator_not_found_returns_false(self) -> None:
        """Фиксирует: есть валидная ссылка-ответ (target_mid), но автор не
        зарегистрирован оператором → handler возвращает False, ничего не
        доставляет (посторонний в админ-группе свайпнул карточку)."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event(user_id=7)
        event.message.link = SimpleNamespace(
            type="reply", message=SimpleNamespace(mid="MID-1")
        )

        deliver = AsyncMock(return_value=True)
        with patch(
            "aemr_bot.handlers.operator_reply.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.operator_reply.operators_service.get",
            AsyncMock(return_value=None),
        ), patch(
            "aemr_bot.handlers.operator_reply._deliver_operator_reply", deliver
        ):
            result = await opr.handle_operator_reply(
                event, body=None, text="ответ"
            )

        assert result is False
        deliver.assert_not_called()
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_swipe_appeal_not_found_sends_no_appeal_notice(self) -> None:
        """Фиксирует: оператор найден, ссылка-ответ есть, но ни по admin-
        mid, ни по маркеру обращение не нашлось → возвращает True и шлёт
        ADMIN_REPLY_NO_APPEAL (просьбу свайпнуть карточку)."""
        from aemr_bot.handlers import operator_reply as opr
        from aemr_bot import texts

        event = _make_event(user_id=7)
        event.message.link = SimpleNamespace(
            type="reply", message=SimpleNamespace(mid="MID-UNKNOWN")
        )
        operator = SimpleNamespace(id=7, max_user_id=42)

        with patch(
            "aemr_bot.handlers.operator_reply.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.operator_reply.operators_service.get",
            AsyncMock(return_value=operator),
        ), patch(
            "aemr_bot.handlers.operator_reply.appeals_service.get_by_admin_message_id",
            AsyncMock(return_value=None),
        ):
            result = await opr.handle_operator_reply(
                event, body=None, text="ответ"
            )

        assert result is True
        event.bot.send_message.assert_awaited_once()
        assert (
            event.bot.send_message.await_args.kwargs.get("text")
            == texts.ADMIN_REPLY_NO_APPEAL
        )


# --- handle_command_reply: финальный vs промежуточный audit_action ------------


class TestCommandReplyAuditAction:
    async def _run(self, *, is_final: bool) -> AsyncMock:
        """Прогоняет cmd_reply до _deliver_operator_reply (он замокан),
        возвращает мок deliver для проверки audit_action/is_final."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event(chat_id=555, user_id=7)
        operator = SimpleNamespace(id=7, max_user_id=42)
        appeal = _fresh_appeal(appeal_id=12)
        deliver = AsyncMock(return_value=True)
        with patch.object(opr.cfg, "admin_group_id", 555), patch(
            "aemr_bot.handlers.operator_reply.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.operator_reply.operators_service.get",
            AsyncMock(return_value=operator),
        ), patch(
            "aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
            AsyncMock(return_value=appeal),
        ), patch(
            "aemr_bot.handlers.operator_reply._deliver_operator_reply", deliver
        ):
            await opr.handle_command_reply(
                event, appeal_id=12, text="ответ", is_final=is_final
            )
        return deliver

    @pytest.mark.asyncio
    async def test_final_uses_reply_via_command(self) -> None:
        """Фиксирует: is_final=True (default /reply) → audit_action=
        'reply_via_command', is_final проброшен True."""
        deliver = await self._run(is_final=True)
        deliver.assert_awaited_once()
        assert deliver.await_args.kwargs["audit_action"] == "reply_via_command"
        assert deliver.await_args.kwargs["is_final"] is True

    @pytest.mark.asyncio
    async def test_intermediate_uses_reply_intermediate_via_command(self) -> None:
        """Фиксирует: is_final=False (промежуточный) → audit_action=
        'reply_intermediate_via_command', is_final проброшен False."""
        deliver = await self._run(is_final=False)
        assert (
            deliver.await_args.kwargs["audit_action"]
            == "reply_intermediate_via_command"
        )
        assert deliver.await_args.kwargs["is_final"] is False


# --- confirm-текст в полном проходе cmd_reply ---------------------------------


class TestCommandReplyDeliversAndPersists:
    @pytest.mark.asyncio
    async def test_full_path_writes_messages_and_audit(self) -> None:
        """Полный проход /reply N: фиксирует, что доставленный ответ
        попадает в messages (add_operator_message) и в audit_log
        (write_audit), а оператор получает confirm-текст про обращение."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event(chat_id=555, user_id=7)
        # житель → SendedMessage; затем confirm оператору.
        event.bot.send_message = AsyncMock(
            side_effect=[SimpleNamespace(body=SimpleNamespace(mid="out-1")), None]
        )
        operator = SimpleNamespace(id=7, max_user_id=42)
        live_op = SimpleNamespace(id=7, max_user_id=42, is_active=True)
        fresh = _fresh_appeal(appeal_id=12)
        fresh.admin_message_id = None

        # get_by_id зовётся трижды на полном пути (cmd_reply lookup,
        # pre-delivery re-read, _persist_reply_and_card) — всем отдаём
        # одно и то же здоровое обращение через return_value, чтобы не
        # завязываться на точный счётчик вызовов.
        with patch.object(opr.cfg, "admin_group_id", 555), patch.object(
            opr.cfg, "answer_max_chars", 1000
        ), patch(
            "aemr_bot.handlers.operator_reply.session_scope",
            _fake_session_scope,
        ), patch(
            "aemr_bot.handlers.operator_reply.operators_service.get",
            AsyncMock(side_effect=[operator, live_op]),
        ), patch(
            "aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
            AsyncMock(return_value=fresh),
        ), patch(
            "aemr_bot.handlers.operator_reply.appeals_service.get_by_id_with_messages",
            AsyncMock(return_value=fresh),
        ), patch(
            "aemr_bot.handlers.operator_reply.appeals_service.add_operator_message",
            AsyncMock(),
        ) as add_message, patch(
            "aemr_bot.handlers.operator_reply.operators_service.write_audit",
            AsyncMock(),
        ) as write_audit, patch(
            "aemr_bot.handlers.operator_reply._is_reply_success_recorded",
            AsyncMock(return_value=False),
        ), patch(
            "aemr_bot.handlers.operator_reply._mark_reply_success_recorded",
            AsyncMock(),
        ), patch(
            "aemr_bot.services.settings_store.find_non_whitelisted_urls",
            return_value=[],
        ), patch(
            "aemr_bot.utils.image_attachments.image_attachments_from_event",
            return_value=[],
        ), patch(
            "aemr_bot.utils.image_attachments.build_outbound_image_attachments",
            return_value=[],
        ), patch(
            "aemr_bot.services.admin_card.render",
            AsyncMock(return_value="new-mid"),
        ):
            await opr.handle_command_reply(
                event, appeal_id=12, text="официальный ответ", is_final=True
            )

        add_message.assert_awaited_once()
        # is_final пробрасывается в запись messages.
        assert add_message.await_args.kwargs["is_final"] is True
        write_audit.assert_awaited_once()
        assert write_audit.await_args.kwargs["action"] == "reply_via_command"
        # Первый send — жителю (user_id), последний — confirm оператору.
        assert event.bot.send_message.await_args_list[0].kwargs["user_id"] == 42
        confirm = event.bot.send_message.await_args_list[-1].kwargs.get("text", "")
        assert "#12" in confirm
        assert "закрыт" in confirm.lower() or "Отвечено" in confirm
