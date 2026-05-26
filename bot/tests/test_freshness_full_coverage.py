"""Характеризация полного контракта freshness-rule для admin-чата.

Цель: один файл = одна страница «как ведёт себя бот при всех возможных
комбинациях карточек × callback'ов». До этого freshness покрывался
точечно в `test_admin_card_render.py` (unit на render) и в
`test_send_or_edit_screen.py` (unit на меню). Но **взаимодействие**
двух freshness-aware сервисов между собой — критично для UX и не было
покрыто. Жалоба владельца 2026-05-26: «карточка обращения снова
редактируется при переходе в админ меню после открытия через listing» —
именно такой межсервисный сценарий.

Канонические правила (источник истины: docstring admin_card.py +
docstring send_or_edit_screen):

1. **admin_card.render(force_new=False, callback_mid=mid)** → edit
   карточки, ЕСЛИ `mid == menu_tracker[admin_group_id]`. Иначе send_new.
2. **admin_card.render(force_new=True, ...)** → всегда send_new, +
   `menu_tracker.clear(admin_group_id)` после успешного send.
3. **send_or_edit_screen(force_new_message=False, callback_mid=mid)** →
   edit меню, ЕСЛИ `mid == menu_tracker[chat_id]`. Иначе send_new,
   tracker обновляется на новый mid.
4. **admin_card.render ВСЕГДА clear()'ит tracker после send_new** — это
   SACRED: следующий тап на любой кнопке менюшки НЕ должен edit'нуть
   sacred-карточку, даже если её mid случайно совпадает с callback_mid.
5. **admin_bus.send + note_incoming_admin_message** двигают tracker на
   свой mid — это закрывает дыру «оператор написал, но tracker остался
   выше».

Test ID-структура: `TestX_<scenario>::test_<expected_behavior>`. Каждый
тест имитирует ровно одну точку решения «edit vs send_new» и проверяет
выбранную ветку через моки `bot.send_message` / `bot.edit_message`.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope


pytest.importorskip("maxapi", reason="нужен maxapi для admin_card / event")


ADMIN_CHAT_ID = 555


def _make_appeal(*, appeal_id: int = 5, admin_mid=None, last_card_mid=None):
    """Минимальный appeal для admin_card.render — повторяет helper из
    test_admin_card_render.py, чтобы тесты были self-contained."""
    user = SimpleNamespace(
        first_name="Иван",
        phone="+79991234567",
        is_blocked=False,
        consent_pdn_at=None,
        consent_revoked_at=None,
        subscribed_broadcast=False,
        max_user_id=42,
    )
    appeal = SimpleNamespace(
        id=appeal_id,
        user=user,
        status="new",
        locality="Елизовское ГП",
        address="ул. Ленина, 5",
        topic="Дороги",
        summary="Яма во дворе.",
        attachments=[],
        admin_message_id=admin_mid,
        last_admin_card_mid=last_card_mid,
        closed_due_to_revoke=False,
    )
    appeal.__dict__["messages"] = []
    return appeal


def _make_bot(send_mids: list[str] | None = None):
    """Bot с настраиваемым стримом mid'ов для последовательных send'ов.

    Цель: тесты на цепочки (listing → open_card → menu) должны
    различать mid'ы каждого send'а, чтобы tracker мог между ними
    переключаться корректно.
    """
    sequence = list(send_mids or ["m-1"])

    def _next_send(*args, **kwargs):
        mid = sequence.pop(0) if sequence else f"m-extra-{len(sequence)}"
        return SimpleNamespace(
            message=SimpleNamespace(body=SimpleNamespace(mid=mid))
        )

    return SimpleNamespace(
        send_message=AsyncMock(side_effect=_next_send),
        edit_message=AsyncMock(
            return_value=SimpleNamespace(
                message=SimpleNamespace(body=SimpleNamespace(mid="m-edited"))
            )
        ),
    )


def _make_event(*, bot, callback_mid: str | None):
    """Event-like объект для send_or_edit_screen. Если callback_mid задан
    — это callback (event.callback присутствует, event.message.body.mid
    задан). Если None — это команда / текст (нет callback).
    """
    msg = SimpleNamespace(
        body=SimpleNamespace(mid=callback_mid) if callback_mid else None,
        recipient=SimpleNamespace(chat_id=ADMIN_CHAT_ID),
    )
    event = SimpleNamespace(
        bot=bot,
        message=msg,
        callback=SimpleNamespace(callback_id="cb-1") if callback_mid else None,
    )

    def _get_ids():
        return (ADMIN_CHAT_ID, 7)

    event.get_ids = _get_ids
    return event


@pytest.fixture(autouse=True)
def _clean_tracker():
    """Каждый тест стартует с чистым tracker'ом — иначе утечки между
    тестами маскируют bug'и (сценарий A выставил tracker, сценарий B
    унаследовал)."""
    from aemr_bot.utils import menu_tracker

    menu_tracker.clear_all()
    yield
    menu_tracker.clear_all()


# ============================================================================
# GROUP A: admin_card.render + freshness rule (unit-level)
# ============================================================================


class TestA_AdminCardRender:
    """Базовый контракт admin_card.render. Дублирует ключевые случаи из
    test_admin_card_render.py для self-containedness — но более явно
    разделяет «что мы проверяем» (один тест = одно правило)."""

    @pytest.mark.asyncio
    async def test_force_new_clears_tracker_after_send(self) -> None:
        """force_new=True → send_new → tracker должен стать None.

        Это SACRED-фикс 2026-05-26: без clear'а следующий callback
        тапа меню edit'нет карточку обращения."""
        from aemr_bot.services import admin_card
        from aemr_bot.utils import menu_tracker

        appeal = _make_appeal()
        bot = _make_bot(send_mids=["card-new-1"])
        menu_tracker.set_last_menu_mid(ADMIN_CHAT_ID, "stale-listing-7")
        with (
            patch("aemr_bot.config.settings.admin_group_id", ADMIN_CHAT_ID),
            patch("aemr_bot.services.admin_card.session_scope",
                  _fake_session_scope),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_last_admin_card_mid",
                AsyncMock(),
            ),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_admin_message_id",
                AsyncMock(),
            ),
        ):
            await admin_card.render(bot, appeal, force_new=True)

        bot.send_message.assert_awaited_once()
        bot.edit_message.assert_not_called()
        # SACRED: tracker очищен, sacred card НЕ участвует в freshness
        # для следующих callback'ов меню.
        assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) is None


