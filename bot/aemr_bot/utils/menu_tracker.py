"""Per-chat tracker «какая карточка-меню сейчас актуальна».

**Зачем:** `send_or_edit_screen` решал «edit vs send new» только по типу
события (callback → edit). Это ломалось когда оператор скроллил вверх
к давно отредактированной карточке-меню и кликал кнопку на ней — бот
редактировал далеко вверх по чату, а оператор не видел изменение
(находится внизу).

**Правило:** edit разрешён **только** если callback пришёл от того
же `mid`, что в tracker[chat_id]. Иначе шлём новое сообщение, и
tracker обновляется на новый mid.

**Не покрывается:**
- «Sacred» карточки (admin appeal card, citizen reply, broadcast
  progress, audit-уведомления, pulse, reminders) отправляются напрямую
  через `bot.send_message`, не через `send_or_edit_screen` — они НЕ
  трогают tracker. Edit на них через send_or_edit_screen не доходит
  (callback_mid не совпадёт с tracker'ом → send new).

**Хранение:** in-memory `dict[int, str]`. Однопроцессный бот. После
рестарта tracker пуст — первое нажатие callback пошлёт new (graceful,
без падений). Для multi-процессного бота понадобится Redis/DB; пока
single-process — in-memory достаточно.
"""
from __future__ import annotations

# chat_id → last mid of a menu-card sent/edited via send_or_edit_screen.
# Не используем `_LAST_MENU_MID` (с подчёркиванием) намеренно: модуль
# уже private по namespace'у `utils.menu_tracker`, а tests мокают
# через `menu_tracker.set_last_menu_mid` без обращения к внутреннему dict.
_chat_to_mid: dict[int, str] = {}


def get_last_menu_mid(chat_id: int) -> str | None:
    """Вернуть mid последней карточки-меню для чата, или None если
    бот ещё не отправлял меню в этот чат (например, после рестарта)."""
    return _chat_to_mid.get(chat_id)


def set_last_menu_mid(chat_id: int, mid: str) -> None:
    """Запомнить mid отправленной/отредактированной карточки-меню."""
    _chat_to_mid[chat_id] = mid


def clear(chat_id: int) -> None:
    """Забыть mid для чата. Используется редко — в основном для тестов."""
    _chat_to_mid.pop(chat_id, None)


def clear_all() -> None:
    """Очистить весь tracker. Используется в тестах для изоляции."""
    _chat_to_mid.clear()
