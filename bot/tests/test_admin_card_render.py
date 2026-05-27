"""Тесты freshness-rule семантики `services/admin_card.render`.

Унифицированное правило (для всех карточек с кнопками — меню и
admin appeal):
- callback_mid задан И равен menu_tracker[admin_group_id] → edit
  (карточка ещё последняя в чате);
- иначе → send new (карточка устарела/это не callback/появились
  сообщения ниже);
- force_new=True → всегда send new (для followup жителя — нужна
  явная отметка появления новой инфы).

Это **то же** правило, что у меню через send_or_edit_screen.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope


pytest.importorskip("maxapi", reason="нужен maxapi для card_format")


def _make_appeal(*, appeal_id: int = 5, admin_mid=None, last_card_mid=None):
    user = SimpleNamespace(
        first_name="Сергей",
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


def _make_bot(new_mid="new-mid-1"):
    return SimpleNamespace(
        send_message=AsyncMock(
            return_value=SimpleNamespace(
                message=SimpleNamespace(body=SimpleNamespace(mid=new_mid))
            )
        ),
        edit_message=AsyncMock(),
    )


@pytest.fixture(autouse=True)
def _clean_tracker():
    from aemr_bot.utils import menu_tracker

    menu_tracker.clear_all()
    yield
    menu_tracker.clear_all()


class TestFreshnessRule:
    @pytest.mark.asyncio
    async def test_render_always_sends_new_sacred_event_log(self) -> None:
        """2026-05-27 dual-tracker: карточка обращения всегда send_new,
        никогда не edit'ится. Раньше тут была edit-ветка
        (callback_mid == tracker → edit), что нарушало sacred event log
        (см. жалобу «закрыл 2 — одна обновила, другая нет»). Удалена.
        """
        from aemr_bot.services import admin_card
        from aemr_bot.utils import menu_tracker

        appeal = _make_appeal(last_card_mid="card-7")
        bot = _make_bot(new_mid="card-new-8")
        # Симулируем «карточка ещё свежая» — раньше это бы дало edit.
        # Теперь должно дать send_new (sacred).
        menu_tracker.set_last_menu_mid(555, "card-7")
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
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
            mid = await admin_card.render(bot, appeal, callback_mid="card-7")

        bot.edit_message.assert_not_called()
        bot.send_message.assert_awaited_once()
        assert mid == "card-new-8"

    @pytest.mark.asyncio
    async def test_render_marks_physical_only(self) -> None:
        """После render карточка регистрируется как event (только
        physical_mid). Editable_mid не трогается — клик кнопки на
        карточке не превратит её в меню."""
        from aemr_bot.services import admin_card
        from aemr_bot.utils import menu_tracker

        appeal = _make_appeal(last_card_mid="old-3")
        bot = _make_bot(new_mid="fresh-9")
        # Pre-state: было меню "menu-prev" — editable_mid должен остаться.
        menu_tracker.note_editable_send(555, "menu-prev", kind="menu")
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
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
            mid = await admin_card.render(bot, appeal, callback_mid="old-3")

        assert mid == "fresh-9"
        state = menu_tracker.get_chat_state(555)
        assert state is not None
        assert state.last_physical_mid == "fresh-9"  # карточка теперь физически последняя
        # Editable_mid остался на предыдущем меню — sacred.
        assert state.last_editable_mid == "menu-prev"

    @pytest.mark.asyncio
    async def test_no_callback_sends_new(self) -> None:
        """callback_mid=None (это не callback — finalize/followup) → send new."""
        from aemr_bot.services import admin_card

        appeal = _make_appeal()
        bot = _make_bot()
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
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
            await admin_card.render(bot, appeal, callback_mid=None)

        bot.edit_message.assert_not_called()
        bot.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_force_new_sends_even_if_last_card(self) -> None:
        """force_new=True (followup жителя) → send new даже если
        карточка ещё последняя в чате. Явная отметка появления инфы."""
        from aemr_bot.services import admin_card
        from aemr_bot.utils import menu_tracker

        appeal = _make_appeal(last_card_mid="latest-3")
        bot = _make_bot(new_mid="followup-4")
        menu_tracker.set_last_menu_mid(555, "latest-3")
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
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
            await admin_card.render(
                bot, appeal, callback_mid="latest-3", force_new=True
            )

        bot.edit_message.assert_not_called()
        bot.send_message.assert_awaited_once()


class TestFirstPublication:
    @pytest.mark.asyncio
    async def test_finalize_updates_both_mids(self) -> None:
        """На finalize (is_first_publication=True) обновляем оба:
        admin_message_id (для reply-link при relay) и
        last_admin_card_mid (текущая карточка)."""
        from aemr_bot.services import admin_card

        appeal = _make_appeal(admin_mid=None, last_card_mid=None)
        bot = _make_bot(new_mid="finalize-1")
        update_last = AsyncMock()
        update_first = AsyncMock()
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
            patch("aemr_bot.services.admin_card.session_scope",
                  _fake_session_scope),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_last_admin_card_mid",
                update_last,
            ),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_admin_message_id",
                update_first,
            ),
        ):
            mid = await admin_card.render(bot, appeal, is_first_publication=True)

        update_last.assert_awaited_once()
        update_first.assert_awaited_once()
        assert mid == "finalize-1"

    @pytest.mark.asyncio
    async def test_non_first_publication_only_updates_last_mid(self) -> None:
        """Обычный render (не finalize) обновляет только last_admin_card_mid."""
        from aemr_bot.services import admin_card

        appeal = _make_appeal(admin_mid="original-1")
        bot = _make_bot(new_mid="status-change-2")
        update_last = AsyncMock()
        update_first = AsyncMock()
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
            patch("aemr_bot.services.admin_card.session_scope",
                  _fake_session_scope),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_last_admin_card_mid",
                update_last,
            ),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_admin_message_id",
                update_first,
            ),
        ):
            await admin_card.render(bot, appeal)

        update_last.assert_awaited_once()
        update_first.assert_not_called()


class TestEditFallback:
    @pytest.mark.asyncio
    async def test_edit_failure_falls_back_to_send(self) -> None:
        """Если edit_message бросает — send new + clear tracker."""
        from aemr_bot.services import admin_card
        from aemr_bot.utils import menu_tracker

        appeal = _make_appeal(last_card_mid="stale-3")
        bot = _make_bot(new_mid="recovery-9")
        bot.edit_message = AsyncMock(side_effect=Exception("MAX 404"))
        menu_tracker.set_last_menu_mid(555, "stale-3")
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
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
            mid = await admin_card.render(bot, appeal, callback_mid="stale-3")

        bot.send_message.assert_awaited_once()
        assert mid == "recovery-9"


class TestAutoWarmMessages:
    """SACRED #6: render авто-подгружает messages если их нет.

    Раньше каждый caller обязан был сам сделать `get_by_id_with_messages`
    или выставить `appeal.__dict__["messages"] = []`. Контракт нарушался
    в menu.do_consent_revoke (через list_unanswered без selectinload) —
    timeline терялся. Теперь render guard: если messages отсутствуют /
    пустые и это не finalize — render сам подгрузит через session_scope.
    """

    @pytest.mark.asyncio
    async def test_auto_warm_when_messages_missing(self) -> None:
        from aemr_bot.services import admin_card

        appeal = _make_appeal()
        # `messages` НЕ выставлен (имитация appeal из list_unanswered)
        appeal.__dict__.pop("messages", None)

        fresh = _make_appeal()
        fake_msg = SimpleNamespace(
            direction="from_user", text="Уточнение жителя",
            attachments=[], created_at=None,
        )
        fresh.__dict__["messages"] = [fake_msg]

        bot = _make_bot()
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
            patch("aemr_bot.services.admin_card.session_scope",
                  _fake_session_scope),
            patch(
                "aemr_bot.services.admin_card.appeals_service.get_by_id_with_messages",
                AsyncMock(return_value=fresh),
            ),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_last_admin_card_mid",
                AsyncMock(),
            ),
        ):
            await admin_card.render(bot, appeal, force_new=True)

        # После render — appeal.__dict__["messages"] заполнен
        assert appeal.__dict__.get("messages") == [fake_msg]

    @pytest.mark.asyncio
    async def test_auto_warm_skipped_on_first_publication(self) -> None:
        """На finalize (is_first_publication=True) НЕ подгружаем — переписки
        реально нет, messages=[] корректно."""
        from aemr_bot.services import admin_card

        appeal = _make_appeal()
        appeal.__dict__["messages"] = []  # explicit empty (finalize)

        get_with_msgs = AsyncMock()
        bot = _make_bot()
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
            patch("aemr_bot.services.admin_card.session_scope",
                  _fake_session_scope),
            patch(
                "aemr_bot.services.admin_card.appeals_service.get_by_id_with_messages",
                get_with_msgs,
            ),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_last_admin_card_mid",
                AsyncMock(),
            ),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_admin_message_id",
                AsyncMock(),
            ),
        ):
            await admin_card.render(bot, appeal, is_first_publication=True)
        # get_by_id_with_messages НЕ должен вызываться на finalize
        get_with_msgs.assert_not_called()


class TestEventHeader:
    """event_header — маркер-шапка над send_new карточкой (для followup).

    На edit-in-place маркер НЕ добавляется (карточку правят inplace,
    оператор уже в контексте действия). На send_new — добавляется
    с разделителем.
    """

    @pytest.mark.asyncio
    async def test_event_header_prepends_on_send_new(self) -> None:
        from aemr_bot.services import admin_card

        appeal = _make_appeal()
        bot = _make_bot(new_mid="follow-9")
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
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
            await admin_card.render(
                bot,
                appeal,
                event_header="📩 Новое дополнение по обращению #5",
            )
        bot.send_message.assert_awaited_once()
        sent_text = bot.send_message.call_args.kwargs.get("text", "")
        assert "📩 Новое дополнение по обращению #5" in sent_text
        assert "────────────────" in sent_text
        # Карточка идёт под маркером — содержимое тоже на месте.
        assert "Обращение #5" in sent_text or "#5" in sent_text

    @pytest.mark.asyncio
    async def test_event_header_always_applied_after_dual_tracker(self) -> None:
        """2026-05-27 dual-tracker: edit-ветка удалена, render всегда
        send_new. Значит event_header применяется ВСЕГДА, если задан.
        Раньше тут проверялось, что на edit маркер не появляется — это
        больше не actual (edit ветки нет)."""
        from aemr_bot.services import admin_card
        from aemr_bot.utils import menu_tracker

        appeal = _make_appeal(last_card_mid="card-7")
        bot = _make_bot(new_mid="card-new-1")
        menu_tracker.set_last_menu_mid(555, "card-7")
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
            patch("aemr_bot.services.admin_card.session_scope",
                  _fake_session_scope),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_last_admin_card_mid",
                AsyncMock(),
            ),
        ):
            await admin_card.render(
                bot,
                appeal,
                callback_mid="card-7",
                event_header="📩 теперь маркер всегда применяется",
            )
        bot.edit_message.assert_not_called()
        bot.send_message.assert_awaited_once()
        sent_text = bot.send_message.call_args.kwargs.get("text", "")
        assert "📩 теперь маркер всегда применяется" in sent_text


class TestGuards:
    @pytest.mark.asyncio
    async def test_no_user_returns_none(self) -> None:
        from aemr_bot.services import admin_card

        appeal = SimpleNamespace(
            id=99, user=None, admin_message_id=None,
            last_admin_card_mid=None, attachments=[],
        )
        appeal.__dict__["messages"] = []
        bot = _make_bot()
        with patch("aemr_bot.config.settings.admin_group_id", 555):
            mid = await admin_card.render(bot, appeal)
        assert mid is None

    @pytest.mark.asyncio
    async def test_no_admin_group_returns_none(self) -> None:
        from aemr_bot.services import admin_card

        appeal = _make_appeal()
        bot = _make_bot()
        with patch("aemr_bot.config.settings.admin_group_id", 0):
            mid = await admin_card.render(bot, appeal)
        assert mid is None