# ============================================================================
# GROUP B: send_or_edit_screen ПОСЛЕ admin_card.render — критичный
# интерактивный сценарий (жалоба владельца).
# ============================================================================


class TestB_MenuAfterAdminCard:
    """После admin_card.render(force_new=True) карточка опубликована и
    tracker очищен. Следующий тап op:menu на КАРТОЧКЕ (callback_mid =
    card_mid) НЕ должен edit'нуть карточку — должен послать новое меню.

    Это центральный сценарий жалобы владельца 2026-05-26 — раньше
    sacred-карточка превращалась в меню при тапе любой кнопки op:menu."""

    @pytest.mark.asyncio
    async def test_menu_callback_on_card_sends_new_menu(self) -> None:
        """force_new render → clear → menu callback с card_mid → send_new
        menu (НЕ edit карточки).
        """
        from aemr_bot.services import admin_card
        from aemr_bot.utils.event import send_or_edit_screen
        from aemr_bot.utils import menu_tracker

        appeal = _make_appeal()
        bot = _make_bot(send_mids=["card-mid-1", "menu-mid-2"])
        with (
            patch("aemr_bot.config.settings.admin_group_id", ADMIN_CHAT_ID),
            patch("aemr_bot.services.admin_card.session_scope",
                  _fake_session_scope),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_last_admin_card_mid",
                AsyncMock(),
            ),
        ):
            # 1. open_card (через клик на listing): render с force_new=True.
            card_mid = await admin_card.render(bot, appeal, force_new=True)
            assert card_mid == "card-mid-1"
            assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) is None

            # 2. op:menu — callback на КАРТОЧКЕ (callback_mid = card_mid).
            event = _make_event(bot=bot, callback_mid=card_mid)
            await send_or_edit_screen(
                event,
                chat_id=ADMIN_CHAT_ID,
                text="📋 Памятка оператора (меню)",
            )

        # CRITICAL: bot.edit_message НЕ должен быть вызван (иначе
        # карточка обращения превратится в меню).
        bot.edit_message.assert_not_called()
        # menu отправлен новой записью.
        assert bot.send_message.await_count == 2
        # И tracker теперь = menu_mid (от send_or_edit_screen),
        # не card_mid.
        assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) == "menu-mid-2"

    @pytest.mark.asyncio
    async def test_menu_callback_with_no_tracker_sends_new(self) -> None:
        """Если tracker = None (после admin_card.render clear), любой
        callback на меню → send_new, независимо от callback_mid.
        """
        from aemr_bot.utils.event import send_or_edit_screen
        from aemr_bot.utils import menu_tracker

        bot = _make_bot(send_mids=["menu-fresh-1"])
        # Tracker уже пуст (после _clean_tracker fixture).
        assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) is None

        event = _make_event(bot=bot, callback_mid="any-old-mid-99")
        await send_or_edit_screen(
            event,
            chat_id=ADMIN_CHAT_ID,
            text="Меню",
        )
        bot.edit_message.assert_not_called()
        bot.send_message.assert_awaited_once()


