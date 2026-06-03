"""TDD-тесты edit-policy и menu_tracker'а — кардинальное UX-улучшение.

**Контекст проблемы (PM/UX research):**

Раньше `send_or_edit_screen` решал «edit vs send new» **только** по типу
события: callback → edit, иначе → send. Это создавало сценарий-поломку:

```
1. /op_help → бот шлёт меню (mid=100)
2. Жмёт «📋 Открытые» → edit_message mid=100 → меню стало списком
3. Свайп-reply на admin appeal card (mid=120, отправлено отдельно) →
   подтверждение mid=130 (новое сообщение).
4. Скроллит ВВЕРХ к mid=100, видит там «карточку #42», жмёт
   «🏠 В админ-меню».
5. Бот edit'ит mid=100 → меню. Но оператор глубоко внизу чата,
   изменения далеко вверху не видит. Кажется, бот не отреагировал.
```

**Решение:** in-memory tracker `chat_id → last_menu_mid`. Edit разрешён
**только** если callback-mid совпадает с tracker'ом. Иначе — send new.

**Контракт:**
- Sacred карточки (admin appeal card, citizen reply, broadcast progress,
  audit, pulse, reminders) НЕ ходят через `send_or_edit_screen` — они
  отправляются напрямую `bot.send_message`. Tracker их не учитывает,
  они всегда new. Это уже так в коде по дизайну.
- Menu карточки (главное меню, /op_help, settings, operators wizard,
  preview-карточка broadcast и т.п.) → `send_or_edit_screen` обновляет
  tracker при каждом send/edit; edit срабатывает только когда mid
  совпадает с tracker'ом.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("maxapi", reason="send_or_edit_screen тянет maxapi")


def _make_event(*, chat_id: int, callback_mid: str | None) -> SimpleNamespace:
    """Минимальное событие для send_or_edit_screen.

    callback_mid=None → не-callback (команда / текстовый шаг).
    callback_mid='X' → callback с mid='X' (нажатие кнопки на карточке с тем mid).
    """
    bot = MagicMock()
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(
            body=SimpleNamespace(mid="new-server-assigned-mid")
        )
    )
    bot.edit_message = AsyncMock()
    msg = SimpleNamespace(
        body=SimpleNamespace(mid=callback_mid),
        recipient=SimpleNamespace(chat_id=chat_id),
        sender=SimpleNamespace(user_id=7),
    )
    event_kwargs: dict = {
        "bot": bot,
        "message": msg,
    }
    if callback_mid is not None:
        # callback-нажатие
        event_kwargs["callback"] = SimpleNamespace(callback_id="cb-1")
    return SimpleNamespace(**event_kwargs)


# ---- menu_tracker module (новый) ------------------------------------------


class TestMenuTracker:
    def setup_method(self) -> None:
        from aemr_bot.utils import menu_tracker
        menu_tracker.clear_all()

    def test_get_unknown_chat_returns_none(self) -> None:
        from aemr_bot.utils import menu_tracker
        assert menu_tracker.get_last_menu_mid(123) is None

    def test_set_and_get(self) -> None:
        from aemr_bot.utils import menu_tracker
        menu_tracker.set_last_menu_mid(123, "mid-A")
        assert menu_tracker.get_last_menu_mid(123) == "mid-A"

    def test_per_chat_isolation(self) -> None:
        from aemr_bot.utils import menu_tracker
        menu_tracker.set_last_menu_mid(123, "mid-A")
        menu_tracker.set_last_menu_mid(456, "mid-B")
        assert menu_tracker.get_last_menu_mid(123) == "mid-A"
        assert menu_tracker.get_last_menu_mid(456) == "mid-B"

    def test_overwrites(self) -> None:
        from aemr_bot.utils import menu_tracker
        menu_tracker.set_last_menu_mid(123, "mid-A")
        menu_tracker.set_last_menu_mid(123, "mid-B")
        assert menu_tracker.get_last_menu_mid(123) == "mid-B"

    def test_clear_single_chat(self) -> None:
        from aemr_bot.utils import menu_tracker
        menu_tracker.set_last_menu_mid(123, "mid-A")
        menu_tracker.set_last_menu_mid(456, "mid-B")
        menu_tracker.clear(123)
        assert menu_tracker.get_last_menu_mid(123) is None
        assert menu_tracker.get_last_menu_mid(456) == "mid-B"

    def test_clear_all(self) -> None:
        from aemr_bot.utils import menu_tracker
        menu_tracker.set_last_menu_mid(123, "mid-A")
        menu_tracker.set_last_menu_mid(456, "mid-B")
        menu_tracker.clear_all()
        assert menu_tracker.get_last_menu_mid(123) is None
        assert menu_tracker.get_last_menu_mid(456) is None

    def test_auto_evicts_oldest_chat_when_cap_exceeded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aemr_bot.utils import menu_tracker

        monkeypatch.setattr(menu_tracker, "_MAX_CHAT_STATES", 2, raising=False)
        now_values = iter([100.0, 101.0, 102.0])
        monkeypatch.setattr(menu_tracker, "_now", lambda: next(now_values), raising=False)

        menu_tracker.set_last_menu_mid(1, "mid-oldest")
        menu_tracker.set_last_menu_mid(2, "mid-middle")
        menu_tracker.set_last_menu_mid(3, "mid-newest")

        assert menu_tracker.get_last_menu_mid(1) is None
        assert menu_tracker.get_last_menu_mid(2) == "mid-middle"
        assert menu_tracker.get_last_menu_mid(3) == "mid-newest"

    def test_evict_stale_removes_old_chat_states(self) -> None:
        from aemr_bot.utils import menu_tracker

        menu_tracker.set_last_menu_mid(1, "mid-old")
        menu_tracker.set_last_menu_mid(2, "mid-fresh")
        old_state = menu_tracker.get_chat_state(1)
        fresh_state = menu_tracker.get_chat_state(2)
        assert old_state is not None
        assert fresh_state is not None
        old_state.updated_at = 10.0
        fresh_state.updated_at = 100.0

        removed = menu_tracker.evict_stale(
            now=110.0,
            max_age_sec=50.0,
            max_entries=10,
        )

        assert removed == 1
        assert menu_tracker.get_last_menu_mid(1) is None
        assert menu_tracker.get_last_menu_mid(2) == "mid-fresh"


# ---- send_or_edit_screen — поведение edit vs send new ----------------------


class TestSendOrEditScreenWithTracker:
    def setup_method(self) -> None:
        from aemr_bot.utils import menu_tracker
        menu_tracker.clear_all()

    @pytest.mark.asyncio
    async def test_no_callback_sends_new(self) -> None:
        """Не-callback (команда `/op_help`) → шлёт новое сообщение,
        даже если в tracker'е что-то есть. Tracker обновляется."""
        from aemr_bot.utils import event as event_mod
        from aemr_bot.utils import menu_tracker

        menu_tracker.set_last_menu_mid(555, "stale-mid")
        e = _make_event(chat_id=555, callback_mid=None)

        await event_mod.send_or_edit_screen(e, text="hi", attachments=[])

        e.bot.send_message.assert_awaited_once()
        e.bot.edit_message.assert_not_called()
        # tracker обновился на новый mid
        assert menu_tracker.get_last_menu_mid(555) == "new-server-assigned-mid"

    @pytest.mark.asyncio
    async def test_callback_with_fresh_mid_edits(self) -> None:
        """Callback пришёл от ПОСЛЕДНЕЙ карточки (mid в tracker'е)
        → edit (не send new). Это «нормальный» путь listing → detail."""
        from aemr_bot.utils import event as event_mod
        from aemr_bot.utils import menu_tracker

        menu_tracker.set_last_menu_mid(555, "current-menu-mid")
        e = _make_event(chat_id=555, callback_mid="current-menu-mid")

        await event_mod.send_or_edit_screen(e, text="hi", attachments=[])

        e.bot.edit_message.assert_awaited_once()
        e.bot.send_message.assert_not_called()
        # Tracker остаётся (edit сохраняет mid).
        assert menu_tracker.get_last_menu_mid(555) == "current-menu-mid"

    @pytest.mark.asyncio
    async def test_callback_with_stale_mid_sends_new(self) -> None:
        """Callback пришёл от СТАРОЙ карточки (mid ≠ tracker) → send new,
        не edit. Это и есть тот UX-фикс — старая карточка выше по чату
        не редактируется, появляется свежее меню внизу."""
        from aemr_bot.utils import event as event_mod
        from aemr_bot.utils import menu_tracker

        menu_tracker.set_last_menu_mid(555, "current-menu-mid")
        # Оператор кликнул на старую карточку выше
        e = _make_event(chat_id=555, callback_mid="OLD-card-mid")

        await event_mod.send_or_edit_screen(e, text="hi", attachments=[])

        # send_message, не edit — это и есть policy
        e.bot.send_message.assert_awaited_once()
        e.bot.edit_message.assert_not_called()
        # tracker обновился на новую (свежую) карточку
        assert menu_tracker.get_last_menu_mid(555) == "new-server-assigned-mid"

    @pytest.mark.asyncio
    async def test_force_new_message_bypasses_tracker(self) -> None:
        """`force_new_message=True` всегда шлёт новое — для случаев,
        когда вызывающий явно хочет создать новую карточку (например,
        wizard-completed → confirm-message новое)."""
        from aemr_bot.utils import event as event_mod
        from aemr_bot.utils import menu_tracker

        menu_tracker.set_last_menu_mid(555, "current-menu-mid")
        e = _make_event(chat_id=555, callback_mid="current-menu-mid")

        await event_mod.send_or_edit_screen(
            e, text="hi", attachments=[], force_new_message=True
        )

        e.bot.send_message.assert_awaited_once()
        e.bot.edit_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_callback_empty_tracker_initial_send(self) -> None:
        """Чистый старт: tracker пуст, не-callback → send new + tracker
        инициализируется новым mid. Это первый /op_help после рестарта."""
        from aemr_bot.utils import event as event_mod
        from aemr_bot.utils import menu_tracker

        # tracker уже очищен setup_method
        e = _make_event(chat_id=555, callback_mid=None)

        await event_mod.send_or_edit_screen(e, text="hi", attachments=[])

        e.bot.send_message.assert_awaited_once()
        assert menu_tracker.get_last_menu_mid(555) == "new-server-assigned-mid"

    @pytest.mark.asyncio
    async def test_callback_after_restart_with_empty_tracker_sends_new(self) -> None:
        """После рестарта процесса tracker пуст. Если оператор кликает
        на существующую карточку → tracker пуст ≠ callback_mid → send
        new. Это graceful behavior, без падений."""
        from aemr_bot.utils import event as event_mod

        e = _make_event(chat_id=555, callback_mid="pre-restart-mid")

        await event_mod.send_or_edit_screen(e, text="hi", attachments=[])

        e.bot.send_message.assert_awaited_once()
        e.bot.edit_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_edit_failure_falls_back_to_send(self) -> None:
        """Существующее поведение fallback'а сохранено: если edit_message
        бросил исключение (например, карточка слишком старая для MAX),
        send_or_edit_screen всё равно шлёт новое сообщение и обновляет
        tracker. Это не новый контракт — он был раньше; тест-страховка."""
        from aemr_bot.utils import event as event_mod
        from aemr_bot.utils import menu_tracker

        menu_tracker.set_last_menu_mid(555, "current-menu-mid")
        e = _make_event(chat_id=555, callback_mid="current-menu-mid")
        e.bot.edit_message = AsyncMock(side_effect=RuntimeError("edit denied"))

        await event_mod.send_or_edit_screen(e, text="hi", attachments=[])

        e.bot.send_message.assert_awaited_once()
        # tracker обновился на новый mid после fallback'а
        assert menu_tracker.get_last_menu_mid(555) == "new-server-assigned-mid"

    @pytest.mark.asyncio
    async def test_explicit_chat_id_kwarg_uses_correct_tracker_entry(self) -> None:
        """chat_id явно передан в kwargs (а не из event) — tracker
        обновляется по нему. Это случай служебной группы: callback
        пришёл от оператора (его chat_id = личный), но экран рисуем
        в админ-группе (явный chat_id=cfg.admin_group_id)."""
        from aemr_bot.utils import event as event_mod
        from aemr_bot.utils import menu_tracker

        e = _make_event(chat_id=999, callback_mid=None)
        admin_chat = -1234567

        await event_mod.send_or_edit_screen(
            e, text="hi", attachments=[], chat_id=admin_chat
        )

        # tracker НЕ обновлён для event-chat_id, а обновлён для явного admin_chat
        assert menu_tracker.get_last_menu_mid(999) is None
        assert menu_tracker.get_last_menu_mid(admin_chat) == "new-server-assigned-mid"
