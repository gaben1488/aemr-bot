"""Human-readable setting for the internal first-response target."""
from secrets import token_hex

from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._common import op_screen, op_send
from aemr_bot.handlers.admin_settings_shared import _intent_get, _intent_set
from aemr_bot.services import operators, settings_store

_KEY = "sla_response_hours"


def _keyboard(*, editing: bool = False, token: str = ""):
    keyboard = InlineKeyboardBuilder()
    if editing:
        keyboard.row(CallbackButton(text="❌ Отмена", payload=f"op:set:sla:cancel:{token}"))
    else:
        keyboard.row(CallbackButton(text="✏️ Изменить", payload="op:set:sla:edit"))
        keyboard.row(CallbackButton(text="↩️ К настройкам", payload="op:settings"))
    return keyboard.as_markup()


def _effective(value) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 168:
        return value
    return cfg.sla_response_hours


async def show(event, *, notice: str | None = None) -> None:
    async with session_scope() as session:
        value = await settings_store.get(session, _KEY)
    await op_screen(event,
        "⏱ Первый ответ\n\n"
        f"Внутренний ориентир для новых периодов: {_effective(value)} рабочих ч.\n"
        "Учитываются рабочие часы, обед и календарь Камчатки. "
        "Изменение действует сразу для новых и вновь открытых обращений; "
        "норматив уже начатого периода не меняется. Для старых записей, "
        "где он не был сохранён, используется значение конфигурации. "
        "Законные сроки рассматриваются отдельно.\n"
        + (f"\n{notice}" if notice else ""), _keyboard())


async def start(event, operator_id: int) -> None:
    async with session_scope() as session:
        baseline, revision = await settings_store.get_versioned(session, _KEY)
    token = token_hex(6)
    _intent_set(operator_id, key=_KEY, kind="sla_hours", baseline=baseline, revision=revision, token=token)
    await op_screen(event,
        f"⏱ Изменить срок первого ответа\n\nСейчас: {_effective(baseline)} рабочих ч.\n"
        "Пришлите целое число от 1 до 168. Изменение затронет только новые периоды.",
        _keyboard(editing=True, token=token))


async def apply(event, operator_id: int, text: str) -> bool:
    intent = _intent_get(operator_id)
    token = intent.get("token", "") if intent and intent.get("kind") == "sla_hours" else ""
    try:
        value = int(text.strip())
    except ValueError:
        await op_send(event, "Введите целое число от 1 до 168.", _keyboard(editing=True, token=token))
        return False
    if not 1 <= value <= 168:
        await op_send(event, "Допустимый диапазон — от 1 до 168 рабочих часов.", _keyboard(editing=True, token=token))
        return False
    intent = _intent_get(operator_id)
    if not intent or intent.get("kind") != "sla_hours" or intent.get("revision") is None:
        await show(event, notice="Редактирование устарело. Откройте его заново.")
        return True
    try:
        async with session_scope() as session:
            await settings_store.set_value(session, _KEY, value, expected_revision=intent["revision"])
            await operators.write_audit(session, operator_max_user_id=operator_id,
                                        action="setting_update", target=_KEY,
                                        details={"before": _effective(intent.get("baseline")), "after": value})
    except settings_store.SettingConflictError:
        await show(event, notice="Настройка уже изменилась. Откройте редактирование заново.")
        return True
    await show(event, notice="Срок сохранён для новых периодов.")
    return True