# ============================================================================
# GROUP C: Полная цепочка listing → open_card → menu (жалоба владельца
# конкретно про этот flow).
# ============================================================================


class TestC_ListingOpenCardMenu:
    """Имитация реального операторского flow:

    1. Op в админ меню (tracker = menu_mid_1).
    2. Op тапает «📂 Открытые обращения» → send_or_edit_screen edit'ит
       menu_mid_1 → tracker остаётся = menu_mid_1.
    3. Op тапает «📂 Открыть #N» → admin_card.render(force_new=True) →
       new card_mid → tracker.clear() → None.
    4. Op тапает «🏠 В админ-меню» (op:menu) — НО эта кнопка на listing
       (выше карточки), не на карточке. callback_mid = listing_mid.
       send_or_edit_screen: tracker=None → send_new menu.

    Если правило freshness работает — карточка обращения НЕ
    редактируется ни в одной точке flow. Тест проверяет именно это.
    """

    @pytest.mark.asyncio
    async def test_full_listing_open_card_flow_no_card_edit(self) -> None:
        from aemr_bot.services import admin_card
        from aemr_bot.utils.event import send_or_edit_screen
        from aemr_bot.utils import menu_tracker

        appeal = _make_appeal()
        # Sequence для send_message — только SEND'ы (edit не consume'ит).
        # Шаг 1: send_new menu → "menu-1". Шаг 2: edit-in-place (без
        # send). Шаг 3: send_new card → "card-2". Шаг 4: send_new menu
        # → "menu-4".
        bot = _make_bot(send_mids=["menu-1", "card-2", "menu-4"])

        with (
            patch("aemr_bot.config.settings.admin_group_id", ADMIN_CHAT_ID),
            patch("aemr_bot.services.admin_card.session_scope",
                  _fake_session_scope),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_last_admin_card_mid",
                AsyncMock(),
            ),
        ):
            # Шаг 1: show_op_menu — tracker = menu-1.
            event_1 = _make_event(bot=bot, callback_mid=None)  # /menu cmd
            await send_or_edit_screen(
                event_1, chat_id=ADMIN_CHAT_ID, text="меню 1"
            )
            assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) == "menu-1"

            # Шаг 2: тап «📂 Открытые обращения» (op:open_tickets) на
            # меню → callback_mid = menu-1 = tracker → edit-in-place.
            # Listing не двигает tracker (edit сохраняет mid).
            event_2 = _make_event(bot=bot, callback_mid="menu-1")
            await send_or_edit_screen(
                event_2, chat_id=ADMIN_CHAT_ID, text="listing"
            )
            assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) == "menu-1"

            # Шаг 3: open_card → admin_card.render(force_new=True).
            card_mid = await admin_card.render(bot, appeal, force_new=True)
            assert card_mid == "card-2"
            # SACRED clear — tracker должен быть None.
            assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) is None

            # Шаг 4: тап op:menu на listing (или callback на любом
            # сообщении) → send_new menu.
            event_4 = _make_event(bot=bot, callback_mid="menu-1")
            await send_or_edit_screen(
                event_4, chat_id=ADMIN_CHAT_ID, text="меню 4"
            )

        # CRITICAL: bot.edit_message вызван РОВНО ОДИН РАЗ — только в
        # шаге 2 (listing edit'ит меню). НЕ должен быть вызван в шаге 4
        # (это было бы sacred-violation: меню edit'ит карточку).
        assert bot.edit_message.await_count == 1
        # И вызван с callback_mid = menu-1 (mid menu, не card).
        edit_call_kwargs = bot.edit_message.call_args.kwargs
        assert edit_call_kwargs["message_id"] == "menu-1"
        # Tracker в конце = menu-4 (последнее menu).
        assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) == "menu-4"


