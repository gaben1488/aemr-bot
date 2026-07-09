"""Типизированный источник построения callback payload'ов.

Этот модуль — единственное место, где собираются строки callback'ов для
inline-кнопок. Он зеркалит реестр маршрутов :mod:`handlers.callback_router`:
на каждый EXACT-маршрут здесь есть строковая КОНСТАНТА, на каждый
PREFIX-маршрут — функция-БИЛДЕР с аннотацией типа аргумента.

Зачем отдельный модуль вместо сырых f-строк в ``ui/*.py``:

* **wire-совместимость.** Эмитируемые строки байт-в-байт совпадают с тем,
  что роутер ожидает увидеть. Контракт-тест
  ``tests/test_callback_payloads_contract.py`` пинит это к реестру, так что
  класс ошибок «кнопка строит ``op:reploy:5`` вместо ``op:reply:5``»
  ловится на CI, а не в проде.
* **типобезопасность.** Билдер ``op_reply(appeal_id: int)`` не даст молча
  подставить, например, объект или ``None`` — нужное приведение к ``str``
  происходит ровно в одном месте.

Намеренно НЕ Pydantic-класс-на-каждый-callback. Бот — MLP, не enterprise:
простые ``str``-константы и тонкие билдеры читаются и проверяются легче, чем
30+ моделей. Семантику (группа, admin_allowed, парсинг хвоста) держит
:mod:`handlers.callback_router`; здесь — только построение строки.

Соглашение о хвостах (видно из ``ui/*.py``):

* числовой хвост — id обращения / max_user_id / номер страницы. Разбирается
  обратно через :func:`handlers.callback_router.parse_int_tail`.
* строковый хвост — путь меню (``op:set:``), действие (``op:aud:``,
  ``op:opadd:``, ``op:tmpl:``) или ключ настройки (``op:setkey:``). Это
  свободная форма, билдер только приклеивает префикс.
"""
from __future__ import annotations

from enum import StrEnum


class CallbackGroup(StrEnum):
    """Логическая группа callback'а — зеркало одноимённого enum в роутере.

    Дублируется здесь, чтобы агенты миграции и тесты могли группировать
    константы/билдеры по смыслу, не завязываясь на структуру роутера.
    Сопоставление со ``callback_router.CallbackGroup`` проверяет
    контракт-тест.
    """

    CITIZEN_FLOW = "citizen_flow"
    GEO_FLOW = "geo_flow"
    BROADCAST_ADMIN = "broadcast_admin"
    OPERATOR_ADMIN = "operator_admin"
    MENU_FALLBACK = "menu_fallback"


# ════════════════════════════════════════════════════════════════════════
# EXACT-константы. Строка == зарегистрированный паттерн EXACT_ROUTES.
# ════════════════════════════════════════════════════════════════════════

# ── CITIZEN_FLOW: навигация по меню жителя ──
MENU_MAIN = "menu:main"
MENU_NEW_APPEAL = "menu:new_appeal"
MENU_MY_APPEALS = "menu:my_appeals"
MENU_USEFUL_INFO = "menu:useful_info"
MENU_APPOINTMENT = "menu:appointment"
MENU_SETTINGS = "menu:settings"
MENU_SECURITY = "menu:security"

# ── CITIZEN_FLOW: воронка обращения ──
CONSENT_YES = "consent:yes"
CONSENT_NO = "consent:no"
CANCEL = "cancel"
ADDR_REUSE = "addr:reuse"
ADDR_NEW = "addr:new"
APPEAL_SUBMIT = "appeal:submit"

# ── GEO_FLOW: подтверждение гео-адреса ──
GEO_CONFIRM = "geo:confirm"
GEO_EDIT_ADDRESS = "geo:edit_address"
GEO_OTHER_LOCALITY = "geo:other_locality"

# ── CITIZEN_FLOW: «Полезная информация» ──
INFO_EMERGENCY = "info:emergency"
INFO_DISPATCHERS = "info:dispatchers"
INFO_SUBSCRIBE_ON = "info:subscribe_on"
INFO_SUBSCRIBE_OFF = "info:subscribe_off"
SUBSCRIBE_CONFIRM = "subscribe:confirm"

