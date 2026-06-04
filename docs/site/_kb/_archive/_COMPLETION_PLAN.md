---
title: Единый план завершения (свод 5 линз)
status: actionable
date: 2026-06-01
scope: bot/aemr_bot (handlers/services/db/ui/copy/utils)
source_of_truth: код с file:line; находки сверены grep'ом и чтением, ложноположительные отсеяны
review_type: РЕВЬЮ (план), не правка
baseline: 1611 тестов зелёные, порог покрытия 67 (ci.yml:139)
---

# Единый план завершения aemr_bot

Свод находок пяти линз (bugs / security / simplify / coverage / comments) в единый
план по **волнам-PR**. Каждая волна — отдельный PR (не аккумулировать). Все
находки перепроверены по коду; ложноположительные явно помечены и отброшены.

## Отсеянные ложноположительные (сверено с кодом)

- **#8 «`list_all` vs `iter_all` в maxapi» — НЕ баг.** Установленный
  `maxapi.types.chats.ChatMembersManager` имеет **оба** метода (`dir` →
  `['add','get','iter_all','kick','list','list_all','me']`). Код
  `admin_operators_wizard.py:102` зовёт `manager.list_all()` — метод существует,
  путь рабочий, F11/stale-cleanup НЕ no-op. Остаётся лишь рассинхрон докстринга
  (стр. 87-93 говорит `iter_all`, код зовёт `list_all`) → тривиальный
  doc-fix, перенесён в ВОЛНУ C. **Severity снижена с «verify» до косметики.**
- **`set_phone`/`set_first_name` in-memory stale** — не баг: `appeal.py:446`
  вручную ставит `user.first_name = cleaned`, downstream `ask_contact_or_skip`
  перечитывает через `current_user` (свежий запрос). Сверено.
- **callback-contract тесты** (`test_callback_coverage_contract.py`,
  `test_callback_router_coverage.py`) — НЕ мусор, это структурные guard'ы
  (orphan payload / dead route / phantom handler). Не трогать.
- **«register = плоский список хендлеров»** (admin_commands.register 321,
  appeal.register 178, start.register 162) — НЕ god-функции по сложности,
  дробить косметически не нужно.
- Множество «assert True / 0 дубль-имён тестов» — детектор бил построчно;
  по факту в разных классах/модулях. Не дубликаты.

---

## ВОЛНА A — подтверждённые баг-фиксы (P0/P1)

Первый PR. Только корректность. Все позиции сверены чтением кода.

### A-1. Глобальный `/cancel` в админ-чате не чистит 3 из 6 wizard/intent-хранилищ
- **file:line:** `handlers/appeal.py:580-593` (ветка `/cancel` в `on_message`).
- **Симптом:** чистит только `broadcast_handler._wizards`,
  `admin_cmd._op_wizards`, `op_reply.drop_reply_intent`. НЕ чистит
  `admin_settings._edit_intents` (consumer `handle_settings_edit_text`
  appeal.py:611), `broadcast_templates._wizards` (consumer
  `handle_wizard_text` appeal.py:602), `admin_audience._search_intents`
  (consumer `handle_audience_search_text` appeal.py:621). Оператор жмёт
  «✏️ Редактировать welcome_text» / «📋 Шаблоны→создать» / «🔍 Поиск»,
  передумывает, шлёт `/cancel`, бот рапортует «сброшено», но intent жив →
  следующий текст молча уходит в живой wizard (напр. становится новым
  `welcome_text` для всех жителей).
- **fix:** в `/cancel` добавить сброс трёх недостающих хранилищ. Лучше —
  через единый агрегатор (см. E-1 / B5), но в ВОЛНЕ A достаточно явных pop'ов
  для немедленного закрытия дыры.