# ============================================================================
# GROUP D: Op action на карточке (op:reply / op:close / op:reopen) после
# open_card — render передаёт callback_mid, tracker=None, send_new.
# ============================================================================


class TestD_OpActionAfterOpenCard:
    """После open_card tracker=None. Op тапает op:reply / op:close / etc
    на карточке. _show_appeal_card_or_result вызывает render с
    callback_mid=card_mid → freshness: callback_mid != None, tracker=None
    → can_edit=False → send_new card."""

    @pytest.mark.asyncio
    async def test_reply_action_after_open_card_sends_new_card(self) -> None:
        from aemr_bot.services import admin_card
        from aemr_bot.utils import menu_tracker

        appeal = _make_appeal()
        bot = _make_bot(send_mids=["card-open-1", "card-after-reply-2"])
        with (
            patch("aemr_bot.config.settings.admin_group_id", ADMIN_CHAT_ID),
            patch("aemr_bot.services.admin_card.session_scope",
                  _fake_session_scope),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_last_admin_card_mid",
                AsyncMock(),
            ),
        ):
            card_mid = await admin_card.render(bot, appeal, force_new=True)
            assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) is None

            # Op тапает «✏️ Ответить» — handler передаёт callback_mid.
            appeal.status = "in_progress"  # после reply intent
            await admin_card.render(bot, appeal, callback_mid=card_mid)

        # CRITICAL: edit НЕ вызывался — карточка появляется новой
        # записью с обновлённым статусом, оригинал остаётся в истории
        # выше как иммутабельная sacred-запись.
        bot.edit_message.assert_not_called()
        assert bot.send_message.await_count == 2

    @pytest.mark.asyncio
    async def test_render_when_tracker_matches_edits_inplace(self) -> None:
        """Контр-кейс: tracker = card_mid (например, после первой
        публикации карточки без force_new и без других сообщений
        между), render(callback_mid=card_mid) → edit-in-place.

        Это правильное поведение, когда карточка ЕЩЁ последняя в чате —
        оператор тапает кнопку, бот обновляет её на месте, чтобы
        оператор видел изменение."""
        from aemr_bot.services import admin_card
        from aemr_bot.utils import menu_tracker

        appeal = _make_appeal(last_card_mid="card-1")
        bot = _make_bot()
        # Имитация: tracker выставлен на карточку (например, после
        # finalize первой публикации с особой логикой outer кода).
        menu_tracker.set_last_menu_mid(ADMIN_CHAT_ID, "card-1")

        with (
            patch("aemr_bot.config.settings.admin_group_id", ADMIN_CHAT_ID),
            patch("aemr_bot.services.admin_card.session_scope",
                  _fake_session_scope),
        ):
            await admin_card.render(bot, appeal, callback_mid="card-1")

        bot.edit_message.assert_awaited_once()
        bot.send_message.assert_not_called()