# ── CITIZEN_FLOW: «Настройки» ──
SETTINGS_HELP = "settings:help"
SETTINGS_RULES = "settings:rules"
SETTINGS_POLICY = "settings:policy"
SETTINGS_GOODBYE = "settings:goodbye"
GOODBYE_UNSUB = "goodbye:unsub"
GOODBYE_REVOKE_ASK = "goodbye:revoke_ask"
GOODBYE_REVOKE_YES = "goodbye:revoke_yes"
GOODBYE_ERASE_ASK = "goodbye:erase_ask"
GOODBYE_ERASE_YES = "goodbye:erase_yes"

# ── CITIZEN_FLOW: рассылки ──
BROADCAST_UNSUBSCRIBE = "broadcast:unsubscribe"

# ── BROADCAST_ADMIN: мастер рассылки ──
BROADCAST_CONFIRM = "broadcast:confirm"
BROADCAST_ABORT = "broadcast:abort"
BROADCAST_EDIT = "broadcast:edit"

# ── OPERATOR_ADMIN: операторское меню ──
OP_MENU = "op:menu"
OP_STATS_MENU = "op:stats_menu"
OP_STATS_TODAY = "op:stats_today"
OP_STATS_WEEK = "op:stats_week"
OP_STATS_MONTH = "op:stats_month"
OP_STATS_QUARTER = "op:stats_quarter"
OP_STATS_HALF_YEAR = "op:stats_half_year"
OP_STATS_YEAR = "op:stats_year"
OP_STATS_ALL = "op:stats_all"
OP_OPEN_TICKETS = "op:open_tickets"
OP_DIAG = "op:diag"
OP_BACKUP = "op:backup"
OP_BROADCAST = "op:broadcast"
OP_BROADCAST_LIST = "op:broadcast_list"
OP_OPERATORS = "op:operators"
OP_SETTINGS = "op:settings"
OP_HELP_FULL = "op:help_full"
OP_HELP_SECURITY = "op:help_security"
OP_AUDIENCE = "op:audience"
OP_REPLY_CANCEL = "op:reply_cancel"


# ════════════════════════════════════════════════════════════════════════
# PREFIX-префиксы. Голые строки префиксов — чтобы билдеры и тесты ссылались
# на один источник, а не дублировали литерал. Совпадают с PREFIX_ROUTES.
# ════════════════════════════════════════════════════════════════════════

# CITIZEN_FLOW
PREFIX_LOCALITY = "locality:"
PREFIX_TOPIC = "topic:"
PREFIX_APPEAL_SHOW = "appeal:show:"
PREFIX_APPEAL_FOLLOWUP = "appeal:followup:"
PREFIX_APPEAL_REPEAT = "appeal:repeat:"
PREFIX_APPEAL_ATTS = "appeal:atts:"
PREFIX_APPEALS_PAGE = "appeals:page:"

# BROADCAST_ADMIN
PREFIX_BROADCAST_STOP = "broadcast:stop:"
PREFIX_BROADCAST_CANCEL_COOLDOWN = "broadcast:cancel-cooldown:"
PREFIX_OP_TMPL = "op:tmpl:"
PREFIX_OP_BC = "op:bc:"

# OPERATOR_ADMIN
PREFIX_OP_AUD = "op:aud:"
PREFIX_OP_REPLY = "op:reply:"
PREFIX_OP_REPLYINT = "op:replyint:"
PREFIX_OP_REOPEN = "op:reopen:"
PREFIX_OP_CLOSE = "op:close:"
PREFIX_OP_ERASE = "op:erase:"
PREFIX_OP_BLOCK = "op:block:"
PREFIX_OP_UNBLOCK = "op:unblock:"
PREFIX_OP_ATTS = "op:atts:"
PREFIX_OP_OPEN_CARD = "op:open_card:"
PREFIX_OP_OPADD = "op:opadd:"
PREFIX_OP_OPCARD = "op:opcard:"
PREFIX_OP_OPROLE = "op:oprole:"
PREFIX_OP_OPCHROLE = "op:opchrole:"
PREFIX_OP_OPDEACT = "op:opdeact:"
PREFIX_OP_OPDEACT_OK = "op:opdeact_ok:"
PREFIX_OP_OPREACT = "op:opreact:"
PREFIX_OP_SETKEY = "op:setkey:"
PREFIX_OP_SET = "op:set:"


