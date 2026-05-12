"""Прогресс-карта FSM-воронки приёма обращения.

Заменяет 5 отдельных echo-сообщений (NAME_RECEIVED / LOCALITY_RECEIVED /
ADDRESS_RECEIVED / TOPIC_RECEIVED + summary-prompt) **одним**
постоянно-обновляемым через MAX `edit_message`.

Идея:
- В `dialog_data` хранится `progress_message_id` — mid сообщения,
  которое мы редактируем при переходе на следующий шаг.
- Каждый шаг вызывает `render_progress(stage, ...)` — получает HTML-
  отформатированный текст с коротким счётчиком, уже введёнными данными
  и подсказкой текущего шага.
- Helper `send_or_edit_progress(bot, chat_id, dialog_data, text,
  attachments)` либо редактирует существующее сообщение, либо шлёт
  новое (fallback при API-ошибке). Передаёт `format=ParseMode.HTML`.

Pure-функция `render_progress` — тестируется без моков MAX.
"""
from __future__ import annotations

import html
import logging
from typing import Literal

log = logging.getLogger(__name__)

Stage = Literal["name", "locality", "address", "topic", "summary"]

# Порядок шагов воронки. Имя идёт после контакта (контакт — отдельный
# pre-step, не показывается в прогрессе). Шаги из этого списка —
# те, на которых строится счётчик шага.
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

_DONE = "✓"
_CURRENT = "▶"


def _esc(value: str | None) -> str:
    """HTML-escape значений жителя. Имя «<script>» или адрес с & не
    должны ломать рендер. quote=False — кавычки внутри text-узла
    не интерпретируются MAX-парсером, экранировать их вредно для UX."""
    return html.escape((value or "").strip(), quote=False)


def render_progress(
    stage: Stage,
    *,
    name: str | None = None,
    locality: str | None = None,
    address: str | None = None,
    topic: str | None = None,
) -> str:
    """HTML-отформатированный текст прогресс-карты.

    Структура намеренно короткая: без цветных баров, квадратов, кружков
    и полного списка будущих этапов. На маленьких экранах MAX они
    создавали визуальный шум и выглядели сложнее самой воронки.

        📋 <b>Подача обращения</b> · <code>2 / 5</code>

        ✓ Имя · <b>Иван</b>
        ▶ <b>Населённый пункт</b>
        <blockquote>Выберите населённый пункт ниже</blockquote>

    Используется MAX `format=ParseMode.HTML`. Значения жителя
    пропускаются через html.escape — иначе «<script>» в имени или
    & в адресе сломают парсинг.
    """
    try:
        current_idx = _STAGES.index(stage)
    except ValueError as e:
        raise ValueError(f"unknown stage {stage!r}") from e

    counter = f"<code>{current_idx + 1} / {len(_STAGES)}</code>"
    values: dict[Stage, str | None] = {
        "name": name,
        "locality": locality,
        "address": address,
        "topic": topic,
        "summary": None,
    }

    lines: list[str] = []
    for st in _STAGES[:current_idx]:
        value = _esc(values[st]) or "—"
        lines.append(f"{_DONE} {_STAGE_LABELS[st]} · <b>{value}</b>")

    current_label = _STAGE_LABELS[stage]
    current_prompt = _STAGE_PROMPTS[stage]
    lines.append(f"{_CURRENT} <b>{current_label}</b>")
    lines.append(f"<blockquote>{current_prompt}</blockquote>")

    header = f"📋 <b>Подача обращения</b> · {counter}"
    return header + "\n\n" + "\n".join(lines)


async def send_or_edit_progress(
    bot,
    *,
    chat_id: int | None,
    user_id: int | None = None,
    dialog_data: dict,
    text: str,
    attachments: list,
    force_new_message: bool = False,
) -> tuple[str | None, bool]:
    """Отправить или отредактировать прогресс-сообщение в HTML-режиме.

    Поведение:
    - Если `force_new_message=True` — сразу шлём новое сообщение. Это
      используется для переходов, где редактирование старого экрана
      ухудшает UX: например, после geo-confirm старое сообщение с
      inline-кнопками остаётся визуально активным, и пользователи жмут
      устаревшие geo:* callback'и.
    - Если в `dialog_data['progress_message_id']` есть mid — пробуем
      edit_message. При успехе возвращаем (mid, edited=True).
    - Если edit упал (API error, message deleted) — шлём новое
      сообщение, возвращаем (новый_mid, edited=False).
    - Если в dialog_data нет mid — сразу шлём новое сообщение.

    `format=ParseMode.HTML` передаётся всегда — render_progress
    выдаёт HTML-разметку. Если из maxapi нельзя импортировать enum
    (будущие версии без bc), молча шлём без format — текст всё равно
    пройдёт, просто с видимыми тегами в худшем случае.

    Для MessageCallback MAX иногда не отдаёт `chat_id` в событии
    callback'а. Типичный симптом — житель нажал «✅ Всё правильно»
    после геолокации, состояние в БД перешло дальше, но новая карточка
    с темами не отправилась. Поэтому fallback-send использует `user_id`,
    если `chat_id` отсутствует.

    Caller должен сохранить возвращённый mid в dialog_data при
    edited=False (новое сообщение).

    Returns: (message_id, was_edited).
    """
    from aemr_bot.utils.event import extract_message_id

    try:
        from maxapi.enums.parse_mode import ParseMode

        fmt = ParseMode.HTML
    except Exception:  # pragma: no cover — защита от breaking changes
        fmt = None

    existing_mid = None if force_new_message else (
        dialog_data.get("progress_message_id") if dialog_data else None
    )

    if force_new_message:
        log.info("send_or_edit_progress: forced new progress message")

    if existing_mid:
        try:
            await bot.edit_message(
                message_id=existing_mid,
                text=text,
                attachments=attachments,
                format=fmt,
            )
            return existing_mid, True
        except Exception:
            log.info(
                "send_or_edit_progress: edit_message %s failed, fallback to new",
                existing_mid,
                exc_info=False,
            )

    # Fallback / первый раз: новое сообщение. В личных callback'ах
    # chat_id может отсутствовать, поэтому при его отсутствии отправляем
    # по user_id. Если нет обоих идентификаторов — это уже битое событие.
    if chat_id is None and user_id is None:
        log.error("send_or_edit_progress: no chat_id and no user_id for send_message")
        return None, False

    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            user_id=None if chat_id is not None else user_id,
            text=text,
            attachments=attachments,
            format=fmt,
        )
    except Exception:
        log.exception("send_or_edit_progress: send_message failed too")
        return None, False
    new_mid = extract_message_id(sent)
    return new_mid, False