# ============================================================================
# GROUP E: admin_bus.send + incoming-middleware двигают tracker —
# любой следующий callback на «карточку выше» → send_new.
# ============================================================================


class TestE_TrackerInvalidationByExternalMessages:
    """Pulse / admin_event / incoming op message смещают tracker. Это
    закрывает дыру «карточка выше tracker'а — callback freshness
    говорит "это последняя" → edit вверху чата → оператор внизу не
    видит изменение»."""

    @pytest.mark.asyncio
    async def test_pulse_via_admin_bus_invalidates_card_callback(self) -> None:
        from aemr_bot.services import admin_bus
        from aemr_bot.utils import menu_tracker
        from aemr_bot.utils.event import send_or_edit_screen

        bot = _make_bot(send_mids=["pulse-mid-1", "fresh-menu-2"])

        with patch("aemr_bot.config.settings.admin_group_id", ADMIN_CHAT_ID):
            # Op опубликовал карточку давно — tracker до того был
            # = old_card_mid.
            menu_tracker.set_last_menu_mid(ADMIN_CHAT_ID, "old-card-99")

            # Pulse приходит — admin_bus.send → tracker = pulse_mid.
            await admin_bus.send(bot, text="🟢 Pulse")
            assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) == "pulse-mid-1"

            # Op тапает кнопку на старой карточке (на «old-card-99»).
            event = _make_event(bot=bot, callback_mid="old-card-99")
            await send_or_edit_screen(event, chat_id=ADMIN_CHAT_ID, text="меню")

        # Send_or_edit_screen видит callback_mid (old-card-99) != tracker
        # (pulse-mid-1) → send_new, НЕ edit старой карточки выше.
        bot.edit_message.assert_not_called()
        # 2 send'а: pulse и меню.
        assert bot.send_message.await_count == 2

    @pytest.mark.asyncio
    async def test_incoming_op_message_invalidates_card_callback(self) -> None:
        """Op написал в чат → middleware (note_incoming_admin_message)
        двигает tracker → следующий тап на старой карточке → send_new."""
        from aemr_bot.services import admin_bus
        from aemr_bot.utils import menu_tracker
        from aemr_bot.utils.event import send_or_edit_screen

        bot = _make_bot(send_mids=["fresh-menu-1"])

        with patch("aemr_bot.config.settings.admin_group_id", ADMIN_CHAT_ID):
            menu_tracker.set_last_menu_mid(ADMIN_CHAT_ID, "old-card-99")
            # Op написал в чат — middleware зарегистрировал mid.
            admin_bus.note_incoming_admin_message("op-msg-77")
            assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) == "op-msg-77"

            event = _make_event(bot=bot, callback_mid="old-card-99")
            await send_or_edit_screen(event, chat_id=ADMIN_CHAT_ID, text="меню")

        bot.edit_message.assert_not_called()
        bot.send_message.assert_awaited_once()


# ============================================================================
# GROUP F: Двойной open_card — открыли #1, затем #2. Tracker = None
# после каждого, обе карточки — новые записи внизу.
# ============================================================================


class TestF_MultipleOpenCard:
    """Op открыл одно обращение, потом второе. Каждый open_card =
    отдельная sacred-запись в чате."""

    @pytest.mark.asyncio
    async def test_two_open_cards_both_send_new(self) -> None:
        from aemr_bot.services import admin_card
        from aemr_bot.utils import menu_tracker

        appeal_1 = _make_appeal(appeal_id=11)
        appeal_2 = _make_appeal(appeal_id=22)
        bot = _make_bot(send_mids=["card-11", "card-22"])
        with (
            patch("aemr_bot.config.settings.admin_group_id", ADMIN_CHAT_ID),
            patch("aemr_bot.services.admin_card.session_scope",
                  _fake_session_scope),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_last_admin_card_mid",
                AsyncMock(),
            ),
        ):
            mid_1 = await admin_card.render(bot, appeal_1, force_new=True)
            assert mid_1 == "card-11"
            assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) is None

            mid_2 = await admin_card.render(bot, appeal_2, force_new=True)
            assert mid_2 == "card-22"
            assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) is None

        # Обе карточки send_new — ни одна не edit'илась.
        bot.edit_message.assert_not_called()
        assert bot.send_message.await_count == 2