# ════════════════════════════════════════════════════════════════════════
# PREFIX-билдеры. Каждый возвращает строку «<префикс><хвост>».
# ════════════════════════════════════════════════════════════════════════

# ── CITIZEN_FLOW ──────────────────────────────────────────────────────────

def locality(index: int) -> str:
    """Выбор населённого пункта по индексу в списке. → ``locality:<index>``."""
    return PREFIX_LOCALITY + str(index)


def topic(index: int) -> str:
    """Выбор темы обращения по индексу в списке. → ``topic:<index>``."""
    return PREFIX_TOPIC + str(index)


def appeal_show(appeal_id: int) -> str:
    """Карточка обращения жителя. → ``appeal:show:<appeal_id>``."""
    return PREFIX_APPEAL_SHOW + str(appeal_id)


def appeal_followup(appeal_id: int) -> str:
    """Дополнить обращение. → ``appeal:followup:<appeal_id>``."""
    return PREFIX_APPEAL_FOLLOWUP + str(appeal_id)


def appeal_repeat(appeal_id: int) -> str:
    """Подать похожее обращение. → ``appeal:repeat:<appeal_id>``."""
    return PREFIX_APPEAL_REPEAT + str(appeal_id)


def appeal_atts(appeal_id: int) -> str:
    """Свои вложения по обращению. → ``appeal:atts:<appeal_id>``."""
    return PREFIX_APPEAL_ATTS + str(appeal_id)


def appeals_page(page: int) -> str:
    """Пагинация списка «Мои обращения». → ``appeals:page:<page>``."""
    return PREFIX_APPEALS_PAGE + str(page)


# ── BROADCAST_ADMIN ───────────────────────────────────────────────────────

def broadcast_stop(broadcast_id: int) -> str:
    """Экстренная остановка идущей рассылки. → ``broadcast:stop:<id>``."""
    return PREFIX_BROADCAST_STOP + str(broadcast_id)


def broadcast_cancel_cooldown(broadcast_id: int) -> str:
    """Отмена рассылки в окне cooldown. → ``broadcast:cancel-cooldown:<id>``."""
    return PREFIX_BROADCAST_CANCEL_COOLDOWN + str(broadcast_id)


def op_tmpl(action: str) -> str:
    """Действие с шаблонами рассылок. → ``op:tmpl:<action>``.

    ``action`` — свободная форма: ``list`` / ``new`` / ``search`` /
    ``cancel`` / ``open:<id>`` / ``apply:<id>`` / ``clone:<id>`` /
    ``rename:<id>`` / ``edit:<id>`` / ``delete:<id>`` / ``delete_ok:<id>`` /
    ``back_to_name``. Разбирается в ``broadcast_templates.py``.
    """
    return PREFIX_OP_TMPL + action


def op_bc(verb: str, broadcast_id: int) -> str:
    """Карточка/клон/failed по рассылке из истории.
    → ``op:bc:<verb>:<broadcast_id>``.

    ``verb`` ∈ {``open``, ``clone``, ``failed``}.
    """
    return PREFIX_OP_BC + verb + ":" + str(broadcast_id)


# ── OPERATOR_ADMIN ────────────────────────────────────────────────────────

def op_aud(action: str) -> str:
    """Действие в разделе «Аудитория». → ``op:aud:<action>``.

    ``action`` — свободная форма: категория ``subs`` / ``consent`` /
    ``blocked``; ``show:<id>`` / ``block:<id>`` / ``unblock:<id>`` /
    ``erase:<id>``; ``page:<category>:<page|noop>`` / ``dump:<category>:<page>`` /
    ``search:<category>``. Разбирается в ``admin_audience.py``.
    """
    return PREFIX_OP_AUD + action


def op_reply(appeal_id: int) -> str:
    """Финальный ответ по обращению. → ``op:reply:<appeal_id>``."""
    return PREFIX_OP_REPLY + str(appeal_id)


def op_replyint(appeal_id: int) -> str:
    """Промежуточный ответ (без закрытия). → ``op:replyint:<appeal_id>``."""
    return PREFIX_OP_REPLYINT + str(appeal_id)


def op_reopen(appeal_id: int) -> str:
    """Вернуть обращение в работу. → ``op:reopen:<appeal_id>``."""
    return PREFIX_OP_REOPEN + str(appeal_id)


