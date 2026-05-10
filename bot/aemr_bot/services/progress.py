"""Прогресс-карта FSM-воронки приёма обращения.

Заменяет 5 отдельных echo-сообщений (NAME_RECEIVED / LOCALITY_RECEIVED /
ADDRESS_RECEIVED / TOPIC_RECEIVED + summary-prompt) **одним**
постоянно-обновляемым через MAX `edit_message`.

Идея:
- В `dialog_data` хранится `progress_message_id` — mid сообщения,
  которое мы редактируем при переходе на следующий шаг.
- Каждый шаг вызывает `render_progress(stage, ...)` — получает текст
  с прогресс-баром «▓▓▓░░ Шаг 3/5» и списком шагов с галочками.
- Helper `send_or_edit_progress(bot, chat_id, dialog_data, text,
  attachments)` либо редактирует существующее сообщение, либо шлёт
  новое (fallback при API-ошибке).

Pure-функция `render_progress` — тестируется без моков MAX.
"""
from __future__ import annotations

import logging
from typing import Literal

log = logging.getLogger(__name__)

Stage = Literal["name", "locality", "address", "topic", "summary"]

# Порядок шагов воронки. Имя идёт после контакта (контакт — отдельный
# pre-step, не показывается в прогрессе). Шаги из этого списка —
# те, на которых строится прогресс-бар.
_STAGES: tuple[Stage, ...] = ("name", "locality", "address", "topic", "summary")
_STAGE_LABELS: dict[Stage, str] = {
    "name": "Имя",
    "locality": "Населённый пункт",
    "address": "Адрес",
    "topic": "Тема",
    "summary": "Суть",
}
_STAGE_PROMPTS: dict[Stage, str] = {
    "name": "Введите ваше имя одним сообщением",
    "locality": "Выберите населённый пункт ниже",
    "address": "Укажите адрес — улица и дом",
    "topic": "Выберите тему обращения ниже",
    "summary": (
        "Опишите суть обращения одним сообщением. К нему можно "
        "приложить фото, видео или файл — пришлите вместе с текстом"
    ),
}

_PROGRESS_FILLED = "▓"
_PROGRESS_EMPTY = "░"

# Иконки шагов
_DONE = "✅"
_CURRENT = "🔵"
_FUTURE = "⚪"


def _progress_bar(current_idx: int, total: int) -> str:
    """«▓▓▓░░» где current_idx = количество ЗАВЕРШЁННЫХ шагов
    (на котором сейчас стоит пользователь = current_idx; всё что до —
    completed)."""
    return _PROGRESS_FILLED * current_idx + _PROGRESS_EMPTY * (total - current_idx)


def render_progress(
    stage: Stage,
    *,
    name: str | None = None,
    locality: str | None = None,
    address: str | None = None,
    topic: str | None = None,
) -> str:
    """Текст прогресс-карты для текущего шага.

    Логика:
    - Шаги до текущего = ✅ + значение (что ввёл житель)
    - Текущий шаг = 🔵 + лейбл + подсказка что сделать
    - Будущие шаги = ⚪ + лейбл (без подсказок)

    Аргументы name/locality/address/topic — значения уже введённых
    шагов. Передаются only те, что уже завершены.
    """
    try:
        current_idx = _STAGES.index(stage)
    except ValueError as e:
        raise ValueError(f"unknown stage {stage!r}") from e

    total = len(_STAGES)
    bar = _progress_bar(current_idx, total)
    values: dict[Stage, str | None] = {
        "name": name,
        "locality": locality,
        "address": address,
        "topic": topic,
        "summary": None,
    }

    lines: list[str] = []
    for i, st in enumerate(_STAGES):
        label = _STAGE_LABELS[st]
        if i < current_idx:
            value = (values[st] or "").strip() or "—"
            lines.append(f"{_DONE} {label}: {value}")
        elif i == current_idx:
            prompt = _STAGE_PROMPTS[st]
            lines.append(f"{_CURRENT} {label} — {prompt}")
        else:
            lines.append(f"{_FUTURE} {label}")

    header = f"📋 Подача обращения\n{bar}  Шаг {current_idx + 1}/{total}"
    return header + "\n\n" + "\n".join(lines)


async def send_or_edit_progress(
    bot,
    *,
    chat_id: int | None,
    dialog_data: dict,
    text: str,
    attachments: list,
) -> tuple[str | None, bool]:
    """Отправить или отредактировать прогресс-сообщение.

    Поведение:
    - Если в `dialog_data['progress_message_id']` есть mid — пробуем
      edit_message. При успехе возвращаем (mid, edited=True).
    - Если edit упал (API error, message deleted) — шлём новое
      сообщение, возвращаем (новый_mid, edited=False).
    - Если в dialog_data нет mid — сразу шлём новое сообщение.

    Caller должен сохранить возвращённый mid в dialog_data при
    edited=False (новое сообщение).

    Returns: (message_id, was_edited).
    """
    from aemr_bot.utils.event import extract_message_id

    existing_mid = dialog_data.get("progress_message_id") if dialog_data else None

    if existing_mid:
        try:
            await bot.edit_message(
                message_id=existing_mid,
                text=text,
                attachments=attachments,
            )
            return existing_mid, True
        except Exception:
            log.info(
                "send_or_edit_progress: edit_message %s failed, fallback to new",
                existing_mid,
                exc_info=False,
            )

    # Fallback / первый раз: новое сообщение
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            attachments=attachments,
        )
    except Exception:
        log.exception("send_or_edit_progress: send_message failed too")
        return None, False
    new_mid = extract_message_id(sent)
    return new_mid, False