# ============================================================================
# GROUP G_close: «закрыл 2 — обе должны обновить» (жалоба владельца).
# DDD-fix 2026-05-26: _show_appeal_card_or_result теперь force_new=True
# всегда, поэтому каждое op-действие — новая карточка внизу с актуальным
# статусом. Раньше первая edit'илась на месте, вторая send'илась → user
# видел «одну обновила, другую нет».
# ============================================================================


class TestG_TwoActionsBothPublishNewCards:
    """После двух последовательных op-действий (close, reopen, reply,
    block) на разных обращениях — обе карточки должны появиться новыми
    записями внизу. Никакого edit-in-place: sacred event log.

    Это regression-тест на жалобу: «закрыл 2 обращения подряд — одна
    обновила статус, другая нет». Корень: freshness rule edit'ил первую
    карточку (она была свежая), вторая sent'илась новой → визуальная
    inconsistency.
    """

    @pytest.mark.asyncio
    async def test_two_closes_both_publish_new_cards(self) -> None:
        from aemr_bot.services import admin_card
        from aemr_bot.utils import menu_tracker

        # Имитация: 2 разных appeal'а закрываются подряд через
        # _show_appeal_card_or_result (force_new=True всегда).
        appeal_1 = _make_appeal(appeal_id=11, last_card_mid="card-1")
        appeal_1.status = "closed"  # после close
        appeal_2 = _make_appeal(appeal_id=22, last_card_mid="card-2")
        appeal_2.status = "closed"

        bot = _make_bot(send_mids=["closed-1-new", "closed-2-new"])
        # Имитация: tracker = card-1 (как было бы перед первым close).
        menu_tracker.set_last_menu_mid(ADMIN_CHAT_ID, "card-1")

        with (
            patch("aemr_bot.config.settings.admin_group_id", ADMIN_CHAT_ID),
            patch("aemr_bot.services.admin_card.session_scope",
                  _fake_session_scope),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_last_admin_card_mid",
                AsyncMock(),
            ),
        ):
            # close #1 → force_new=True → send_new "closed-1-new",
            # tracker.clear() → None.
            mid_1 = await admin_card.render(bot, appeal_1, force_new=True)
            assert mid_1 == "closed-1-new"
            assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) is None

            # close #2 → force_new=True → send_new "closed-2-new".
            mid_2 = await admin_card.render(bot, appeal_2, force_new=True)
            assert mid_2 == "closed-2-new"
            assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) is None

        # CRITICAL: обе карточки send_new (НЕ edit). Каждая — новая
        # запись внизу с актуальным статусом CLOSED. Оператор видит обе
        # как «обновлённые», нет визуальной inconsistency.
        bot.edit_message.assert_not_called()
        assert bot.send_message.await_count == 2


# ============================================================================
# GROUP H: edit_message fail → fallback на send + clear tracker
# ============================================================================


class TestH_EditFailureFallback:
    """Если MAX вернул ошибку на edit (например, message removed) —
    fallback на send_new. Tracker должен быть очищен, чтобы следующий
    callback не попытался edit'нуть тот же битый mid."""

    @pytest.mark.asyncio
    async def test_send_or_edit_screen_edit_fail_clears_tracker(self) -> None:
        from aemr_bot.utils.event import send_or_edit_screen
        from aemr_bot.utils import menu_tracker

        bot = _make_bot(send_mids=["recovery-mid-9"])
        bot.edit_message = AsyncMock(side_effect=Exception("MAX 404"))
        menu_tracker.set_last_menu_mid(ADMIN_CHAT_ID, "stale-7")

        event = _make_event(bot=bot, callback_mid="stale-7")
        await send_or_edit_screen(
            event, chat_id=ADMIN_CHAT_ID, text="меню",
        )

        bot.send_message.assert_awaited_once()
        # После fallback tracker = новый mid (от send), НЕ старый.
        assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) == "recovery-mid-9"
