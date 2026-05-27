"""Per-chat tracker с dual-state: `last_physical_mid` (что физически
последнее в чате) отдельно от `last_editable_mid` + `last_editable_kind`
(какую карточку и какого вида разрешено редактировать).

Edit разрешён только при совпадении всех трёх условий — physical, editable
и kind. Иначе send_new и оба tracker'а сдвигаются на новый mid.

In-memory dict, после рестарта tracker пуст (graceful: первое нажатие
после рестарта даёт send_new). Старый API `get/set_last_menu_mid`
оставлен как обёртка над dual-tracker для совместимости.

Полная мотивация, контракт и история conflicts: см.
`docs/_meta/_archive/CODE_DECISIONS_LOG.md §1`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EditableKind = Literal["menu", "wizard", "progress", "listing"]


@dataclass
class ChatState:
    """Тройка состояний на чат. Все поля Optional — None означает «бот
    ещё не отправлял ничего в этот чат» (cold start)."""

    last_physical_mid: str | None = None
    last_editable_mid: str | None = None
    last_editable_kind: EditableKind | None = None


_state_by_chat: dict[int, ChatState] = {}


# ───────────────────── Новое явное API ─────────────────────


def _state_for(chat_id: int) -> ChatState:
    """Получить (или создать) ChatState для чата."""
    state = _state_by_chat.get(chat_id)
    if state is None:
        state = ChatState()
        _state_by_chat[chat_id] = state
    return state


def note_event(chat_id: int, mid: str) -> None:
    """Зарегистрировать historic event в чате — двигает ТОЛЬКО physical mid.

    Используется для:
    - CITIZEN_REPLY жителю.
    - APPEAL_ACCEPTED после finalize.
    - audit-уведомлений в admin chat (pulse, retention, deactivation).
    - broadcast progress кадров.
    - admin_card.render (карточка обращения — sacred event log).
    - Любое сообщение бота, которое не является редактируемой
      меню-карточкой.

    Editable mid НЕ двигается — clicks по кнопкам на historic event
    дадут send_new menu, event остаётся в чате.
    """
    state = _state_for(chat_id)
    state.last_physical_mid = mid


def note_editable_send(
    chat_id: int, mid: str, kind: EditableKind = "menu"
) -> None:
    """Зарегистрировать отправку редактируемой карточки — двигает ОБА mid.

    Используется когда бот шлёт новый экран меню/wizard, который можно
    редактировать при следующем тапе кнопки. После этого:
    - `last_physical_mid` = mid (карточка теперь физически последняя).
    - `last_editable_mid` = mid (она же редактируемая).
    - `last_editable_kind` = kind.

    Каллер должен указывать `kind` явно (по умолчанию `menu`), чтобы
    edit-чек разрешил только смену экрана той же категории.
    """
    state = _state_for(chat_id)
    state.last_physical_mid = mid
    state.last_editable_mid = mid
    state.last_editable_kind = kind


def note_incoming(chat_id: int, mid: str) -> None:
    """Зарегистрировать входящее сообщение пользователя — двигает ТОЛЬКО
    physical mid.

    Используется middleware'ом `AdminChatActivityMiddleware` на каждое
    MessageCreated в admin chat. Editable mid не двигается — клик
    оператора по старой карточке-меню всё ещё должен редактировать
    её, если она была последней редактируемой (но callback_mid !=
    physical → freshness откажет).
    """
    state = _state_for(chat_id)
    state.last_physical_mid = mid


def can_edit(
    chat_id: int, callback_mid: str | None, kind: EditableKind = "menu"
) -> bool:
    """Проверить, разрешён ли edit для callback'а.

    Три условия одновременно:
    1. callback_mid == last_physical_mid (физически последняя).
    2. callback_mid == last_editable_mid (была редактируемой).
    3. kind == last_editable_kind (caller показывает экран той же
       категории).

    Если хотя бы одно False — edit не разрешён, caller должен сделать
    send_new + позвать `note_editable_send` для регистрации нового
    редактируемого экрана.
    """
    if callback_mid is None:
        return False
    state = _state_by_chat.get(chat_id)
    if state is None:
        return False
    return (
        callback_mid == state.last_physical_mid
        and callback_mid == state.last_editable_mid
        and kind == state.last_editable_kind
    )


def get_chat_state(chat_id: int) -> ChatState | None:
    """Доступ к полному состоянию чата — для тестов и диагностики."""
    return _state_by_chat.get(chat_id)


def clear(chat_id: int) -> None:
    """Забыть состояние чата."""
    _state_by_chat.pop(chat_id, None)


def clear_all() -> None:
    """Очистить весь tracker — используется в тестах для изоляции."""
    _state_by_chat.clear()


# ───────────────────── Старое API (совместимость) ─────────────────────


def get_last_menu_mid(chat_id: int) -> str | None:
    """СОВМЕСТИМОСТЬ: вернуть last_editable_mid.

    Старый API. Новый код должен использовать `can_edit()` напрямую —
    это правильнее, потому что проверяет также physical и kind.
    Старый код работает с предположением «если editable mid совпал —
    можно edit», что эквивалентно нашему `can_edit(kind='menu')` для
    случаев, где kind не различается.
    """
    state = _state_by_chat.get(chat_id)
    return state.last_editable_mid if state else None


def set_last_menu_mid(chat_id: int, mid: str) -> None:
    """СОВМЕСТИМОСТЬ: двигает ОБА mid (physical + editable) с kind='menu'.

    Старый API. По смыслу старые caller'ы (send_or_edit_screen,
    _send_or_edit_menu, admin_bus.send) звали это только когда отправляли
    меню или wizard — то есть редактируемую карточку. Поэтому эквивалент
    `note_editable_send(chat_id, mid, kind='menu')`.

    Если caller хотел зарегистрировать historic event (без edit), он
    должен использовать `note_event` вместо этого.
    """
    note_editable_send(chat_id, mid, kind="menu")