def op_close(appeal_id: int) -> str:
    """Закрыть обращение без ответа. → ``op:close:<appeal_id>``."""
    return PREFIX_OP_CLOSE + str(appeal_id)


def op_erase(appeal_id: int) -> str:
    """Стереть ПДн по обращению. → ``op:erase:<appeal_id>``."""
    return PREFIX_OP_ERASE + str(appeal_id)


def op_block(appeal_id: int) -> str:
    """Заблокировать жителя по обращению. → ``op:block:<appeal_id>``."""
    return PREFIX_OP_BLOCK + str(appeal_id)


def op_unblock(appeal_id: int) -> str:
    """Разблокировать жителя по обращению. → ``op:unblock:<appeal_id>``."""
    return PREFIX_OP_UNBLOCK + str(appeal_id)


def op_atts(appeal_id: int) -> str:
    """Показать вложения обращения. → ``op:atts:<appeal_id>``."""
    return PREFIX_OP_ATTS + str(appeal_id)


def op_open_card(appeal_id: int) -> str:
    """Полная карточка обращения с историей. → ``op:open_card:<appeal_id>``."""
    return PREFIX_OP_OPEN_CARD + str(appeal_id)


def op_opadd(action: str) -> str:
    """Мастер добавления оператора. → ``op:opadd:<action>``.

    ``action`` — свободная форма: ``list`` / ``start`` / ``cancel`` /
    ``from_group`` / ``confirm`` / ``name_keep`` / ``name_edit`` /
    ``edit_role`` / ``pick:<user_id>`` / ``role:<role>``. Разбирается в
    ``admin_operators_wizard.py``.
    """
    return PREFIX_OP_OPADD + action


def op_opcard(max_user_id: int) -> str:
    """Карточка оператора. → ``op:opcard:<max_user_id>``."""
    return PREFIX_OP_OPCARD + str(max_user_id)


def op_oprole(max_user_id: int) -> str:
    """Открыть picker смены роли. → ``op:oprole:<max_user_id>``."""
    return PREFIX_OP_OPROLE + str(max_user_id)


def op_opchrole(max_user_id: int, role: str) -> str:
    """Применить смену роли. → ``op:opchrole:<max_user_id>:<role>``.

    ``role`` — значение ``OperatorRole`` (``it`` / ``coordinator`` /
    ``aemr`` / ``egp``).
    """
    return PREFIX_OP_OPCHROLE + str(max_user_id) + ":" + role


def op_opdeact(max_user_id: int) -> str:
    """Деактивация оператора — подтверждение. → ``op:opdeact:<max_user_id>``."""
    return PREFIX_OP_OPDEACT + str(max_user_id)


def op_opdeact_ok(max_user_id: int) -> str:
    """Деактивация оператора — применить. → ``op:opdeact_ok:<max_user_id>``."""
    return PREFIX_OP_OPDEACT_OK + str(max_user_id)


def op_opreact(max_user_id: int) -> str:
    """Реактивация оператора. → ``op:opreact:<max_user_id>``."""
    return PREFIX_OP_OPREACT + str(max_user_id)


def op_setkey(key: str) -> str:
    """Экспертный wizard правки ключа настройки. → ``op:setkey:<key>``.

    ``key`` — имя ключа settings_store (``welcome_text``, ``policy_url``, …).
    """
    return PREFIX_OP_SETKEY + key


def op_set(path: str) -> str:
    """Иерархическое меню настроек. → ``op:set:<path>``.

    ``path`` — свободная форма пути меню: ``cat:texts`` / ``list:topics`` /
    ``obj:emergency_contacts`` / ``text:<key>`` / ``url:<key>`` /
    ``edit:<key>`` / ``cancel:<key>`` / ``list_add:<key>`` /
    ``list_del:<key>:<i>`` / ``obj_add:<key>`` / ``obj_view:<key>:<i>`` /
    ``obj_del:<key>:<i>`` / ``author`` / ``quiet`` / ``quiet:toggle`` /
    ``quiet:edit:start`` / ``notify`` / ``notify:toggle:<key>`` /
    ``pr:start`` / ``pr:diff`` / ``pr:confirm`` / ``expert``.
    Разбирается в ``admin_settings.py``.
    """
    return PREFIX_OP_SET + path
