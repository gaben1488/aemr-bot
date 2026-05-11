"""Прогресс-карта FSM-воронки приёма обращения.

Заменяет 5 отдельных echo-сообщений (NAME_RECEIVED / LOCALITY_RECEIVED /
ADDRESS_RECEIVED / TOPIC_RECEIVED + summary-prompt) **одним**
постоянно-обновляемым через MAX `edit_message`.

Идея:
- В `dialog_data` хранится `progress_message_id` — mid сообщения,
  которое мы редактируем при переходе на следующий шаг.
- Каждый шаг вызывает `render_progress(stage, ...)` — получает HTML-
  отформатированный текст с цветным баром, моноширинным счётчиком,
  bold-значениями и blockquote-подсказкой текущего шага.
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

# Цветные квадраты вместо ASCII ▓░ — крупнее, читаются на маленьких
# экранах, единая высота на любом клиенте MAX (Android/iOS/Web).
# Зелёный = пройдено, синий = текущий, белый = впереди.
_BAR_DONE = "🟢"
_BAR_CURRENT = "🟦"
_BAR_FUTURE = "⬜"

# Иконки строк-шагов
_DONE = "✓"
_CURRENT = "▶"
_FUTURE = "○"


def _esc(value: str | None) -> str:
    """HTML-escape значений жителя. Имя «<script>» или адрес с & не
    должны ломать рендер. quote=False — кавычки внутри text-узла
    не интерпретируются MAX-парсером, экранировать их вредно для UX."""
    return html.escape((value or "").strip(), quote=False)


def _render_bar(current_idx: int, total: int) -> str:
    """Строка из total квадратов: done (🟢) до current_idx, current (🟦)
    в позиции current_idx, future (⬜) после."""
    parts: list[str] = []
    for i in range(total):
        if i < current_idx:
            parts.append(_BAR_DONE)
        elif i == current_idx:
            parts.append(_BAR_CURRENT)
        else:
            parts.append(_BAR_FUTURE)
    return "".join(parts)


def render_progress(
    stage: Stage,
    *,
    name: str | None = None,
    locality: str | None = None,
    address: str | None = None,
    topic: str | None = None,
) -> str:
    """HTML-отформатированный текст прогресс-карты.

    Структура:
        📋 <b>Подача обращения</b>
        🟢🟢🟦⬜⬜  <code>3 / 5</code>

        ✓ Имя · <b>Иван</b>
        ✓ Населённый пункт · <b>Елизовское ГП</b>
        ▶ <b>Адрес</b>
        <blockquote>Укажите адрес — улица и дом</blockquote>
        ○ Тема
        ○ Суть

    Используется MAX `format=ParseMode.HTML`. Значения жителя
    пропускаются через html.escape — иначе «<script>» в имени или
    & в адресе сломают парсинг.

    Аргументы name/locality/address/topic — значения уже введённых
    шагов. Передаются only те, что уже завершены.
    """
    try:
        current_idx = _STAGES.index(stage)
    except ValueError as e:
        raise ValueError(f"unknown stage {stage!r}") from e

    total = len(_STAGES)
    bar = _render_bar(current_idx, total)
    counter = f"<code>{current_idx + 1} / {total}</code>"
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
            value = _esc(values[st]) or "—"
            lines.append(f"{_DONE} {label} · <b>{value}</b>")
        elif i == current_idx:
            prompt = _STAGE_PROMPTS[st]
            lines.append(f"{_CURRENT} <b>{label}</b>")
            lines.append(f"<blockquote>{prompt}</blockquote>")
        else:
            lines.append(f"{_FUTURE} {label}")

    header = f"📋 <b>Подача обращения</b>\n{bar}  {counter}"
    return header + "\n\n" + "\n".join(lines)


async def send_or_edit_progress(
    bot,
    *,
    chat_id: int | None,
    dialog_data: dict,
    text: str,
    attachments: list,
) -> tuple[str | None, bool]:
    """Отправить или отредактировать прогресс-сообщение в HTML-режиме.

    Поведение:
    - Если в `dialog_data['progress_message_id']` есть mid — пробуем
      edit_message. При успехе возвращаем (mid, edited=True).
    - Если edit упал (API error, message deleted) — шлём новое
      сообщение, возвращаем (новый_mid, edited=False).
    - Если в dialog_data нет mid — сразу шлём новое сообщение.

    `format=ParseMode.HTML` передаётся всегда — render_progress
    выдаёт HTML-разметку. Если из maxapi нельзя импортировать enum
    (будущие версии без bc), молча шлём без format — текст всё равно
    пройдёт, просто с видимыми тегами в худшем случае.

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

    existing_mid = dialog_data.get("progress_message_id") if dialog_data else None

    # Если адрес был определён через геолокацию, подтверждающий geo-экран
    # остаётся отдельным сообщением с inline-кнопками. Переход к тематике
    # через edit_message визуально выглядит как «кнопка ничего не сделала»:
    # житель остаётся глазами на старом geo-сообщении и повторно жмёт
    # уже устаревшие geo:* callback'и, которые FSM корректно игнорирует.
    # Поэтому именно экран «Тема» после geo-flow отправляем новым сообщением.
    if (
        existing_mid
        and dialog_data.get("detected_locality")
        and "▶ <b>Тема</b>" in text
    ):
        log.info(
            "send_or_edit_progress: force new topic message after geo-flow, "
            "old progress_message_id=%s",
            existing_mid,
        )
        existing_mid = None

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

    # Fallback / первый раз: новое сообщение
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            attachments=attachments,
            format=fmt,
        )
    except Exception:
        log.exception("send_or_edit_progress: send_message failed too")
        return None, False
    new_mid = extract_message_id(sent)
    return new_mid, False
