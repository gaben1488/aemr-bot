"""Edit the working window atomically, with revision checks and a period snapshot."""
from secrets import token_hex

from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from aemr_bot.db.session import session_scope
from aemr_bot.handlers._common import op_screen, op_send
from aemr_bot.handlers.admin_settings_shared import _intent_get, _intent_set
from aemr_bot.services import operators, settings_store, work_schedule


def _keyboard(*, token: str | None = None, revision: int = 0):
    kb = InlineKeyboardBuilder()
    if token is not None:
        kb.row(CallbackButton(text="❌ Отмена", payload=f"op:set:schedule:cancel:{token}"))
    else:
        kb.row(CallbackButton(text="✏️ Часы работы и обеда", payload="op:set:schedule:edit"))
        kb.row(CallbackButton(text="📅 Взять праздники из файла", payload=f"op:set:schedule:calendar:{revision}"))
        kb.row(CallbackButton(text="⏱ Внутренние сроки", payload="op:set:sla"))
        kb.row(CallbackButton(text="↩️ К настройкам", payload="op:settings"))
    return kb.as_markup()


def _hours(value: dict) -> str:
    return (f"{value['start']:02d}:00–{value['end']:02d}:00; "
            f"обед {value['lunch_start']:02d}:00–{value['lunch_end']:02d}:00")


async def show(event, *, notice: str = "") -> None:
    async with session_scope() as session:
        stored, revision = await settings_store.get_versioned(session, work_schedule.KEY)
        value = await work_schedule.snapshot(session)
    await op_screen(event,
        "🕘 Рабочее время\n\n"
        f"Понедельник–пятница: {_hours(value)}.\n"
        f"Часовой пояс: {value['timezone']}.\n"
        f"Исключено праздничных дат: {len(value['holidays'])}.\n"
        f"Источник: {'настройка в базе' if stored is not None else 'конфигурация запуска'}.\n\n"
        "Изменение часов сразу применяется к регулярным напоминаниям и новым периодам обращений. "
        "Уже начатые периоды сохраняют своё расписание и календарь, в том числе после перезапуска. "
        "Для старых периодов без снимка закреплено расписание первого запуска этой версии; "
        "это оперативный ориентир, а не достоверная историческая оценка.\n\n"
        "Календарь из файла читается при запуске. Кнопка ниже обновляет праздничные даты "
        "для новых периодов и напоминаний. Снимки прошлых периодов не меняются. "
        "Часовой пояс сохраняется вместе с расписанием; часы здесь вводятся в нём.\n"
        + (f"\n{notice}" if notice else ""), _keyboard(revision=revision))


async def start(event, operator_id: int) -> None:
    async with session_scope() as session:
        _, revision = await settings_store.get_versioned(session, work_schedule.KEY)
        baseline = await work_schedule.snapshot(session)
    token = token_hex(6)
    _intent_set(operator_id, key=work_schedule.KEY, kind="work_schedule",
                baseline=baseline, revision=revision, token=token)
    await op_screen(event,
        f"🕘 Сейчас: {_hours(baseline)} ({baseline['timezone']}).\n\n"
        "Пришлите четыре целых часа через пробел: начало работы, конец работы, "
        "начало обеда, конец обеда. Например: 9 18 12 13. "
        "Обед должен находиться в пределах рабочего дня. "
        "Час окончания работы может быть 24. Все четыре значения сохраняются вместе.",
        _keyboard(token=token))


async def _save(event, operator_id: int, value: dict, before: dict, revision: int) -> None:
    async with session_scope() as session:
        await settings_store.set_value(session, work_schedule.KEY, value, expected_revision=revision)
        await operators.write_audit(session, operator_max_user_id=operator_id,
                                    action="setting_update", target=work_schedule.KEY,
                                    details={"before": before, "after": value})
    # Publish only after the write and its audit have both committed.
    async with session_scope() as session:
        await work_schedule.refresh(session)


async def apply(event, operator_id: int, text: str) -> bool:
    intent = _intent_get(operator_id)
    if not intent or intent.get("kind") != "work_schedule":
        await show(event, notice="Редактирование устарело. Откройте его заново.")
        return True
    value = dict(intent["baseline"])
    try:
        parts = text.split()
        if len(parts) != 4:
            raise ValueError("Введите четыре целых часа через пробел, например: 9 18 12 13.")
        try:
            hours = [int(part) for part in parts]
        except ValueError as exc:
            raise ValueError("Введите целые часы, например: 9 18 12 13.") from exc
        value.update(zip(("start", "end", "lunch_start", "lunch_end"), hours, strict=True))
        work_schedule.validate(value)
    except ValueError as exc:
        await op_send(event, str(exc), _keyboard(token=intent["token"]))
        return False
    try:
        await _save(event, operator_id, value, intent["baseline"], intent["revision"])
    except settings_store.SettingConflictError:
        await show(event, notice="Расписание уже изменилось. Откройте редактирование заново.")
        return True
    await show(event, notice="Расписание сохранено. Напоминания и новые периоды используют новые часы.")
    return True


async def update_calendar(event, operator_id: int, revision: int) -> None:
    async with session_scope() as session:
        before = await work_schedule.snapshot(session)
    value = dict(before, holidays=work_schedule.environment()["holidays"])
    try:
        await _save(event, operator_id, value, before, revision)
    except settings_store.SettingConflictError:
        await show(event, notice="Кнопка устарела. Откройте расписание заново.")
        return
    await show(event, notice="Праздничные даты обновлены для новых периодов и напоминаний.")