- **эффорт:** S. **риск:** низкий. **safety-net:** да — тест на каждый из 3
  сторов (после `/cancel` → следующий текст НЕ потреблён wizard'ом).

### A-2. ФИО оператора валидируется только по длине (`len < 2`), без проверки на буквы
- **file:line:** `handlers/admin_operators_wizard.py:505-513` (step
  `awaiting_name`).
- **Симптом:** `full_name = text.strip(); if len < 2: reject`. Принимает «77»,
  «...», «👍👍» как ФИО оператора в журнал 152-ФЗ. У citizen-имени защита есть
  (`_HAS_ALNUM.search` в `appeal_funnel.py:444`), у оператора — нет.
- **fix:** применить тот же guard `_HAS_ALNUM.search(full_name)` (re-export из
  `appeal_runtime`).
- **эффорт:** XS. **риск:** низкий. **safety-net:** да — тест отклонения «77» /
  принятия «Иван Петров».

### A-3. `do_subscribe_confirm` — нет re-check уже выданного согласия
- **file:line:** `handlers/menu.py:472-510`. Проверяет только `is_blocked`.
- **Симптом:** stale-тап старой кнопки `subscribe:confirm` (или двойной) →
  `consent_broadcast_at` перезаписывается на now, `notify_broadcast_subscribed`
  шлёт оператору повтор, в audit_log дубль `self_subscribe_broadcast`.
  `subscribe:confirm` — menu-callback, funnel-guard не покрывает (appeal.py:99).
- **fix:** до записи: `if user.consent_broadcast_at and user.subscribed_broadcast:
  показать SUBSCRIBE_ALREADY_ON, return`.
- **эффорт:** XS. **риск:** низкий. **safety-net:** да — тест повторного тапа
  (нет второго notify/audit).

### A-4. `cmd_export` (`/export`) читает `ap.messages` без `selectinload` → detached/lazy-load
- **file:line:** `handlers/start.py:254-279`; корень — `services/appeals.py:150-186`
  (`list_for_user` НЕ грузит `messages`). Итерация `reversed(ap.messages or [])`
  на стр. 261 внутри async-сессии.
- **Симптом:** в async SQLAlchemy lazy-load на `.messages` → `MissingGreenlet` /
  `DetachedInstanceError`. Команда скрытая (152-ФЗ ст.14), но при реальном
  вызове падает с пустым ответом жителю. Mock'и это не ловят.
- **fix:** грузить через `selectinload(Appeal.messages)` (отдельный list-метод
  или дозагрузка), как в admin-путях.
- **эффорт:** S. **риск:** низкий. **safety-net:** **обязателен** — тест с
  РЕАЛЬНОЙ сессией (pg), иначе регрессия невидима под моками.

> Примечание: A-3/A-4 строго это P2 по исходной классификации линзы bugs, но
> по характеру (порча данных всех жителей / падение регуляторной команды)
> включены в ВОЛНУ A одним багфикс-PR. A-1/A-2 — прямые OWNER-находки.

---

## ВОЛНА B — покрытие до максимума

Только тесты, прод-код не трогается. **Критичный кавеат:** локально 74.3% —
**заниженная** оценка: `tests/conftest.py:24-30` делает `pytest.skip` для 4
`*_service_pg.py` без Postgres (123 skip). CI поднимает Postgres
(`ci.yml:103-121`), там сервисы покрыты. **Цифры сервисов брать из CI-артефакта
`coverage.xml`/`htmlcov`, не из локали.** Перед коммитом гонять с
`DATABASE_URL=postgresql+asyncpg://…`, иначе ложное падение `--cov-fail-under`.

### Реальные дыры (мок-достижимо, нет dedicated-теста) — приоритет
| # | file:line | суть | новый/расширить | эффорт | severity |
|---|---|---|---|---|---|
| B1 | `handlers/admin_commands.py:145-455` (15.1%) | НЕТ файла-теста; authz-guard, парсинг `/reply` (len<3, нечисловой id, пустой текст :178-195), period `/stats` (:155-161) | новый `test_admin_commands.py` (mock MessageCreated, DB не нужен) | M | 🔴 high |
| B2 | `handlers/appeal_runtime.py:144-254` (29.4%) | НЕТ файла-теста; SACRED-финализация (rate-limit :156, snapshot detached :200, `admin_card.render` :202, relay :214, ack+меню :241, swallow ack-fail :246) | новый `test_appeal_runtime.py` (паттерн `test_cron_jobs.py:265`) | M-L | 🔴 high |
| B3 | `services/stats.py:96-174` (47.1%) | `_render_workbook` — ЧИСТАЯ ф-я (list[Appeal]→bytes), не покрыта | unit на MagicMock-Appeal'ах (без DB) | L | 🟡 med |
| B4 | `services/settings_store.py:577-960` (59.4%) | sanitize/validation ветки :798-939, :627-652, :717-769 (часть sync, не pg) | `test_settings_store_validation.py` | M | 🟡 med |
| B5 | `handlers/admin_panel.py:216-427` (47.8%) | рендер/обработка панели оператора | расширить `test_admin_panel.py` | M | 🟡 med |
| B6 | `services/cron.py:328-423,732-784` (58.1%) | тела `_job_*` ниже guard'ов | усилить `test_cron_jobs.py` (mock session_scope с rowcount) | M | 🟡 med |
| B7 | `services/idempotency.py:86-191` (59.8%) | insert/lookup :86-142, GC :155-191 — **SEC #7 fail-open регрессия** | мок-session тест fail-open | L-M | 🟡 med |
| B8 | `handlers/appeal_geo.py:47-168` (44.7%) | гео-шаги воронки | расширить `test_geo.py` callback-ветками | L-M | 🟢 low |
| B9 | `services/admin_relay.py:112-198` (55.9%) | тело relay-вложений | `test_uploads_policy_admin_relay.py` | L | 🟢 low |
| B10 | `handlers/start.py:328-467` (59.3%) | consent/онбординг ветки | расширить `test_handlers_start.py` | M | 🟢 low |

### Мусорные / слабые тесты — усилить (не раздувать)
Наглой мусорки мало. `assert True`/`1==1` — 0. Кандидаты:
- **T1 (удалить/заменить):** `test_handlers_auth_broadcast.py:170-190`
  (`TestAdminCommandsExports`, 4 теста) — только `assert callable(...)`, ноль
  поведения. После B1 становится избыточным → удалить класс ИЛИ один
  `__all__`-контракт-тест. **S.**
- **T2 (усилить):** `test_main_helpers.py:172,202,211` — «не падает» без
  наблюдаемой проверки → добавить `caplog` на info/warning. **S.**
- **T3 (усилить):** assert-free «swallow exception» — `test_cron_jobs.py:218,229,289`,
  `test_operator_reply_characterization.py:112`,
  `test_uploads_policy_admin_relay.py:278`, `test_event_helpers.py:237` →
  `patch session_scope side_effect=RuntimeError` + assert «не пробросило И
  loop/handler продолжил». **S-M.**
- **T4 (дёшево усилить, ~13 файлов):** негативные «no_user_id/no_actor/empty→noop» —
  валидны по намерению, но без assert не проверяют отсутствие побочки →
  `mock.assert_not_called()` на `send_message`/`answer`. **S.**
- Итог раздела: **0 удалить безусловно** (только T1 опц.), **~26 assert-free
  усилить** 1-2 строками mock-ассертов.

### Легитимно низкие (не гнать к 100%)
`main.py` 38.7% (`main()`/polling-loop — боевой процесс; корректнее
`# pragma: no cover`), `cron_registry.py` 38.5% (3 строки), `health.py` 68.1%.

### Целевой порог
- Текущий: **67** (только `ci.yml:139`, в `pyproject.toml` `fail_under` НЕТ).
- **67→70 безопасно уже сейчас** (запас 7.3 пп локально, в CI выше).
- **67→72+ после B1-B3.** Поднимать порог тем же PR, что закрывает дыры.

---

## ВОЛНА C — русификация комментариев handlers/utils/db

**Объём НЕ «партии по каталогам».** Сырой скан дал 83 хита, после верификации
контекста — **~13 реальных** англ. комментов в **~7 файлах**: один маленький
cleanup-PR на 20-30 минут. handlers/ и services/ уже фактически русифицированы
(#114, #140). db/ практически чист (1 позиция). Рантайм-влияние нулевое.

### Партия (один PR, по каталогам внутри)
**handlers/:**
- `admin_commands.py:96,101,109,113,117,125` — секции-ярлыки `# Stats`/`# Settings`/
  `# Audience`/`# Per-appeal ops`/`# Common`/`# Operators wizard` → рус.
- `admin_callback_dispatch.py:208` — `# operator menu / actions` → `# Меню оператора / действия`.
- `admin_callback_dispatch.py:93` (опц., докстринг) — `ack` → «подтверждение».
- `broadcast_wizard.py:320` — `# Typing-indicator: count subscribers...` (самый
  «настоящий» английский) → рус.
- `broadcast_templates.py:122` — `# Strip prefix` → `# Срезаем префикс`.
- **+ `admin_operators_wizard.py:87-93`** — синхронизировать докстринг
  `iter_all`→`list_all` (последствие отсева ложноположительного #8).

**utils/:**
- `url_defang.py:61,63,69` — `# Cyrillic + CIS` / `# Common global` /
  `# Cheap/new (frequent phishing)` → рус.
- `attachments.py:275` — `# Dict-fallback` → `# Запасной разбор словаря`.

**db/:**
- `alembic/versions/0009_partial_indexes_for_hot_paths.py:46` — `# 1) PDn-retention
  partial index.` → рус. (пункты 2/3 уже рус., тело миграции не трогаем).

### НЕ трогать (иначе сломать/зашуметь)
- Все `# noqa: …` / `# type: ignore[…]` (25 шт.) — директивы линтера.
- ASCII-дивайдеры `---- … ----` (15 шт.) — проектная конвенция секций.
- Continuation-tails (~18) — вторые строки УЖЕ русских блок-комментов.

- **эффорт:** S (вся волна). **риск:** нулевой (только комменты). **safety-net:**
  не нужен — `ruff`+`pytest` зелёные без изменений тестов.
- **Опц. P4 (анти-регрессия, owner-call):** local pre-commit hook, падающий на
  новых `#`-комментах с латиницей вне whitelist (`noqa|type:|pragma` + дивайдеры
  + бэктик-идентификаторы). ~1 ч, риск false-positive — согласовать с владельцем.

---

## ВОЛНА D — безопасные упрощения + мёртвый код

Прод-поведение не меняется. Подтверждено grep'ом (0 prod-callers) + ruff/vulture.

### D-1. Мёртвый код в `wizard_registry.py` (крупнейшая находка)
- **file:** `services/wizard_registry.py` (279 строк). Docstring заявляет
  «единое хранилище», но prod использует лишь reply-intent срез + op_wizard
  set/clear. **Мёртвые (0 не-тест вызовов):** `update_op_wizard` (:63),
  `get_op_wizard` (:55), `get_broadcast_wizard` (:76), `clear_broadcast_wizard`
  (:84), `is_recent_reply` (:114), `remember_recent_reply` (:118),
  `evict_old_replies` (:122), dict `_recent_replies` (:49 — operator_reply
  переимплементировал дедуп локально), `reset_all` (:151, test-only),
  `schedule_persist_broadcast`+`_persist_*_broadcast` (:265,:218,:233 —
  broadcast-wizard не персистится).
- **`clear_all_for` (:139-148) мёртв И сломан-by-design** (сверено): зовёт
  `clear_op_wizard`/`clear_broadcast_wizard` (registry-внутренние dict'ы), а
  `/cancel` держит состояние в ДРУГИХ dict'ах → не пересекаются. Решение
  «доделать (→E-1) vs удалить» — owner-call.
- **fix:** (1) удалить мёртвые ф-ии + `_recent_replies`; (2) переписать
  докстринг под реальный API.
- **эффорт:** M (с тестами). **риск:** низкий (удаление неиспользуемого).
  **safety-net:** да — `ruff`+`pytest`; grep подтвердить 0 prod-ref до удаления.

### D-2. Прочий мёртвый код (0 prod-callers)
- `utils/event.py:304` `send_to` (дублирует `send`).
- `settings_store.py:115` `is_whitelisted_url` (public wrapper; prod зовёт
  приватную `_is_whitelisted_url`).
- `menu_tracker`: `get_chat_state` (:128), `set_last_menu_mid` (:159),
  `clear_all` (:138) — старый API до dual-tracker.
- `quiet_hours.reset_cache_for_tests` (:142), `image_attachments.attachment_meta`
  (:33), `url_defang.has_defangable_url` (:162), `cron_registry.all_ids`/`lookup`
  (:122,:127), `broadcast_templates.count_active` (services).
- **fix:** удалить пачкой + соответствующие тесты.
- **эффорт:** S-M. **риск:** низкий. **safety-net:** да (grep + тесты).

### D-3. Stale `# noqa` (40 шт, RUF100, авто-фикс)
- Подтверждено `ruff check --select RUF100`: admin_settings.py (:43,61,73,74,79,86,91,97),
  broadcast.py (:52,68,87), broadcast_templates.py (:49-92), keyboards.py (:19-25),
  texts.py (:20-26), admin_card.py (:174,175), main.py (:46,358,368),
  event.py (:223), operator_reply.py (:380), 0010_*.py:60.
- **fix:** `ruff check aemr_bot --select RUF100 --fix`.
- **эффорт:** XS. **риск:** низкий. **safety-net:** прогнать тесты (фасадные
  re-export в admin_settings — ruff утверждает noqa лишний, значит уже used).

### D-4. Дубль admin-chat guard + лишние алиасы (E3)
- `appeal.py:529,574` хардкодит `if cfg.admin_group_id and chat_id ==
  cfg.admin_group_id` вместо `is_admin_chat(event)`. Плюс 3 ненужных алиаса
  `_is_admin_chat = is_admin_chat` (start.py:31, admin_commands.py:86,
  broadcast.py:113 — все сверены grep'ом).
- **fix:** appeal.py звать `is_admin_chat`; убрать алиасы, импортировать напрямую.
- **эффорт:** S. **риск:** низкий. **safety-net:** тесты роутинга есть.

### D-5. (опц.) Лишние function-local импорты в services/
- 167 ленивых импортов в 46 файлах; в services/ (settings_store, card_format,
  uploads, users, cron) циклов обычно нет → поднять в top-level. В handlers
  оставить реальные cycle-breakers с комментом.
- **эффорт:** M (по файлу, инкрементально). **риск:** низкий. **safety-net:**
  `python -c "import aemr_bot.main"` после каждого файла. Owner: можно отложить.

---

## ВОЛНА E — структурные потеряшки (каждая отдельным PR)

Сердце архитектуры. Каждый пункт — свой PR с characterization-тестами ДО правки.

### E-1. `callback_router` — мёртвый реестр-дубль (унификация диспетчеров)
- **file:** `handlers/callback_router.py` (167 строк). Сверено grep'ом: из
  модуля реально используются ТОЛЬКО `parse_int_tail` (appeal.py:341,433;
  admin_callback_dispatch.py:350) и `is_admin_callback` (appeal.py:530).
  Таблицы `EXACT_ROUTES`/`PREFIX_ROUTES` (~90 маршрутов) **не читает никто** —
  только упомянуты в комменте appeal.py:527. Маршруты задублированы в 3 местах
  (`appeal._CITIZEN_*`, `admin_callback_dispatch._EXACT/_PREFIX_*`,
  `menu._EXACT/_PREFIX_*`) → реестр стал 4-й параллельной копией-справочником.
- **Развилка (owner-decision, TD-02 в RUNBOOK.md:1091 — сознательно отложено):**
  - (a) сделать `EXACT_ROUTES` единым источником группы для трёх диспетчеров —
    **эффорт L, риск высокий** (трогает callback-маршрутизацию ПРОД-бота).
  - (b) удалить таблицы, оставить `parse_int_tail`+`is_admin_callback`,
    переименовать в `callback_utils` — **эффорт S, риск низкий.**
- **safety-net:** для (a) **обязательны** characterization-тесты на каждую
  группу (база — `test_appeal_dispatcher.py`); для (b) — grep 0 prod-ref таблиц.

### E-2. Три callback-диспетчера не объединены (общий механизм ×3)
- **file:** `appeal.on_callback` (appeal.py:510-560) последовательно зовёт
  `_dispatch_citizen_callback` → `dispatch_admin_callback` → `menu.handle_callback`;
  каждый = `_EXACT dict` + `_PREFIX tuple` + int-tail + lambda-обёртки.
- **fix:** вынести общий `dispatch(payload, exact, prefix_id, prefix_raw) -> bool`
  в `callback_router`, переиспользовать в 3 местах. **Связан с E-1** (логично
  одним решением).
- **эффорт:** M. **риск:** средний (сердце роутинга). **safety-net:** да
  (характеризация). Можно отложить — копии маленькие, покрыты тестами.

### E-3. `main.py` не вынесен в `create_app()`
- **file:** `main.py` — module-level side-effects (`bot=Bot(...)` :34,
  `install_outgoing_tracker_hook` :48, `dp=Dispatcher(...)` + `register_handlers`
  :53-54, webhook на импорте :152-189). `async def main()` :265-423 (158 строк,
  11 boot-шагов).
- **fix:** `def create_app() -> tuple[Bot, Dispatcher]` + `async def run(bot, dp)`
  (boot разбить на `_boot_preflight`/`_boot_seed`/`_boot_recover`/`_boot_scheduler`).
- **эффорт:** M. **риск:** низкий. **safety-net:** сохранить re-export
  `spawn_background_task` (:26) и module-level `bot`/`dp`; перед PR
  `grep "from aemr_bot.main import"`. Owner: можно делать.

### E-4. 13 анонимных хендлеров `async def _(` в start.py
- **file:** `start.py:327,333,374,383,392,406,413,423,430,437,444,451,458` —
  все обёрнуты `@dp.message_created(Command(...))`, в трейсбэке ПРОД виден
  только `_`. Контрпример сделан правильно: `admin_commands.register` именует
  вложенные (`cmd_open_tickets`, `cmd_stats`…).
- **fix:** переименовать 13 `_` → осмысленные (`cmd_start_dispatch`,
  `on_bot_started`, …). Имя функции не влияет на регистрацию декоратором.
- **эффорт:** S (механический). **риск:** низкий. **safety-net:** не нужен
  (наблюдаемость; vulture перестанет шуметь «unused function '_'»).

### E-5. (P1.2) Типизация callback — `CallbackPayload(BaseModel)`
- **Задача #81** (бэклог MAXAPI_INSIGHTS). Структурная типизация payload
  callback'ов вместо строкового парсинга. Логично делать **после/вместе с
  E-1/E-2** (единая точка парсинга — естественное место для типизации).
- **эффорт:** M. **риск:** средний. **safety-net:** да (характеризация роутинга).
  Owner: зависит от решения по E-1.

> **E-1/E-2 объединять ли — owner-decision (TD-02 отложен сознательно).**
> E-3/E-4 — safe, можно делать сразу отдельными PR. E-5 — после E-1/E-2.

---

## ВОЛНА F — security-фиксы

Линза security: **эксплуатируемых дыр с утечкой ПДн или обходом авторизации НЕ
найдено** (SEC #1-9, F3-F14, A1-A7 закрыты, сверено). Найдено 2 дефекта, оба
**fail-closed** (не эксплуатируемые на утечку), но требуют правки.

### F-A. `/find_resident` сломан + неверная идиома авторизации
- **file:line:** `handlers/admin_resident_search.py:112-113`. Сверено:
  `ensure_operator()` по контракту (`_auth.py:38`) возвращает **`bool`**, не
  `Operator`. Идиома `if operator is None` никогда не истинна. Для реального
  оператора `ensure_operator→True`, далее `operator.max_user_id` (стр. 153,193,…)
  → `AttributeError` → **команда падает у всех, фича мёртвая**. Для не-оператора
  `False is None`→False → ранний return не срабатывает; защита держится только на
  `is_admin_chat` (стр. 110) + последующем краше. **Утечки ПДн нет (fail-closed),
  но гард логически неверен** (риск регрессии, если `is_admin_chat` ослабнет).
  Все 15 других хендлеров используют корректную `if not await ensure_operator(event): return`.
- **Почему тесты зелёные:** `test_admin_resident_search.py:192,195` мокают
  `ensure_operator` как `SimpleNamespace(max_user_id=999)` — расходится с
  реальным bool-контрактом.
- **fix:** `if not await ensure_operator(event): return` + взять
  `operator_max_user_id` из `get_user_id(event)`; ИЛИ
  `operator = await get_operator(event); if operator is None: return`.
- **эффорт:** S. **риск:** низкий (fail-closed). **safety-net:** **обязателен** —
  поправить мок в тесте (вернуть `True`/реальный объект), иначе fix уронит тест
  на `True.max_user_id` (что и обнажит исходный баг).

### F-B. Followup-текст жителя не ограничен по длине (несогласованно с summary)
- **file:line:** `handlers/appeal_funnel.py:635` (сверено: `(text_body or "").strip()`
  без `[: cfg.summary_max_chars]`) → `services/appeals.py:35-51` `add_user_message`
  не обрезает. Исходная суть режется `chunk[: cfg.summary_max_chars]`
  (appeal_funnel.py:511, лимит 2000), followup — сырьём. DoS-storage класс.
- **Impact ограничен:** followup rate-limit (5/час, 30с, SEC #5) + admin-карточка
  клипает `_clip(text,400)`. Раздувание строки, не пробой.
- **fix:** `text = (text_body or "").strip()[: cfg.summary_max_chars]`.
- **эффорт:** XS. **риск:** низкий. **safety-net:** тест длинного followup.

### Наблюдения (НЕ дефекты, для протокола — не трогать)
- O-1: полный телефон в admin appeal card (`card_format.py:206`) — by design
  (оператор перезванивает); ПДн под защитой (admin-чат, audit, GPG-backup).
- O-2: `repo_sync` интерполирует `cfg.repo`/branch из env, не из ввода — OK.
- O-3..O-7: authz callback'ов defense-in-depth, IDOR закрыт, антифишинг цел,
  152-ФЗ (GPG backup, sha256-хеш телефона в логах, geo не plaintext), DoS/инъекции
  (hmac.compare_digest, semaphore(32), bound-параметры, statement_timeout=30s) —
  всё сверено, на месте.

> **F-A + F-B** можно одним PR (обе локальны, не трогают sacred-инварианты).
> Перед коммитом: `ruff check . && pytest` + поправить мок в
> `test_admin_resident_search.py`.

---

## OWNER / ЮРИСТ — требует решения владельца

- **E-1/E-2 (унификация callback-диспетчеров):** объединять через
  `callback_router` (риск, нужна характеризация) ИЛИ удалить мёртвые таблицы и
  переименовать в `callback_utils` (safe). TD-02 (RUNBOOK.md:1091) — отложено
  сознательно. **Нужно явное решение направления.**
- **D-1 `clear_all_for`:** доделать как единую точку `/cancel` (E-1/B5) ИЛИ
  удалить. Решение про single-point-of-cancel — за владельцем.
- **E-5 (P1.2 типизация callback, #81):** делать ли и когда (зависит от E-1).
- **Cross-wizard contamination на ВХОДЕ (#6 bugs):** единая «начать wizard =
  очистить остальные intent'ы» (закрывает и A-1) — рефактор единой точки сброса,
  эффорт M. Owner-decision о форме (агрегатор vs явные cross-pops).
- **E2 simplify (тройная дедупликация ответа оператора,
  `operator_reply._deliver_operator_reply`):** 3 слоя на инвариант «не отправить
  дважды» (UX-дедуп / DB-дедуп / idempotency). Защита ПДн-ответа — **не трогать
  вслепую**; задокументировать почему три ИЛИ схлопнуть (1)+(3). Анализ за
  владельцем.
- **E1 simplify (`menu._send_or_edit_menu` ≈ `event.send_or_edit_screen`,
  ~85 строк дубля freshness):** freshness — корень прошлых ПРОД-жалоб «потерял
  уведомление». Делегировать menu→screen рекомендуется, но с обязательными
  freshness-тестами до/после и сохранением различия в catch-exception. Owner:
  осторожно.
- **Анти-регрессия комментов (ВОЛНА C, P4 hook):** риск false-positive на
  легитимных терминах — согласовать.
- **Подъём порога покрытия:** 67→70 безопасно сейчас; 67→72+ после B1-B3 —
  подтвердить целевое число.

---

## Сводная таблица волн

| Волна | Содержание | PR | Эффорт | Риск | Safety-net |
|---|---|---|---|---|---|
| A | A-1 cancel-3store, A-2 ФИО alnum, A-3 subscribe re-check, A-4 export lazy-load | 1 | S-M | низкий | да (A-4 — pg-тест обязателен) |
| B | B1-B10 покрытие + T1-T4 hardening + порог 67→70/72 | 1-2 | M-L | низкий | сами тесты; гонять с pg |
| C | ~13 рус.-комментов в 7 файлах + докстринг list_all | 1 | S | нулевой | не нужен |
| D | D-1 wizard_registry dead, D-2 dead-пачка, D-3 noqa --fix, D-4 guard-дубль, D-5 опц. импорты | 1-2 | S-M | низкий | grep+тесты |
| E | E-1 router, E-2 dispatch×3, E-3 create_app, E-4 анон-хендлеры, E-5 P1.2 — КАЖДАЯ свой PR | 5 | S→L | E-3/E-4 низкий; E-1/E-2/E-5 средний-высокий | E-1/E-2/E-5 — характеризация |
| F | F-A find_resident authz, F-B followup-лимит | 1 | S | низкий (fail-closed) | да (мок-fix в тесте) |

**Рекомендованный порядок:** F (или A) первыми — оба багфикс, низкий риск. Затем
D (чистый cleanup без смены поведения) + C (комменты). Затем B (покрытие,
поднять порог). E — после, по одному PR, начиная с safe E-3/E-4; E-1/E-2/E-5 —
только после owner-решения по направлению (TD-02).

**Первый «чистый» PR без риска (можно слить хоть сейчас):** E-4 (переименовать
анонимные хендлеры) + D-3 (`ruff --fix RUF100`) + D-2 (удалить dead-функции с
тестами) + D-4 (убрать guard-дубль). Прод-поведение не меняется.
