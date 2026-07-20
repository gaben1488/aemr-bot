# _LEDGER.md — выверенный реестр фактов (супер-канон)

> **Статус:** канонический источник истины проекта. **КОД = истина №1.**
> Каждый факт привязан к `file:line` и сверен с кодом.
> **Сверено:** 2026-06-01 (распределённая верификация, wf_96077512-752);
> ревизия line-refs — 2026-06-04. Учтены решения владельца и Постановление №2129.
>
> **Как читать.** Канон — это ЗНАЧЕНИЕ/факт; `file:line` — подсказка на момент
> сверки. Код эволюционирует, номер строки может сдвинуться — истина в значении,
> не в номере. Анти-дрейф ключевых чисел закрывают тесты (cron-реестр, callback-реестр).
>
> **Структура.** Часть I — реестр фактов по ролям. Часть II — решения и
> рекомендации с обоснованием. От этого файла «питаются» Регламент v8, вики
> `_kb2/` и Политика: все числа и процедуры сверяются здесь.

---

## ЧАСТЬ I — РЕЕСТР ФАКТОВ

## Житель / воронка

_Раздел сверен по коду; ключевые факты ниже. Кратко: счётчик прогресса M = `len(_STAGES)` = 5 (не зависит от заранее заполненных телефона/имени — контакт это pre-step вне счётчика); на шаге населённого пункта есть нативная `RequestGeoLocationButton`._

---

**ФАКТЫ РАЗДЕЛА «ЖИТЕЛЬ» — подтверждено по коду (file:line, дословные значения)**

**1. Формат подписи прогресса и число шагов M**

- Шаблон счётчика — `services/progress.py:94`: `counter = f"<code>{current_idx + 1} / {len(_STAGES)}</code>"`.
- Заголовок (header) — `services/progress.py:113`: `header = f"📋 <b>Подача обращения</b> · {counter}"`. То есть **не** «Подача обращения · N / M» текстом, а с HTML-тегами: дословно рендерится как `📋 Подача обращения · 2 / 5` (header → `<b>…</b>`, счётчик → `<code>N / 5</code>`), пример в docstring `services/progress.py:79`.
- M = 5 жёстко. `_STAGES` — `services/progress.py:29`: `("name", "locality", "address", "topic", "summary")`, `len = 5`.
- **M НЕ зависит** от заранее заполненных телефона/имени. Контакт — отдельный pre-step, не входит в `_STAGES` (комментарий `services/progress.py:26-28`). Прогресс-карта вообще начинает рендериться только со стадии `name` и далее (`handlers/appeal_funnel.py:296` `_show_progress_step`); шаги `AWAITING_CONTACT`/`AWAITING_NAME` обрабатываются текстовыми экранами `texts.CONTACT_REQUEST`/`CONTACT_RECEIVED` без счётчика (`handlers/appeal_funnel.py:218-228`). Знаменатель всегда `len(_STAGES)=5`, меняется только числитель `current_idx+1`.
- Подписи стадий — `services/progress.py:30-36`: `name`→«Имя», `locality`→«Населённый пункт», `address`→«Адрес проблемы», `topic`→«Тема», `summary`→«Суть». Маркеры: `_DONE = "✓"` (`:54`), `_CURRENT = "▶"` (`:55`).

**2. DialogState — все состояния по порядку (`db/models.py:13-30`)**

`IDLE = "idle"` → `AWAITING_CONSENT = "awaiting_consent"` → `AWAITING_CONTACT = "awaiting_contact"` → `AWAITING_NAME = "awaiting_name"` → `AWAITING_LOCALITY = "awaiting_locality"` → `AWAITING_ADDRESS = "awaiting_address"` → `AWAITING_TOPIC = "awaiting_topic"` → `AWAITING_SUMMARY = "awaiting_summary"` → `AWAITING_FOLLOWUP_TEXT = "awaiting_followup_text"` → `AWAITING_GEO_CONFIRM = "awaiting_geo_confirm"`. Оба запрошенных состояния присутствуют (`AWAITING_LOCALITY` — `:18`, `AWAITING_GEO_CONFIRM` — `:30`).

**3. STATUS_LABELS (житель) — дословно (`copy/citizen_funnel.py:296-301`)**

```
"new": ("🆕", "Новое"),
"in_progress": ("🔄", "В работе"),
"answered": ("✅", "Завершено"),
"closed": ("⛔", "Закрыто без ответа"),
```
Используется в `services/card_format.py:353` и `:396` (`user_card` / лента). Примечание: для CSV-экспорта статистики есть отдельный маппинг `services/stats.py:177-181` (`_status_label`: «Новое»/«В работе»/… — без эмодзи), это не та таблица, что показывается жителю.

**4. `settings_store.py` DEFAULTS**

- `DEFAULTS['appointment_text']` — `services/settings_store.py:387-392`, дословно: `"Приём граждан временно исполняющим полномочия Главы Елизовского муниципального района А.С. Гончаровым осуществляется два раза в месяц (1 и 3 среда каждого месяца) по предварительной записи. Запись на приём ведётся по номеру телефона 8 (415-31) 7-25-29."` Содержит «РАЙОНА» и «А.С. Гончаровым» — соответствует решению владельца (осознанный seed).
- `DEFAULTS['topics']` — `services/settings_store.py:395`: `"topics": []` (пустой список; реальные темы приходят из seed-файла).
- `seed/topics.json` — список тем по порядку (9 шт.): `Дороги`, `Мусор`, `Свалки`, `Благоустройство`, `Управляющие компании`, `Транспорт`, `Образование`, `Культура и спорт`, `Другое`. Заливается в БД через `seed_if_empty` (`services/settings_store.py:806`).
- Дополнительно: `DEFAULTS['localities']` задан прямо в коде (`:415-426`, 10 поселений: Елизовское ГП, Вулканное ГП, Корякское СП, Начикинское СП, Николаевское СП, Новоавачинское СП, Новолесновское СП, Паратунское СП, Пионерское СП, Раздольненское СП).

**5. Кнопки ГЛАВНОГО меню жителя — `ui/citizen_keyboards.py::main_menu` (`:34-80`)**

Ровно **6 рядов** (по одной кнопке в ряд):
1. `📝 Написать обращение` (`menu:new_appeal`) — `:51`
2. `📂 Мои обращения` (`menu:my_appeals`) — `:52`
3. Динамическая подписка: `🔕 Не хочу получать рассылку` (`info:subscribe_off`) если подписан, иначе `🔔 Подписаться на рассылку` (`info:subscribe_on`) — `:57-68`
4. `🏛 Приём граждан` (`menu:appointment`) — `:72`
5. `ℹ️ Полезная информация` (`menu:useful_info`) — `:73`
6. `🛡️ Защита от мошенников` (`menu:security`) — `:78`
7. `⚙️ Настройки и помощь` (`menu:settings`) — `:79`

Уточнение: фактически **7 кнопок/рядов** в текущем коде. Docstring `:40-49` декларирует «6 кнопок» и утверждает, что «Электронная приёмная переехала в подменю», но кнопка `🛡️ Защита от мошенников` (`:78`) была добавлена отдельным пунктом сверх исходных шести — комментарий в docstring устарел относительно кода (код = истина: 7 рядов).

**6. Геолокация на шаге населённого пункта**

Подтверждено: на шаге `AWAITING_LOCALITY` присутствует **нативная** `RequestGeoLocationButton` — `ui/citizen_keyboards.py:266` в `localities_keyboard`: `kb.row(RequestGeoLocationButton(text="📍 Поделиться геолокацией", quick=False))` (импорт `:14-16`). Это первая кнопка над списком поселений. Тап → бот определяет поселение/адрес через `services/geo.py` и переводит жителя в `AWAITING_GEO_CONFIRM` (комментарий `:261-263`; подтверждение — `geo_confirm_keyboard` `:273-289` с `geo:confirm`/`geo:edit_address`/`geo:other_locality`). То есть житель **не шлёт геолокацию вложением** — есть штатная кнопка запроса геопозиции. Бэклог MAX P0.1 по геолокации в текущем коде **уже реализован**.

---

## Оператор

_Нюанс прав: `ensure_role` проверяет точное вхождение в множество (`_auth.py:64`), поэтому IT-гейтед команды (`/erase`, `/setting`, `/backup`, `/add_operators`) принимают ТОЛЬКО роль IT — координатор не проходит. `ensure_operator` принимает любого активного оператора._

---

**ФАКТ — file:line — ЗНАЧЕНИЕ**

## 1. `op_help_keyboard` — число базовых действий и добавки по ролям

`bot/aemr_bot/ui/operator_keyboards.py:397-446`. Кнопки строятся в одну колонку (`kb.row(...)` на каждую). Утверждение из задачи (3/6/10 + «Памятка») подтверждается частично — **разбивка иная**. Точный подсчёт по коду:

**Базовые (любая роль, всегда), строки 427-436 + 445 — 4 кнопки:**
- «📋 Открытые обращения» (или «… (N)» при `open_count`) — payload `op:open_tickets` (430)
- «📊 Статистика» — `op:stats_menu` (431)
- «🛠 Диагностика» — `op:diag` (436)
- «📋 Памятка оператора» — `op:help_full` (445) — доступна любой роли

**Добавка `can_broadcast=True` (IT и COORDINATOR), строки 432-435 — +3 кнопки:**
- «📢 Сделать рассылку» — `op:broadcast` (433)
- «📜 История рассылок» — `op:broadcast_list` (434)
- «📋 Шаблоны рассылок» — `op:tmpl:list` (435)

**Добавка `is_it=True` (только IT), строки 437-441 — +4 кнопки:**
- «💾 Снять бэкап» — `op:backup` (438)
- «👥 Операторы» — `op:operators` (439)
- «⚙️ Настройки бота» — `op:settings` (440)
- «📊 Аудитория и согласия» — `op:audience` (441)

**Итог по ролям (фактический, из кода):**
- **AEMR / EGP** (`can_broadcast=False`, `is_it=False`): **4 кнопки** (3 базовых действия + «Памятка»). Утверждение «3 + Памятка» = верно, если «Памятка» считать сверх трёх.
- **COORDINATOR** (`can_broadcast=True`, `is_it=False`): **4 + 3 = 7 кнопок** (6 действий + «Памятка»). Утверждение «6 + Памятка» = верно.
- **IT** (`can_broadcast=True`, `is_it=True`): **4 + 3 + 4 = 11 кнопок** (10 действий + «Памятка»). Утверждение «10 + Памятка» = верно.

То есть формулировка «3/6/10 действий + кнопка Памятка сверх» корректна, если «Памятка» не входит в счёт действий. Чисто арифметически массив содержит 4/7/11 элементов.

Роли, передающие флаги: `bot/aemr_bot/handlers/admin_panel.py:71-75` — `is_it = role == it`; `can_broadcast = role in {it, coordinator}`.

## 2. `OperatorRole` enum

`bot/aemr_bot/db/models.py:48-52` — `StrEnum`, четыре значения:
- `COORDINATOR = "coordinator"` (49)
- `AEMR = "aemr"` (50)
- `EGP = "egp"` (51)
- `IT = "it"` (52)

(Задача в скобках перечислила coordinator/aemr/egp/it — совпадает; `it` в enum присутствует, на строке 52.)

## 3. `SLA_RESPONSE_HOURS` = 4 + отсутствие тематической приоритизации

`bot/aemr_bot/config.py:38` — `sla_response_hours: int = Field(4, alias="SLA_RESPONSE_HOURS")`. **Значение = 4.** Подтверждено.

**Тематическая приоритизация (вода/газ/отопление/важность) — ОТСУТСТВУЕТ.** Подтверждено. Grep по `вода|газ|отоплени|приоритет|priority|важность|importance|urgent|severity` (case-insensitive) по всему `aemr_bot/` дал только нерелевантные совпадения:
- `services/appeals.py:173-182` — `status_priority` это сортировка listing'а по статусу (open→answered→closed via `case()` + `ORDER BY`), не по теме обращения.
- `services/settings_store.py:564`, `handlers/admin_settings_obj.py:108` — «Электроэнергия / Отопление / Холодная вода» это дефолтные подписи справочных контактов для жителя (emergency_contacts), не маршрутизация важности.
- `copy/broadcast_texts.py:262`, `services/broadcast_templates.py:229` — слово «вода» как пример поискового запроса по шаблонам рассылок.

Ни одного механизма назначения важности по теме обращения в коде нет. Поле важности/приоритета у модели `Appeal` отсутствует (в enum только `AppealStatus`: new/in_progress/answered/closed, models.py:41-45).

## 4. Дословные строки `action=` в audit_log

- Рассылка: `bot/aemr_bot/handlers/broadcast_wizard.py:350` — `action="broadcast_send"`. Подтверждено (НЕ `broadcast_started`).
- Изменение настройки: `bot/aemr_bot/handlers/admin_commands.py:359` — `action="setting_update"` (команда `/setting`); и `bot/aemr_bot/handlers/admin_settings.py:711` и `:1076` — `action="setting_update"` (кнопочный wizard). Подтверждено (НЕ `setting_changed`).

Смежные действия для полноты (тоже из кода): `setting_list_add`/`setting_list_del`/`setting_obj_add`/`setting_obj_del` (admin_settings.py 1125/440/1178/581), `broadcast_stop`/`broadcast_cancel_cooldown` (broadcast.py 253/204), `reopen`/`close`/`erase` (admin_commands.py 217/246/305), `operator_role_change`/`operator_deactivate`/`operator_reactivate`/`operator_upsert` (admin_operators.py 438/549/593/940).

## 5. `/find_resident` — что делает, кому доступна, audit

Handler: `bot/aemr_bot/handlers/admin_resident_search.py`. Регистрация `Command("find_resident")` — строка 231; точка входа `run_find_resident` — строка 103.

**Что делает:** поиск одного жителя по телефону ИЛИ MAX user id. `_detect_query_kind` (40-67) различает: только цифры длиной 4-9 → `max_user_id`; цифры ≥10 или `+` → `phone`; иначе `invalid`. По `max_user_id` зовёт `users_service.find_by_max_id`, по телефону — `find_by_phone`. Возвращает карточку жителя (`OP_FIND_RESIDENT_CARD`, строки 206-219): имя, **маскированный** телефон `+7***1234` (`_mask_phone`), статус согласия (✅ активно / 🔁 отозвано / — нет), подписка (🔔/🔕), строка блокировки (если заблокирован), последнее обращение (#id, дата, тема≤40, статус), общее число обращений. Без аргумента или при `invalid` — usage-подсказка. Не-найдено / неоднозначно (телефон дал None) — `OP_FIND_RESIDENT_NOT_FOUND`.

**Кому доступна:** только админ-чат (`is_admin_chat`, строка 110) + `ensure_operator` (строка 112) → **любая активная роль оператора** (coordinator/aemr/egp/it), отдельного role-guard нет. (Докстринг на строках 8-9 пишет «OP/SH/IT» — устаревшая терминология ролей, фактический гард — `ensure_operator`, т.е. любой активный оператор.)

**Пишет ли в audit_log:** ДА, каждый запрос. Три исхода через `ops_svc.write_audit`:
- `action="resident_search_not_found"` — строки 154 и 173 (телефон-None и общий not-found);
- `action="resident_search_found"` — строка 189, с `details={kind, found_max_user_id}`.

Телефон в audit маскируется (`_mask_query_for_audit`, 70-75: для phone — `_mask_phone`, для id — как есть). Retention 365 дней (152-ФЗ), как указано в докстринге (13).

## 6. Полный список slash-команд оператора с ролями

Операторские команды (админ-чат). Гард `ensure_role(event, IT)` принимает **строго IT** (`_auth.py:64` — точное вхождение в множество, coordinator НЕ проходит); `ensure_operator` — любая активная роль:

- `/open_tickets` — `admin_commands.py:136` — `_ensure_operator` → любая роль
- `/stats [period]` — `admin_commands.py:149` — `_ensure_operator` → любая роль
- `/reply <id> <текст>` — `admin_commands.py:164` — `_ensure_operator` (двухслойно, SEC #9) → любая роль
- `/reopen <id>` — `admin_commands.py:199` — `_ensure_operator` → любая роль
- `/close <id>` — `admin_commands.py:228` — `_ensure_operator` → любая роль
- `/erase max_user_id=|phone=` — `admin_commands.py:254`, гард `:256` — **только IT**
- `/setting [list|<key> <value>]` — `admin_commands.py:316`, гард `:318` — **только IT**
- `/diag` — `admin_commands.py:365` — `_ensure_operator` → любая роль
- `/backup` — `admin_commands.py:371`, гард `:373` — **только IT**
- `/op_help` — `admin_commands.py:377` — только `_is_admin_chat` (открывает меню; внутри меню кнопки уже фильтруются по роли) → любой в админ-чате
- `/add_operators` — `admin_commands.py:383`, гард `:390` — **только IT**
- `/find_resident <phone|max_user_id>` — `admin_resident_search.py:231` — `ensure_operator` → любая роль
- `/broadcast [list]` — `broadcast.py:887` — гейт `_is_admin_chat` на входе (`:889`), а реальный role-guard `ensure_role(IT, COORDINATOR)` срабатывает внутри wizard'а: `broadcast_wizard.py:127` и `:314`, и в операциях `broadcast.py:678/719/769/824` → **IT и COORDINATOR**

Команды `/start /help /menu /forget /cancel /export /policy /rules /subscribe /unsubscribe /whoami` (`start.py:373-457`) — гражданская сторона (личка жителя), не операторские.

---

## Администратор / инфраструктура

_SCHEMA — ровно 23 ключа (2026-07-09: было 17, +6 модульных тумблеров `admin_notify_*`, см. `services/notify_toggles.py`). Источник истины — код (`C:/Users/filat/max/aemr-bot`), ссылки `file:line`._

## 1. Cron-задачи: полный перечень с точным расписанием

**Источник истины расписания — `services/cron.py` (объект `jobs`, строки 849–1010), НЕ `cron_registry.py`.** Реестр `cron_registry.py` — машинно-читаемый anti-drift список имён для docs-sync теста, его `schedule_human` человекочитаемый и в ОДНОМ месте расходится с фактическим триггером (см. ⚠️ ниже). Расписание ниже взято из реальных `CronTrigger(...)` в `cron.py`.

| id (name) | Триггер в коде (cron.py) | TZ | Назначение | file:line |
|---|---|---|---|---|
| `db-backup` | `day_of_week=BACKUP_DAY_OF_WEEK("sun"), hour=BACKUP_HOUR(3), minute=BACKUP_MINUTE(0)` → **вс 03:00** | Asia/Kamchatka | pg_dump→GPG→том, ротация; алёрт по `fail_kind` | cron.py:851-860; config.py:126-128 |
| `events-retention` | `hour=4, minute=0` → ежедневно **04:00** | TZ | удаление `events` старше 30 дней (idempotency cleanup, PII в payload) | cron.py:862-866 |
| `audit-log-retention` | `hour=4, minute=15` → ежедневно **04:15** | TZ | удаление `audit_log` старше `AUDIT_LOG_RETENTION_DAYS` (default 365) | cron.py:868-872 |
| `broadcast-draft-reaper` | `minute=37` → **ежечасно :37** | TZ | DRAFT-рассылки старше 30 мин → FAILED (SEC F5) | cron.py:878-882 |
| `threat-intel-refresh` | `minute=17` → **ежечасно :17** | TZ | refresh URLhaus+ThreatFox(+PhishTank); critical-алёрт если stale >6ч | cron.py:888-892 |
| `stale-operators-cleanup` | `hour=4, minute=20` → ежедневно **04:20** | TZ | деактивация операторов, покинувших служебную группу MAX | cron.py:898-902 |
| `health-selfcheck` | `minute=*/HEALTHCHECK_INTERVAL_MIN` (default 5) → **каждые 5 мин** | TZ | алёрт при heartbeat fresh↔stale | cron.py:904-910 |
| `monthly-stats` | `day=1, hour=9, minute=0` → **1-го числа 09:00** | TZ | XLSX-отчёт за месяц в админ-чат | cron.py:912-916 |
| `startup-pulse` | `DateTrigger(now+5s)` → **однократно +5 сек после старта** | TZ | catch-up pulse при рестарте | cron.py:921-927 |
| `pulse-hourly` | `minute=5` → **каждый час :05, 24/7** | TZ | базовый heartbeat «бот работает» | cron.py:949-956 |
| `pulse-workhours-extra` | `day_of_week=mon-fri, hour=9-17, minute=35` → **пн–пт 09:35–17:35** | TZ | доп-пинг в рабочее время | cron.py:957-966 |
| `appeals-5y-retention` | `hour=4, minute=45` → ежедневно **04:45** | TZ | обнуление текста обращений старше 5 лет | cron.py:968-972 |
| `pdn-retention` | `hour=4, minute=30` → ежедневно **04:30** | TZ | обезличивание жителей через 30 дней после revoke (152-ФЗ ст.21 ч.5) | cron.py:974-978 |
| `funnel-watchdog` | `minute=15` → **ежечасно :15** | TZ | сброс зависших анкет воронки (>24ч); алёрт при ≥5/час | cron.py:981-985 |
| `open-reminder-workhours` | `day_of_week=mon-fri, hour="9-11,13-17", minute=10` → **пн–пт :10 (кроме обеда 12ч)** | TZ | напоминание об открытых обращениях | cron.py:996-1002 |
| `overdue-reminder-workhours` | `day_of_week=mon-fri, hour="9-11,13-17", minute=40` → **пн–пт :40 (кроме обеда 12ч)** | TZ | напоминание о просроченных по SLA | cron.py:1003-1009 |
| `healthcheck-ping` | `minute=*/HEALTHCHECK_INTERVAL_MIN` (5) — **условно**, только если задан `HEALTHCHECK_URL` | TZ | внешний ping (Healthchecks.io/Uptime Kuma) | cron.py:1013-1020 |

Общие параметры всех job (цикл регистрации, cron.py:1022-1029): `max_instances=1`, `coalesce=True`, `misfire_grace_time=120` сек (`_MISFIRE_GRACE_SEC`, cron.py:49).

**⚠️ Расхождение docs-реестра с кодом (зафиксировать, это реальный дефект тир-2):**
- `cron_registry.py:63-64` для `open-reminder-workhours` пишет «пн–пт 09:00–11:59 **и 13:00–17:59**», и аналогично `overdue` (`:68`) — это совпадает с кодом (`hour="9-11,13-17"`). ОК.
- А вот **`calendar_ru.is_workday` (calendar_ru.py:70-77) считает рабочими днями пн–сб** (`weekday() != 6`, исключено только воскресенье), и docstring `_job_working_hours_open_reminder` (cron.py:751) говорит «пн–сб». **Но фактический `CronTrigger` у обеих reminder-задач — `day_of_week="mon-fri"`** (cron.py:988, 995). То есть суббота отсечена триггером, и `is_workday` для субботы не отрабатывает на этих задачах. Docstring на cron.py:751 («пн–сб») устарел относительно своего же триггера. На поведение reminder это не влияет (триггер строже), но текст вводит в заблуждение.

## 2. Производственный календарь РФ

- **Путь к файлу праздников:** `/app/seed/holidays.json` — жёстко зашит как `HOLIDAYS_PATH = Path("/app/seed/holidays.json")` (calendar_ru.py:24). Реальный файл репозитория: `seed/holidays.json`.
- **`is_workday(d)`** (calendar_ru.py:70-77): `return d.weekday() != 6 and not is_holiday(d)` — рабочий день = НЕ воскресенье И не праздник. (Суббота формально считается рабочей в этой функции.)
- **Покрытые годы:** **2026 и 2027** (holidays.json:4-13 и 14-23). 2026 — 19 дат включая переносы (8–9 марта, 9–11 мая); 2027 — 16 дат. Фоллбэк: если файла нет или он битый — `frozenset()` (пустой), деградация к «weekend-only» с WARNING в лог (calendar_ru.py:35-46).
- **Подавление в праздники:** ДА для напоминалок — обе reminder-job вызывают `if not is_workday(...): return` (cron.py:762, 797). НЕТ для pulse и retention: pulse-задачи к календарю не привязаны (комментарий cron.py:933-937 «госпраздники в пульсе не учитываются»), retention тоже не привязан (docstring модуля calendar_ru.py:8-11 «ретенция ПДн обязана работать в выходные, срок 30 дней не делает каникул»). Подтверждается тем, что `is_workday` импортируется только в cron.py (calendar_ru.py:8) и используется лишь в двух reminder-job.

## 3. health.py — эндпоинты и порт

Эндпоинты регистрируются в `start()` (health.py:207-209):
- **`/livez`** (`_livez`, health.py:180-183) — liveness. Только heartbeat (`heartbeat.is_fresh()`), БД НЕ трогает (`include_db=False`). Используется Docker healthcheck, watchdog, auto-deploy gate.
- **`/readyz`** (`_readyz`, health.py:186-188) — readiness. heartbeat + `SELECT 1` к БД (`include_db=True`, `_ping_db_cached`, TTL 10 сек). Для диагностики, не для авто-рестарта.
- **`/healthz`** (`_healthz`, health.py:191-198) — backward-compat alias на `_readyz` (heartbeat + DB).
- Ответ 200 если `ok`, иначе **503** (health.py:177, `_status_response`). Локальным запросам (127.0.0.1/::1/localhost, `_is_local_request` health.py:128-135, проверка на :164) отдаётся полная диагностика (`heartbeat_fresh`, `last_beat_age_seconds`, `db_ok`); внешним — только `{"ok": ...}` (health.py:174-175).

**Порт:** дефолт сигнатуры `start(host="0.0.0.0", port=8080)` (health.py:201-204), но фактически вызывается из `main.py:717-718` с `host=settings.webhook_host, port=settings.webhook_port`. `webhook_port` = `Field(8080, alias="WEBHOOK_PORT")` (config.py:21), `webhook_host` = `"0.0.0.0"` (config.py:20). В compose наружу пробрасывается только loopback: `"127.0.0.1:8080:8080"` (docker-compose.yml:70), и Docker healthcheck бьёт `/livez` (docker-compose.yml:80).

## 4. db/models.py — таблица настроек + SCHEMA

- **`__tablename__ = "settings"`** для `class Setting(Base)` (models.py:271-272). Ожидание подтверждено — таблица называется `settings`, НЕ `app_settings`. PK — `key: String(64)` (models.py:274).
- **`settings_store.SCHEMA` — ровно 17 ключей** (settings_store.py:432-488), подтверждено импортом (`len(SCHEMA)==17`, анти-дрейф `test_ledger_facts_sync.py`). Полный перечень: `welcome_text`, `consent_text`, `commit_author_name`, `commit_author_email`, `policy_url`, `electronic_reception_url`, `udth_schedule_url`, `udth_schedule_intermunicipal_url`, `appointment_text`, `emergency_contacts`, `transport_dispatcher_contacts`, `topics`, `broadcast_max_images`, `admin_quiet_hours_enabled`, `admin_quiet_hours_start`, `admin_quiet_hours_end`, `localities`. `DEFAULTS` тоже 17 ключей (settings_store.py:369-427); `SYNCED_KEYS` — 9 (settings_store.py:740-750, без `commit_author_*`, `welcome_text`, `consent_text`, `broadcast_max_images`, `admin_quiet_hours_*`).

`appointment_text` дефолт (settings_store.py:387-392) содержит «Елизовского **муниципального района**» и «**А.С. Гончаровым**» — соответствует решению владельца (осознанный seed), не ошибка.

## 5. docker-compose.yml — как DATABASE_URL попадает в бота

Механика — **Compose-интерполяция переменных хоста (`${VAR:-default}` / `${VAR:?error}`), НЕ envsubst и НЕ ручная синхронизация.** Две раздельные точки:

- **Postgres-контейнер** получает три отдельные переменные (docker-compose.yml:28-31): `POSTGRES_DB: ${POSTGRES_DB:-aemr}`, `POSTGRES_USER: ${POSTGRES_USER:-aemr}`, `POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in infra/.env}` (пароль без дефолта — compose падает, если не задан в `infra/.env`).
- **Бот** получает уже **готовый** `DATABASE_URL`, собранный Compose-интерполяцией из тех же переменных (docker-compose.yml:64-65):
  ```
  DATABASE_URL: postgresql+asyncpg://${POSTGRES_USER:-aemr}:${POSTGRES_PASSWORD:?...}@db:5432/${POSTGRES_DB:-aemr}
  ```
  То есть один и тот же набор `POSTGRES_*` из `infra/.env` подставляется в обе секции. Хост БД жёстко `db:5432` (имя сервиса в compose-сети), порт 5432. Дефолты user/db — `aemr`. Бот читает `DATABASE_URL` как обязательное поле (config.py:23, `Field(..., alias="DATABASE_URL")`). Дополнительно бот подключает `env_file: .env` (docker-compose.yml:63) для остальных переменных. Синхронизация user/password/db между Postgres и ботом гарантируется тем, что обе строки ссылаются на одни и те же `${POSTGRES_*}` — рассинхрон невозможен by construction.

## 6. infra/Dockerfile — pip или uv

- **Зависимости ставятся из `uv.lock` с хеш-проверкой каждого пакета** (Dockerfile:39-45). Механика: `COPY bot/pyproject.toml` + `COPY bot/uv.lock`, затем `uv export --frozen --no-emit-project --no-dev --format requirements-txt` разворачивает lock в `requirements.txt` с `--hash` на каждый wheel, и `pip install --require-hashes --no-deps` ставит строго их (fail-closed: один неверный хеш — build падает). `uv` тянется build-time из `COPY --from=ghcr.io/astral-sh/uv` (пиннутый digest, не `curl|sh`) и удаляется из финального образа (Dockerfile:45). Сам пакет ставится `pip install -e /app --no-deps` (Dockerfile:50) — без повторного резолва deps.
- **`uv.lock` — источник истины образа** (Dockerfile:40 его копирует, Dockerfile:42 экспортирует). База: `python:3.12-slim` (pinned by digest, Dockerfile:1). Запуск: `alembic upgrade head && python -m aemr_bot.main` под non-root `botuser` UID/GID 1000. _(Ревизия 2026-06-04: ранее тут значилось «pip из диапазонов, uv.lock не копируется» — Dockerfile с тех пор мигрировал на lock+hash; откат ловит анти-дрейф `test_ledger_facts_sync.py::test_dockerfile_uses_uv_lock_with_hashes`.)_

## 7. services/db_backup.py — формат, лимиты, поведение

- **Формат имени файла:** `aemr-{YYYYMMDD_HHMMSS}{suffix}`, где timestamp по TZ бота, `suffix = ".sql.gpg"` при шифровании, `.sql` без (db_backup.py:303-305): `ts = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")`, `out = target_dir / f"aemr-{ts}{suffix}"`. Пример: `aemr-20260601_030000.sql.gpg`. Права файла ужесточаются до `0600` после записи (db_backup.py:316).
- **`BACKUP_KEEP_COUNT` = 8** (config.py:134, `Field(8, alias="BACKUP_KEEP_COUNT")`), комментарий «8 еженедельных ≈ 2 месяца». Ротация по mtime, удаляет файлы сверх `keep` (db_backup.py:95-109, вызов на :349).
- **Минимальная длина passphrase — порог отказа: 12 символов** (db_backup.py:273-280). Если `1 ≤ len < 12` → лог `ERROR`, `passphrase` сбрасывается в `""` (бэкап продолжается БЕЗ шифрования, далее попадает под проверку opt-in).
- **Поведение без passphrase:** `encrypt = bool(passphrase)` (db_backup.py:281). Если шифрования нет И `BACKUP_ALLOW_UNENCRYPTED` не выставлен (default `False`, config.py:142-144) → бэкап **ОТКАЗАН**: `BackupResult(path=None, fail_kind="config", ...)` с сообщением про 152-ФЗ (db_backup.py:287-302). Plain-text дамп с PII создаётся ТОЛЬКО при явном `BACKUP_ALLOW_UNENCRYPTED=1` (dev/local). Категории провала (`fail_kind`): `config` / `pg_dump` / `gpg` / `unknown` (db_backup.py:61-66); `backup_db()` никогда не бросает исключение — `_job_backup_with_alert` читает `fail_kind` и шлёт categorized critical-алёрт (cron.py:165-234). Шифрование: `gpg --symmetric --cipher-algo AES256`, passphrase через `os.pipe` (не argv), `--homedir` в `$TMPDIR/.gnupg` под read-only rootfs (db_backup.py:175-201).

---

### Сводка ключевых «факт — file:line — значение»
- Расписание бэкапа — config.py:126-128 + cron.py:853-857 — вс 03:00 (BACKUP_DAY_OF_WEEK=sun / HOUR=3 / MINUTE=0).
- `is_workday` — calendar_ru.py:70-77 — `weekday()!=6 and not is_holiday`.
- Праздники — seed/holidays.json:4,14 — годы 2026 и 2027.
- Health-порт — config.py:21 — `webhook_port=8080` (WEBHOOK_PORT); вызов main.py:717-718.
- Таблица настроек — models.py:272 — `"settings"`.
- SCHEMA — settings_store.py — 23 ключа (было 17, +6 `admin_notify_*` 2026-07-09; проверено импортом, анти-дрейф `test_ledger_facts_sync.py`).
- DATABASE_URL — docker-compose.yml:65 — Compose-интерполяция `${POSTGRES_*}`.
- Установка зависимостей — Dockerfile:39-50 — `uv export --frozen` из `uv.lock` → `pip install --require-hashes`, пакет `pip install -e --no-deps` (uv.lock = источник истины, не декоративен).
- Имя бэкапа — db_backup.py:304-305 — `aemr-%Y%m%d_%H%M%S.sql[.gpg]`.
- BACKUP_KEEP_COUNT — config.py:134 — 8.
- Порог passphrase — db_backup.py:274 — `< 12` символов.
- Без passphrase — db_backup.py:287-302 — отказ (`fail_kind="config"`), если нет `BACKUP_ALLOW_UNENCRYPTED=1`.

**Один реальный дефект для бэклога (не из вашего списка вопросов):** docstring `_job_working_hours_open_reminder` (cron.py:751) утверждает «пн–сб», но фактический триггер обеих reminder-задач — `day_of_week="mon-fri"` (cron.py:988, 995). Расхождение текста с поведением; на работу не влияет, но при доках введёт в заблуждение.

---

## Разработчик + Безопасность

## Раздел «Разработчик»

**1. Миграции Alembic — 22 файла, последняя `0022_users_consent_text_hash`** _(ревизия 2026-07-20: ранее 21/0021, добавлена 0022 — хеш редакции согласия для доказуемости по 152-ФЗ ст. 9 ч. 3)_
`bot/aemr_bot/db/alembic/versions/` — полный список (0001..0022):
0001_initial, 0002_broadcast, 0003_phone_normalized, 0004_indexes_and_autovacuum, 0005_appeals_locality, 0006_consent_revoked_at, 0007_consent_broadcast_anonymous, 0008_backfill_consent_broadcast, 0009_partial_indexes_for_hot_paths, 0010_pg_ops_hardening, 0011_wizard_state_persistence, 0012_messages_appeal_created_index, 0013_settings_synced_at, 0014_broadcasts_attachments, 0015_broadcast_templates, 0016_broadcast_template_usage, 0017_appeals_last_card_mid, 0018_users_trigram_search_indexes, 0019_appeals_status_created_index, 0020_appeals_coordinates, 0021_drop_appeals_coordinates, 0022_users_consent_text_hash.

**2. ORM-модели — 11, ровно как в списке**
`bot/aemr_bot/db/models.py`: User (models.py:61), Operator (:168), Appeal (:182), Message (:232), Event (:259), AuditLog (:272), Setting (:283), Broadcast (:305), BroadcastDelivery (:339), WizardState (:355), BroadcastTemplate (:389). Классы `Base` (:9) и StrEnum-перечисления (DialogState, AppealStatus, OperatorRole, MessageDirection, BroadcastStatus) — не таблицы. _(ревизия 2026-07-09: Appeal получил `__table_args__` с композитным индексом (status, created_at) — номера строк последующих классов сдвинулись; значения и состав те же. Анти-дрейф числа 11 — `test_ledger_facts_sync.py`.)

**3. Зависимости / coverage gate**
- maxapi `~=1.1` — pyproject.toml:9 (подтверждено).
- fastapi `>=0.118,<0.137` — pyproject.toml:16 (не «`<0.136`», а `<0.137`; коммент о 0.136.3 устарел против пина — мелкое расхождение, флагаю).
- Прочие ключевые: uvicorn[standard] ~=0.32 (:17), sqlalchemy ~=2.0 (:18), asyncpg ~=0.30 (:19), alembic ~=1.14 (:20), apscheduler ~=3.10 (:21), pydantic ~=2.9 (:22), pydantic-settings ~=2.6 (:23), shapely ~=2.0 (:30).
- dev-deps: pytest `>=9.0.3,<10` (:37), pytest-asyncio `>=1.3,<2` (:41), pytest-cov `>=6,<8` (:42), ruff ~=0.7 (:43), mypy `>=1.13,<3.0` (:44), bandit ~=1.7 (:45), pip-audit ~=2.7 (:46), hypothesis ~=6.115 (:53).
- `--cov-fail-under=84` — `.github/workflows/ci.yml:148` (КОД=истина: gate поднят до 84; ранее в леджере значилось 67 на ci.yml:139 — устарело).

**4. AUDIT_LOG_RETENTION_DAYS**
config.py:180-182 — дефолт **365**, диапазон `ge=30, le=3650` (30..3650 дней). _(ревизия 2026-06-04: ранее значилось config.py:129-130.)_

---

## Раздел «Безопасность»

**5. Модель удаления ПДн**
- `ANONYMOUS_MAX_USER_ID = -1` — models.py:38 (sentinel технической записи anonymous-user).
- `erase_pdn` — users.py:296 (bool-wrapper); `erase_pdn_detailed` — users.py:307. Процедура (по докстрингу :310-330 и коду):
  - **Очищается** свободный текст/вложения по обращениям: `appeals.address, appeals.summary, messages.text, attachments` через `_redact_appeal_payloads_for_user` (вызов users.py:366) — именно там ПДн.
  - **Сохраняется**: метаданные/статистика количества обращений — обращения переподвешиваются на anonymous-запись `UPDATE appeals.user_id = anonymous_id` (users.py:371-375); открытые NEW/IN_PROGRESS закрываются с `closed_due_to_revoke=true` (users.py:349-362).
  - **Физически удаляется** строка `users`: `delete(User).where(User.id == user_row)` — users.py:377. Это не обезличивание-флагом, а DELETE.
  - Отличие от `is_blocked`: `is_blocked` — IT-блокировка за злоупотребления, ставится через `set_blocked` (users.py:418), при `revoke_consent` НЕ ставится (users.py:398 «is_blocked НЕ ставится: житель может передумать»). Anonymous-«могильная» запись `first_name="Удалено", is_blocked=True` создаётся отдельно для retention-обезличивания (users.py:264-266) — это другой путь, не сам erase.

**6. threat_intel — источники, is_stale, critical-алёрт, NFKC**
- Источники: **URLhaus + ThreatFox + опц. PhishTank** (если задан `PHISHTANK_APP_KEY`). threat_intel.py:42 `_URLHAUS_URL = https://urlhaus.abuse.ch/downloads/csv_online/`, :43 `_THREATFOX_URL = https://threatfox.abuse.ch/downloads/hostfile/`, :44 `_PHISHTANK_URL_TEMPLATE`; PhishTank активируется только при ключе (:235-241). **Kaspersky/OpenTIP ОТСУТСТВУЕТ** — подтверждено: ни одного упоминания kaspersky/opentip в threat_intel.py (задача #89 его планировала, но в коде нет; реализована только триада abuse.ch).
- `is_stale()` — threat_intel.py:101 (`age > _STALENESS_BUDGET_SEC`); вспомогательный `staleness_age_seconds()` — :91.
- Critical-алёрт при устаревании (PR #166): cron.py:326-341 — `if store.is_stale()` (cron.py:332) → `_send_admin_text_with_retry(..., context="threat-intel-stale", critical=True)`; `critical=True` пробивает quiet hours (комментарий cron.py:309-310). Job `threat-intel-refresh` зарегистрирован cron.py:888-892, обработчик `_job_threat_intel_refresh` — cron.py:298.
- NFKC-нормализация в `extract_urls`: **settings_store.py:184** `text = unicodedata.normalize("NFKC", text)` (полноширинные/совместимые символы → канон; кириллические гомоглифы NFKC не трогает — их ловит `_QUASI_URL_PATTERN`).

**7. Dormant-функционал**
- **Sentry — полностью reverted, следов в коде НЕТ.** Файла `observability/sentry.py` не существует; по всему `bot/` ноль совпадений `sentry`; в `config.py` поля sentry нет (нет `SENTRY_DSN`). Упоминания остались только в документации (`docs/site/...`, `docs/_meta/...`), не в коде. Это согласуется с PR XIII (задача #139 «revert Sentry полностью») поверх #130/#134. То есть для документации точная формулировка — «Sentry удалён из кода (был в PR VII–X, откачен PR XIII)», а не «present-but-disabled no-op».
- **Внешний мониторинг (HEALTHCHECK_URL) — present-but-optional, активный код.** `healthcheck_url: str | None = Field(None, alias="HEALTHCHECK_URL")` — config.py:172. Job `healthcheck-ping` добавляется в расписание только если URL задан: `if settings.healthcheck_url:` — cron.py:1013; пинг `_ping_healthcheck` (cron.py:1070) при пустом URL делает ранний return (cron.py:1072-1073). Интервал — `HEALTHCHECK_INTERVAL_MIN` (дефолт 5, config.py:55). Это рабочий dormant-by-default, а не reverted.

Расхождения с ожиданиями: (а) fastapi верхняя граница `<0.137`, а не `<0.136`; (б) Sentry не «no-op present-but-disabled», а полностью отсутствует в коде — обе формулировки в целевой документации надо поправить под код.

---

---

# ЧАСТЬ II — РЕШЕНИЯ И РЕКОМЕНДАЦИИ (с обоснованием)

_Ниже — обоснованные вердикты по открытым вопросам проекта: не «факты-из-кода», а рекомендации с правовой и инженерной аргументацией. Статус: uv-в-Docker — на решении владельца; срок audit_log — применён к Политике v2; спецификация telegram_analytics — план следующей фазы._

---

## РЕШЕНИЕ — uv vs pip в Docker ✅ ПРИМЕНЕНО (2026-06-04)

> **Статус: реализовано, причём надёжнее рекомендации.** Образ уже ставит зависимости из `uv.lock` — не через `uv sync`, а через `uv export --frozen` → `pip install --require-hashes` (явная хеш-верификация каждого пакета, см. Часть I §6). Дрейф «lock декоративен» закрыт. Остаточный пункт — CI: `ci.yml` всё ещё `pip install -e ".[dev]"` из диапазонов (тестируем не то, что деплоим) — это «отдельный PR» из разбора ниже. Сам разбор сохранён как обоснование решения.

# Рекомендация (выполнена): ставить зависимости образа из `uv.lock`. Однозначно — да.

## Короткий вердикт

Мигрировать infra/Dockerfile с `pip install -e /app` на multi-stage `uv sync --frozen`. Риск миграции — низкий, выигрыш — закрытие дыры, которая уже один раз положила бота в проде. Сейчас `bot/uv.lock` существует, закоммичен (2269 строк, `revision = 3`), объявлен в DEPS.md единственным источником истины — но **ни Docker, ни CI его не читают**. Lock-файл сегодня декоративный.

## Главный аргумент — это не «скорость/размер», это устранение реального drift'а

Весь deps-аппарат проекта построен вокруг `uv.lock`, а два места, которые порождают работающие артефакты, его игнорируют:

- `infra/Dockerfile:24` — `pip install -e /app` ставит из **диапазонов** pyproject.toml (`maxapi~=1.1`, `fastapi>=0.118,<0.137`), резолвит транзитивные версии заново на момент сборки.
- `.github/workflows/ci.yml:28,130` — `pip install -e ".[dev]"`, тоже мимо lock.

При этом guard-тест `bot/tests/test_deps_environment.py:39` (`test_maxapi_version_matches_lock`) утверждает `installed == "1.1.0"` из lock. **Этот тест сейчас зелёный по случайности** — потому что `~=1.1` пока резолвится в 1.1.0. В день, когда выйдет maxapi 1.2.0, `pip install` в Docker подтянет его молча, lock останется на 1.1.0, а тест в CI (который тоже ставит мимо lock) либо покраснеет, либо пропустит — рассинхрон. Это буквально воспроизведение инцидента PR #48-50, описанного в шапке самого guard-теста: «тестировал на 1.0.0, в Docker 0.9.18 — `TypeError: ClientSession got max_retries`».

Документация (DEPS.md, файл `test_deps_environment.py`, dependabot.yml) **уже считает, что lock — закон**. Сейчас образ ему не подчиняется. Это противоречие «код vs док», где побеждать должен код — но здесь правильнее подтянуть код к доку, потому что инцидент-история и guard-тест доказывают: намерение владельца — детерминизм по lock.

## Плюсы перехода (для ЭТОГО проекта)

**Детерминизм (главное).** `uv sync --frozen` ставит ровно то, что в `uv.lock`, без резолвинга. Локальная среда (`uv sync --extra dev`, как в DEPS.md), CI и образ дают побитово одинаковые версии. Drift «у меня работает / на проде падает» закрывается на уровне инфраструктуры, а не надеждой на guard-тест.

**Авто-деплой каждые 10 минут становится безопаснее.** `scripts/auto-deploy.sh:73` делает `docker compose up -d --build` от cron. Сейчас любая сборка может молча подтянуть новый патч транзитивной зависимости (например, обновлённый `aiohttp` или `starlette`), потому что pip резолвит заново. С `--frozen` пересборка детерминирована: меняется только то, что в коммите изменило `uv.lock`. Для каждые-10-минут auto-rebuild это критично — иначе образ дрейфует без единой строки в git diff.

**Согласованность с dependabot.** `.github/dependabot.yml` уже обновляет Python-зависимости и сам бампит lock в PR. Если образ ставит из lock — деплоится ровно то, что dependabot протестировал в PR. Если из диапазонов — dependabot бампит lock, а образ всё равно может взять другое.

**Скорость сборки.** uv ставит зависимости на порядок быстрее pip; с cache-mount (`--mount=type=cache,target=/root/.cache/uv`) скачанные wheels переживают пересборки. На auto-deploy каждые 10 минут пересборка случается часто — даже если слой зависимостей закеширован, повторная установка через uv заметно дешевле. Это не главный аргумент, но бесплатный бонус.

**Размер образа.** Multi-stage (uv-бинарь и кэш только в builder-стейдже, в финал копируется готовый `.venv`) даёт чуть меньший образ. Второстепенно для self-host VPS, но идёт в комплекте.

## Минусы и риск миграции — оба низкие

**`uv` в образе.** Тянется одной строкой `COPY --from=ghcr.io/astral-sh/uv:0.11.17`, без `curl | bash` — что прямо совпадает с уже принятым в Dockerfile решением (комментарий на строках 11-14: rclone берётся из apt именно чтобы не исполнять `curl rclone.org/install.sh | bash` как root). uv через `COPY --from` — тот же принцип: никакого исполнения сетевого инсталлятора.

**`uv.lock` надо скопировать в build-контекст.** Сейчас Dockerfile копирует только `pyproject.toml` (строка 23). `.dockerignore` lock не исключает (исключает только кэши, docs, .env) — значит он доступен в контексте `context: ..` из docker-compose.yml:61. Правка тривиальна.

**`read_only: true` совместим.** docker-compose.yml:92 монтирует rootfs read-only, `/tmp` через tmpfs. uv ставит всё в build-time (в стейдже), в рантайме `.venv` уже готов и неизменен — read-only рантайму не мешает. Комментарий в compose «pip wheels use /tmp during runtime» (строка 88) уже сейчас **устаревший** (рантайм ничего не ставит); после миграции он станет окончательно неверным — заодно поправить.

**Root-пакет.** В pyproject.toml нет `[tool.uv]`, значит действует дефолт `package = true` — `aemr-bot` собирается как wheel через setuptools (build-system уже задан, строки 56-58). В multi-stage с `--no-editable` пакет ставится как обычный wheel, без editable-симлинка. Поведение эквивалентно текущему `pip install -e` минус символическая ссылка на исходники — для рантайма разницы нет, наоборот чище (рантайм-образ не зависит от наличия исходников рядом).

**Главный риск — `CMD` и `alembic` в PATH.** Сейчас `CMD` зовёт голые `alembic` и `python` (строка 40), которые pip кладёт в системный PATH. После перехода на `.venv` нужно либо добавить `.venv/bin` в PATH, либо звать через `uv run`. Я закладываю `ENV PATH="/app/.venv/bin:$PATH"` — тогда `CMD` остаётся почти как есть. Это единственное место, где легко ошибиться; в диффе ниже учтено.

Откат тривиален: `git revert`, плюс auto-deploy уже имеет health-gate с авто-rollback (`auto-deploy.sh:97-119`) — если образ не поднимет `/livez` за 60 секунд, сервер сам откатится на предыдущий коммит. То есть даже неудачная миграция self-heal'ится.

## Почему НЕ остаться на pip

Остаться на pip — значит сознательно держать `uv.lock` декоративным, а guard-тест — зелёным по совпадению. Это противоречит DEPS.md, который владелец уже принял как канон, и оставляет открытой ровно ту дыру (drift между «протестировано» и «задеплоено»), которая дала инцидент PR #48-50. Единственное, что оправдывало бы pip — если бы lock не существовал или не поддерживался. Но он есть, закоммичен, и dependabot его автоматически бампит. Половинчатое состояние «lock есть, но образ его игнорирует» — худший из трёх вариантов: вся цена ведения lock без выгоды детерминизма.

(Замечание на будущее, вне этого диффа: CI стоило бы перевести на тот же `uv sync --frozen` — тогда «протестировано» и «собрано» используют один механизм. Сейчас фокус на образе, как и просил владелец; CI — отдельным PR, чтобы не мешать.)

## Конкретный дифф infra/Dockerfile

Multi-stage, по официальному паттерну Astral (`docs.astral.sh/uv/guides/integration/docker`), адаптированный под текущие реалии образа: non-root UID/GID 1000, apt-слой с postgresql-client/rclone/tzdata, `/backups`, seed-файлы, `read_only` рантайм.

Полная замена файла:

```dockerfile
# ---- Builder stage: резолв и установка зависимостей по uv.lock ----
FROM python:3.12-slim@sha256:4386a385d81dba9f72ed72a6fe4237755d7f5440c84b417650f38336bbc43117 AS builder

# uv тащим через COPY --from, не `curl | bash` — тот же принцип, что и
# rclone из apt ниже: никакого исполнения сетевого инсталлятора в образе.
# Версия запинена (best practice Astral для воспроизводимых сборок).
COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# 1) Сначала только зависимости (без самого пакета) — слой кешируется и
#    пересобирается лишь когда меняется uv.lock / pyproject.toml.
#    --frozen: ставить строго из lock, не трогать и не перерезолвивать его.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=bot/uv.lock,target=uv.lock \
    --mount=type=bind,source=bot/pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev --no-editable

# 2) Затем исходники + установка самого пакета aemr-bot как wheel
#    (--no-editable: без симлинка на исходники, чтобы в финал скопировать
#    только .venv).
COPY bot/pyproject.toml /app/pyproject.toml
COPY bot/aemr_bot /app/aemr_bot
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---- Runtime stage: тонкий образ без uv и без кэша ----
FROM python:3.12-slim@sha256:4386a385d81dba9f72ed72a6fe4237755d7f5440c84b417650f38336bbc43117

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Kamchatka \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client gnupg ca-certificates curl tzdata unzip rclone \
    && rm -rf /var/lib/apt/lists/*
# rclone берём из debian apt вместо `curl rclone.org/install.sh | bash`:
# любой будущий компромет домена rclone.org исполнялся бы как root в
# образе. Версия из apt чуть старше (1.60 на bookworm), но достаточна
# для S3-аплоада бэкапов; функциональность не урезана.

# Non-root user. Fixed UID/GID so the named volume `backups` keeps the
# same ownership across rebuilds and on hosts where bind-mounts are used.
RUN groupadd --system --gid 1000 botuser \
    && useradd --system --uid 1000 --gid 1000 --create-home --home-dir /home/botuser botuser

WORKDIR /app

# Готовый виртуалэнв из builder-стейджа: зависимости + сам aemr-bot.
COPY --from=builder /app/.venv /app/.venv

COPY bot/alembic.ini /app/alembic.ini
COPY bot/aemr_bot /app/aemr_bot
COPY seed /app/seed
COPY docs/PRIVACY.pdf /app/seed/PRIVACY.pdf

# /backups is the named volume mount target. Pre-create it so chown runs
# at build time; compose will mount over it but ownership of the mount
# point stays for the volume.
RUN mkdir -p /backups && chown -R botuser:botuser /app /backups

ENV SEED_DIR=/app/seed

USER botuser

# alembic и python берутся из /app/.venv/bin (см. PATH выше).
CMD ["sh", "-c", "alembic upgrade head && python -m aemr_bot.main"]
```

### Что изменилось построчно

- **Двухстейджевая сборка**: builder тащит uv + резолвит `.venv` по lock; runtime получает только готовый `.venv` — без uv, без кэша.
- **`uv.lock` копируется** через bind-mount в builder (был не нужен pip, теперь обязателен).
- **`bot/aemr_bot` копируется до второго `uv sync`** в builder (чтобы собрать wheel пакета) и **повторно в runtime** — потому что `--no-editable` ставит пакет в `.venv` как wheel, но исходники модуля `python -m aemr_bot.main` всё равно ожидает рядом. Альтернатива — не копировать второй раз и положиться на установленный wheel; копирование исходников и в рантайм оставлено для совместимости с текущим запуском `python -m aemr_bot.main` и alembic-миграциями, которые читают `aemr_bot/`.
- **`PATH="/app/.venv/bin:$PATH"`** — чтобы голые `alembic`/`python` в `CMD` резолвились из venv. `CMD` не меняется.
- **`PIP_NO_CACHE_DIR` убран** — pip больше не используется в рантайме.
- **`--no-dev`** — dev-группа (pytest, ruff, mypy, bandit, hypothesis) в прод-образ не попадает (раньше Dockerfile ставил `pip install -e /app` без `[dev]`, то есть тоже без dev — поведение сохранено, но теперь явно).
- **Комментарий про «pip wheels use /tmp»** в docker-compose.yml:88 станет окончательно неверным — поправить отдельной строкой при мердже (это не в Dockerfile, отметка для владельца).

### Обязательная проверка перед мерджем (по ритуалу DEPS.md §5)

```bash
cd infra
docker compose build bot          # должен собраться без ошибок резолва
docker compose up bot             # alembic upgrade head проходит, /livez → 200, нет ImportError
```

Если `uv.lock` хоть на одну версию расходится с pyproject-диапазонами — `uv sync --frozen` **упадёт на этапе сборки** (это фича, не баг: ловит рассинхрон до прода, а не после). Тогда `cd bot && uv lock` локально, закоммитить обновлённый lock, пересобрать.

## Файлы, относящиеся к задаче (абсолютные пути)

- `C:\Users\filat\Documents\aemr-bot\infra\Dockerfile` — цель правки (строки 23-24: `COPY pyproject.toml` + `pip install -e`).
- `C:\Users\filat\Documents\aemr-bot\bot\uv.lock` — источник истины, существует, 2269 строк, `revision = 3`.
- `C:\Users\filat\Documents\aemr-bot\bot\pyproject.toml` — диапазоны зависимостей, нет `[tool.uv]` (дефолт `package = true`).
- `C:\Users\filat\Documents\aemr-bot\docs\DEPS.md` — декларирует lock каноном; после миграции образ ему наконец соответствует.
- `C:\Users\filat\Documents\aemr-bot\bot\tests\test_deps_environment.py:39` — guard-тест, сейчас зелёный по совпадению; после миграции образ реально гарантирует то, что тест утверждает.
- `C:\Users\filat\Documents\aemr-bot\.github\workflows\ci.yml:28,130` — CI тоже ставит мимо lock (отдельный PR, вне этого диффа).
- `C:\Users\filat\Documents\aemr-bot\scripts\auto-deploy.sh:73` — `docker compose up -d --build` каждые 10 минут; именно здесь детерминизм образа окупается.
- `C:\Users\filat\Documents\aemr-bot\infra\docker-compose.yml:88,92` — `read_only: true` (совместим) + устаревший комментарий про pip/tmp (поправить).
- `C:\Users\filat\Documents\aemr-bot\.github\dependabot.yml` — уже бампит lock автоматически; согласуется с `--frozen`.

Источники по uv-в-Docker (2026):
- [Using uv in Docker — официальный гайд Astral](https://docs.astral.sh/uv/guides/integration/docker/)
- [astral-sh/uv-docker-example — эталонный Dockerfile](https://github.com/astral-sh/uv-docker-example/blob/main/Dockerfile)
- [uv releases — текущая стабильная 0.11.17 (28.05.2026)](https://github.com/astral-sh/uv/releases)

---

## РЕШЕНИЕ — срок журнала audit_log по 152-ФЗ

_Факты сверены по коду, правовые нормы — по КонсультантПлюс/ФСТЭК._

---

# РЕШЕНИЕ: `AUDIT_LOG_RETENTION_DAYS=365` (оставить как в коде). 5 лет — НЕ применять.

## 1. Что такое `audit_log` (источник истины — код)

`db/models.py:260-268` — таблица `audit_log` хранит **действия операторов**, не сами обращения граждан: `operator_max_user_id` (кто), `action` (что: block/unblock/reopen/close/erase/setting_update и пр.), `target` (над кем/чем), `details` JSONB (может содержать ПДн — например `setting_update` пишет before/after значений). Это **журнал доступа/действий с ПДн**, то есть журнал событий безопасности ИСПДн в смысле приказа ФСТЭК №21.

Критично для развязки: обращения граждан имеют **отдельный** ретеншн-механизм — `cron.py:569 _job_appeals_5y_retention` (5 лет, 04:45). `audit_log` (`cron.py:266`, 04:15) — это другой объект и другая задача. Старый черновик Политики п.7.5 ошибочно «согласовал» срок audit_log со сроком обращений («Срок согласован со сроком хранения обращений (пункт 7.2)», `Политика_v2.md:125`). Эта привязка юридически ложная — у двух сущностей разные правовые основания.

## 2. Правовая база (проверено, без галлюцинаций)

**Никакой закон не задаёт жёсткий 5-летний срок для журнала действий операторов этого бота.** Разбор по нормам:

**152-ФЗ, ст.5 ч.7** (КонсультантПлюс, cons_doc_LAW_61801, ст.5): хранение ПДн — «**не дольше, чем этого требуют цели обработки**, если срок не установлен федеральным законом / договором». **Ст.21 ч.4**: ПДн **подлежат уничтожению по достижении целей** обработки. Для `details` с ПДн действует принцип минимизации — держать ровно столько, сколько нужно цели (расследование инцидента ИБ), и не дольше.

**Приказ ФСТЭК №21, мера РСБ.1** (это и есть профильная норма для журнала событий безопасности ИСПДн): срок хранения **оператор устанавливает сам**; критерий — записи должны «обеспечивать обнаружение, идентификацию и анализ инцидентов». Жёсткой цифры в базовой мере нет. В **требовании к усилению РСБ.1 п.4** установлен **минимум: «не менее трёх месяцев»** (если иное не установлено законодательством). То есть закон даёт **нижний порог 3 месяца**, а не 5 лет.

**ПП РФ №1119** (КонсультантПлюс, cons_doc_LAW_137356): задаёт **уровни защищённости** ИСПДн (4 уровня), а не сроки хранения журналов. Срока ретеншена там нет вообще.

**Постановление Администрации №2129 §1.2**: при работе с сообщениями из открытых источников **59-ФЗ не применяется**. Значит 5-летний срок, установленный оператором для обращений по аналогии с практикой их хранения (на него опирается `appeals_5y_retention`), к журналу действий операторов **юридически не привязан**. ПДн — только по 152-ФЗ → принцип минимизации.

**Вывод по праву:** жёсткого срока для audit_log нет. Есть пол (ФСТЭК — 3 мес.) и потолок-принцип (152-ФЗ — «не дольше цели»). 365 дней — внутри коридора с большим запасом над минимумом.

## 3. Обоснование выбора 365 (минимизация + операционная достаточность)

Раз закон не диктует цифру, она выбирается как **минимально достаточная для цели**. Цель audit_log — расследование инцидента ИБ/спора о действиях оператора. Типовой горизонт такого расследования и большинства проверок — **год** (годовой цикл, перекрывает сезонность, отпуска, отложенное обнаружение). 365 дней:
- **выше** обязательного минимума ФСТЭК (3 мес.) в 4 раза — соответствие РСБ.1 с запасом;
- **не избыточно** против ст.5 ч.7 / ст.21 152-ФЗ — ПДн в `details` не лежат «вечно», что и есть требуемая минимизация;
- параметр настраиваемый (`config.py:180-182`, диапазон 30–3650) — если конкретная проверка/инцидент потребует, срок поднимается через env без правки кода.

5 лет = хранение ПДн операторов в 5 раз дольше, чем требует цель, без правового основания (59-ФЗ исключён §1.2; ФСТЭК не требует; 152-ФЗ прямо против). Это **нарушение принципа минимизации**, а не «усиление защиты». Поэтому 5 лет отклоняется.

## 4. Что писать в Политике ПДн (чтобы текст совпал с кодом)

Заменить п.7.5 и п.11.3 черновика `Политика_v2.md`.

**Было** (`Политика_v2.md:125`): «Журнал действий (audit_log) — хранится **5 лет**… Срок согласован со сроком хранения обращений (пункт 7.2).»

**Стало** (предлагаемая формулировка):

> 7.5. **Журнал действий операторов (`audit_log`)** — записи о действиях операторов с персональными данными (кто, какое действие, над какими данными) хранятся **1 год (365 дней)** с момента события, после чего автоматически уничтожаются. Срок установлен оператором в соответствии с принципом ограничения хранения персональными данными (часть 7 статьи 5 Федерального закона № 152-ФЗ) и обеспечивает возможность обнаружения, идентификации и анализа инцидентов информационной безопасности (мера РСБ.1 приказа ФСТЭК России № 21); он превышает минимальный срок хранения сведений о событиях безопасности (не менее трёх месяцев). Срок является конфигурируемым параметром.

**Поправка п.11.3** (`Политика_v2.md:198`): убрать «хранятся 5 лет», заменить на «хранятся в течение срока хранения журнала действий (пункт 7.5)». Если владелец хочет, чтобы записи именно об инцидентах с ПДн (`action='pdn_incident'`) жили дольше года, это отдельное продуктовое решение — потребует выделенного срока в коде (сейчас все действия чистятся единым cutoff, `cron.py:281`), и тогда формулировку синхронизировать с реализацией. По умолчанию — единый год.

**Поправка чек-листа юриста** (`Политика_v2.md:234`): пункт «5 лет по audit_log» снять; зафиксировать «1 год по audit_log — реализовано в `cron.py::_job_audit_log_retention`».

## 5. Код менять не нужно

`config.py:180-182` уже = 365, диапазон 30–3650 корректен (нижняя граница 30 ниже ФСТЭК-минимума 90 дней, но это техническая граница «не выключить вовсе»; рабочий дефолт 365 ей не противоречит). Комментарии в коде (`config.py:174-179`, `cron.py:262-274`) и `SECURITY.md §8.1` (строки 312-314) уже описывают 365 как «год аудита, типовая глубина расследования инцидента» — совпадает с принятым обоснованием. Рассинхрон **только** в черновике `Политика_v2.md` (5 лет) — правится текст Политики, не код.

---

## Файлы (абсолютные пути)

- Канон значения: `C:\Users\filat\Documents\aemr-bot\bot\aemr_bot\config.py:180-182` (`audit_log_retention_days = 365`, ge=30 le=3650) _(ранее :129-131)_
- Cron-задача: `C:\Users\filat\Documents\aemr-bot\bot\aemr_bot\services\cron.py:266-291` (`_job_audit_log_retention`, cutoff = now − 365 дней)
- Модель (что хранится): `C:\Users\filat\Documents\aemr-bot\bot\aemr_bot\db\models.py:260-268`
- Отдельный 5-летний ретеншн обращений (НЕ путать): `C:\Users\filat\Documents\aemr-bot\bot\aemr_bot\services\cron.py:565-591` (`_job_appeals_5y_retention`)
- **Править текст:** `C:\Users\filat\Documents\aemr-bot\docs\Политика_v2.md` — п.7.5 (стр.125), п.11.3 (стр.198), чек-лист (стр.234)
- Уже согласовано с 365 (трогать не надо): `C:\Users\filat\Documents\aemr-bot\docs\SECURITY.md:312-314`; `C:\Users\filat\Documents\aemr-bot\bot\aemr_bot\services\cron_registry.py:95`
- Открытые вопросы (закрыть после этого решения): `C:\Users\filat\Documents\aemr-bot\docs\site\_kb\_QUESTIONS.md:47,77,213`

## Источники (правовые нормы)

- [152-ФЗ, ст.5 (принципы; ч.7 — ограничение срока хранения) — КонсультантПлюс](https://www.consultant.ru/document/cons_doc_LAW_61801/96fbc469f91f57235cc842a85e0516a99f23dc85/)
- [152-ФЗ (полный текст, последняя редакция) — КонсультантПлюс](https://www.consultant.ru/document/cons_doc_LAW_61801/)
- [Приказ ФСТЭК России №21, мера РСБ.1 (определение событий и сроков их хранения)](https://fstec21.blogspot.com/2017/07/the-definition-of-security-events-to-be.html)
- [Приказ ФСТЭК России №21 (ред. от 14.05.2020) — КонсультантПлюс](https://www.consultant.ru/document/cons_doc_LAW_146520/)
- [Регистрация событий безопасности в отношении ПДн — срок устанавливает оператор (radium-it)](https://www.radium-it.ru/brief/pd/security-events/)
- [Постановление Правительства РФ №1119 (уровни защищённости; сроков хранения журналов не задаёт) — КонсультантПлюс](https://www.consultant.ru/document/cons_doc_LAW_137356/)

---

## СПЕЦИФИКАЦИЯ — telegram_analytics + аналитическая панель

_Спецификация переиспользования наработок telegram_analytics в боте. Сверено по обеим кодовым базам: схема БД, путь /stats XLSX, разрыв в расчёте SLA (бот считает по календарю, у аналитики есть `working_hours.py`), мост таксономии тем, детектор эскалации, матрицы тона/качества, шаблоны._

---

# СПЕЦИФИКАЦИЯ: переиспользование наработок `telegram_analytics` в `aemr-bot` + аналитическая панель `/stats analytics`

## Часть 0. Что есть на двух сторонах (источник истины — код, с file:line)

**`aemr-bot` (бот, прод).** Схема БД (`bot/aemr_bot/db/models.py`):
- `Appeal` (`models.py:142`): `status` (`new/in_progress/answered/closed`), `locality`, `topic`, `summary`, `created_at` (idx), `answered_at`, `closed_at`, `closed_due_to_revoke`, `assigned_operator_id`. Индекс `ix_appeals_status` + `ix_appeals_created`.
- `Message` (`models.py:180`): `direction` (`from_user/from_operator/system`), `text`, `created_at`, `operator_id`, композит-индекс `ix_messages_appeal_created`.
- `User` (`models.py:61`): `consent_pdn_at`, `consent_revoked_at`, `consent_broadcast_at`, `subscribed_broadcast`, `is_blocked`, `dialog_state`, partial-индексы под подписчиков и «застрявших в воронке».
- `Broadcast`/`BroadcastDelivery` (`models.py:253`,`287`): `delivered_count`, `failed_count`, `status`; `BroadcastDelivery.error IS NULL` = доставлено.
- `AuditLog` (`models.py:220`), `BroadcastTemplate` (`models.py:337`) с `use_count`/`last_used_at`.

Существующая аналитика — только выгрузка XLSX (`services/stats.py`, `handlers/admin_stats.py`). SLA там считается **по календарному времени** (`stats.py:136-139`: `answered_at - created_at <= sla_response_hours*3600`), без учёта рабочих часов и праздников. Темы обращения — свободный текст из runtime-настройки `settings_store["topics"]` (`settings_store.py:358,441`; список ≤30 строк, редактируется оператором), привязки «тема→ведомство» нет.

**`telegram_analytics` (офлайн-аналитика 4304 обращений, Камчатка).** Чистый Python, CPU-only, без БД. Ключевые переиспользуемые активы:
- `working_hours.py` — корректный расчёт **рабочих часов** между двумя моментами (Пн–Пт, 9–18, праздники РФ), `working_hours_between()` (`working_hours.py:78`). Это ровно то, чего не хватает боту для честного SLA по Постановлению 2129.
- `escalation_prevention.py` — детектор риска эскалации GREEN/YELLOW/ORANGE/RED по 9 категориям триггер-слов (60+ фраз) + поведенческие модификаторы + протоколы реагирования (`escalation_prevention.py:22,201,293`). Pure-Python, без зависимостей.
- `agent_export/tone_matrix.json` — 7 эмоций → тон + первая фраза + «чего избегать».
- `agent_export/quality_checklist.json` — чек-лист Стандарта качества (что ДОЛЖЕН/НЕ должен содержать ответ, SLA).
- `incident_model.py`, `situation_graph.py` — агрегаты (инцидент = склейка тредов; ситуация = тема×тип с 12 критериями). Это **офлайн-аналитика**, не runtime.
- `routing_system.py` (`ORGANIZATIONS` `:1093`, `RoutingRule` SLA-таблица, маршруты A–H) — справочник ведомств Камчатки + дифференцированные SLA.
- `agent_export/knowledge_base.json` — `contacts_by_topic` (22 темы), `faq` (30), `routing_rules` (8).
- `agent_export/operator_templates.json` — 33 реальных ответа + рейтинг 46 шаблон-ID по частоте (`zhkh_no_water` 1746, `zhkh_no_heating` 828…).

**Главное противоречие (код побеждает).** В `system_prompt.json` и tone_matrix фигурирует «РАЙОН» / список приоритетных тем (вода/газ/отопление). Это противоречит решениям владельца и Постановлению 2129: территория формально «округ» (АЕМО), приоритет назначается Куратором ЦУР вручную, а не по теме. Поэтому при переносе **приоритетных тем из суггест-рейтинга нельзя превращать в авто-SLA-приоритет** — рейтинг используем только как «частотность для подсказки шаблона», не как «высокая важность».

---

## Часть 1. Реестр наработок и вердикт по переиспользованию

| Наработка | Файл-источник | Куда в боте | Вердикт |
|---|---|---|---|
| Расчёт рабочих часов (SLA) | `working_hours.py:78` | новый `services/work_time.py` | **MLP сейчас** — чистая функция, 0 инфраструктуры |
| Чек-лист качества ответа | `quality_checklist.json` | `services/answer_lint.py` + warning оператору | **MLP сейчас** |
| Матрица тона | `tone_matrix.json` | `seed/tone_matrix.json` → подсказка в карточке | **MLP сейчас** (подсказка, не автотекст) |
| Детектор эскалации | `escalation_prevention.py` | `services/escalation.py` | **MLP сейчас** (как флаг в admin-карточке) |
| Быстрые шаблоны ответов | `operator_templates.json` + рейтинг | `broadcast_templates`-подобная таблица `reply_templates` | **Фаза 2** (новая таблица) |
| Справочник ведомств + SLA A–H | `routing_system.ORGANIZATIONS`, `knowledge_base.contacts_by_topic` | `seed/org_directory.json`, settings | **Фаза 2** |
| Граф ситуаций (тема×тип, 12 критериев) | `situation_graph.py` | офлайн-отчёт раз в квартал | **На будущее** (нет `appeal_type` в боте) |
| Инцидентная модель (склейка) | `incident_model.py` | офлайн / будущая projection | **На будущее** |
| NLP-классификатор темы/эмоции | `classifiers.py`, `nlp_engine.py`, `keyword_dicts.py` | автоподсказка темы в воронке | **На будущее** (natasha/transformers — тяжёлая зависимость, против «CPU self-host без облака») |

Принцип отбора (бритва Оккама из их же `WORK_SYSTEM.md`): берём то, что встраивается **без новой инфраструктуры и тяжёлых зависимостей**. natasha/CEDR/rubert в прод-бота не тащим — это нарушит deployment-модель (self-host, long polling, без облака).

---

## Часть 2. Поэтапная встройка (MLP-совместимое — сейчас)

### Этап A. `services/work_time.py` — честный SLA по рабочим часам (объём: S, ~3–4 ч)

Перенести из `working_hours.py` функции `is_working_day`, `working_hours_between`, переписав под бота:
- Источник праздников — уже существующий `services/calendar_ru.py` (`calendar_ru.py:65` `is_holiday`, читает `seed/holidays.json`). Не дублировать `RU_HOLIDAYS_FIXED` — переиспользовать.
- Рабочие часы — из настроек (новые `WORK_START_HOUR=9`, `WORK_END_HOUR=18` в `config.py`, рядом с `sla_response_hours` `config.py:38`).
- TZ — уже есть `settings.timezone` (`Asia/Kamchatka`).

Сигнатура: `working_hours_between(start: datetime, end: datetime) -> float`. Внимание к расхождению семантики выходного: у бота `is_workday` это **Пн–Сб** (`calendar_ru.py:70`, суббота рабочая для оператора), а у аналитики Пн–Пт. Для SLA по Постановлению 2129 («рабочие часы») брать **Пн–Пт 9–18** — это отдельная функция `is_sla_working_day`, не путать с операторской субботой.

Зачем сейчас: `services/stats.py:136` и весь будущий `/stats analytics` считают «в SLA / просрочка». Без рабочих часов обращение, поданное в пятницу 17:50 и отвеченное в понедельник 9:10, ложно «просрочено на 63 часа». С `work_time` — 1.3 рабочих часа, в SLA. Это меняет цифры отчётности в разы и снимает несправедливые претензии к операторам.

Тесты: property-тесты (Hypothesis уже в проекте, см. задачи #129) — `working_hours_between(x,x)==0`, монотонность, пятница-вечер→понедельник-утро.

### Этап B. `services/answer_lint.py` — лёгкий линтер ответа оператора (объём: S, ~3 ч)

Из `quality_checklist.json` (`response_must_not_contain`) + антипаттернов `escalation_prevention.OPERATOR_ANTIPATTERNS_ESCALATION` (`escalation_prevention.py:441`) собрать набор regex-проверок Стандарта качества (Прил.2 Постановления 2129):
- канцелярит: «настоящим информируем», «доводим до вашего сведения», «в рамках действующего», «уведомляем вас»;
- страдательный залог-маркеры: «было направлено», «было передано»;
- обещания без срока: «в ближайшее время», «в кратчайшие сроки», «по мере возможности»;
- многоточие; аббревиатуры без расшифровки (эвристика — заглавные 2–5 букв);
- лимит символов: Telegram ≤1024 (реком. 800) — у бота `answer_max_chars` сейчас **800** (`config.py:55`), на уровне рекомендации Стандарта, ок; но линтер должен предупреждать о канцелярите, не о длине.

Где встроить: в `handlers/operator_reply.py::_deliver_operator_reply` (`operator_reply.py:501`) — **до** отправки жителю прогнать текст, и если есть нарушения — добавить оператору в admin-чат **некритичный warning** («Ответ отправлен. Замечания по Стандарту качества: канцелярит „доводим до сведения“; обещание без срока „в ближайшее время“»). Не блокировать отправку (Стандарт — рекомендация, оператор решает). Это аналог уже существующего URL-warning в `card_format._maybe_url_warning` (`card_format.py:262`) — тот же паттерн «совет, не блок».

### Этап C. `seed/tone_matrix.json` + подсказка тона (объём: S, ~2 ч)

Скопировать `tone_matrix.json` в `seed/` бота (как `seed/holidays.json`). Очистить от спорного: убрать привязку к «району». При показе admin-карточки нового обращения (`services/card_format.py::admin_card` `:180`) добавить **строку-подсказку** на основе грубого keyword-детектора эмоции по `summary` (взять `EMOTION_KEYWORDS` из `keyword_dicts.py` — это plain dict, без NLP): «Тон ответа: при гневе — спокойно, без оправданий; начните с „Понимаем ваше возмущение“». Подсказка — текст в карточке, **никакого автогенерируемого ответа** (владелец: ПДн и ответы — ручные).

### Этап D. `services/escalation.py` — флаг риска эскалации в карточке (объём: M, ~5–6 ч)

Перенести `escalation_prevention.py` целиком (он самодостаточен, без внешних зависимостей):
- `detect_escalation_triggers` + `calculate_risk_score` + `determine_risk_level` (`escalation_prevention.py:155,175,201`).
- Поведенческие сигналы адаптировать под схему бота: вместо их `analyze_citizen_history` использовать данные из БД — `message_count_today` (COUNT messages этого user за сегодня), `days_without_response` (now − last from_user message без from_operator после), `is_repeated_topic` (есть ли ещё appeal с тем же `topic` у этого `user_id`). Всё это — простые SELECT'ы по существующим таблицам.

Где встроить: в `admin_card` (`card_format.py:180`) — строкой вверху карточки при ORANGE/RED: «🟠 Риск эскалации: ORANGE (score 6.0). Триггеры: повторное обращение, обвинение в бездействии. Рекомендация: извинение + конкретный срок, ответ ≤2 ч». При RED — дополнительно подсветить «передать Куратору ЦУР» (это и есть «высокая важность» из Постановления, но назначает её человек — бот лишь подсказывает).

Важно для комплаенса: это **подсказка оператору**, не автоэскалация и не автоприоритет. «Высокая важность» по Постановлению 2129 назначается Куратором ЦУР вручную — детектор лишь обращает внимание. Так и документировать.

Тесты: характеризационные на 12 примерах (их же методика «12 персон», `WORK_SYSTEM.md:14`) — «напишу губернатору» → RED; «спасибо» → GREEN; «опять нет воды, третий раз пишу» → ORANGE.

---

## Часть 3. Фаза 2 (новая таблица/seed, без новой инфраструктуры)

### Этап E. Пул быстрых шаблонов ответов `reply_templates` (объём: M, ~1 день)

Зеркалит уже существующий `BroadcastTemplate` (`models.py:337`) — тот же паттерн: таблица + soft-delete + `use_count`/`last_used_at`. 
- Миграция `0018_reply_templates`: `id, name, topic, text, use_count, last_used_at, archived_at, created_by_operator_id`.
- Сервис `services/reply_templates.py` — копия структуры `services/broadcast_templates.py` (CRUD, `_validate_name/_validate_text`, archive).
- Seed из `operator_templates.json`: 33 готовых текста, привязать к темам. Рейтинг `suggested_templates_ranking` (`operator_templates.json:125`) использовать **только** для сортировки в списке (частые сверху), не для приоритета SLA.
- Интеграция: в admin-карточке кнопка «📝 Шаблоны» → список по теме обращения → вставка в поле ответа (механика reply-intent уже есть, `operator_reply.py:71` `remember_reply_intent`).

Это закрывает «быстрые шаблоны ответов» из ТЗ, переиспользуя готовую инфраструктуру шаблонов рассылок (DRY, минимум нового кода).

### Этап F. Справочник ведомств `seed/org_directory.json` (объём: S, ~3 ч)

Из `routing_system.ORGANIZATIONS` (`:1093`) + `knowledge_base.contacts_by_topic` собрать `seed/org_directory.json`: тема → {ведомство, телефон, что входит}. Подключить к `settings_store` рядом с `emergency_contacts`/`transport_dispatcher_contacts` (`settings_store.py:356-357`) — та же валидация item_keys `{name, phone}`. Использовать: в карточке-подсказке «по теме „Электроснабжение“ профильное ведомство — КамчатскЭнерго, аварийная 8(4152)29-70-80» и для шаблона `redirect_*` («не наша компетенция» — возврат ≤30 мин по Постановлению).

SLA-таблица A–H из `RoutingRule` (`routing_system.py`) — на будущее: дифференцированный SLA по типу маршрута. Сейчас бот использует единый `sla_response_hours=4`; дифференциацию вводить только когда появится `appeal_type`/маршрутизация (см. Часть 4).

---

## Часть 4. На будущее (требует новых сущностей — не MLP)

1. **`appeal_type` и `route` на обращении.** Граф ситуаций (`situation_graph.py`) и инцидентная модель (`incident_model.py`) опираются на `appeal_type` (жалоба/вопрос/просьба/благодарность) и маршрут A–H, которых в схеме бота **нет**. Чтобы их оживить — добавить `Appeal.appeal_type` (nullable, классификатор `routing_system.APPEAL_TYPES_L1` `:24` — он keyword-based, без NLP, можно посчитать на лету) и `Appeal.route`. После этого ситуационный граф строится офлайн-скриптом раз в квартал по выгрузке.
2. **Автоподсказка темы в воронке.** `classifiers.py`/`keyword_dicts.py` могут предлагать тему по тексту `summary` ДО ручного выбора. Но полноценный `classifiers.py` тянет natasha+transformers — против deployment-модели. Компромисс: перенести только keyword-часть (`_score_keywords` + guard-regex `classifiers.py:23`) без ML-fallback. Объём — большой, ценность спорная (житель и так выбирает тему кнопкой).
3. **Офлайн-отчёт «ситуации, требующие внимания».** Раз в квартал гонять `situation_graph.build_situation_graph` по экспорту БД → DOCX для руководителя (их `docx_writer.py` переиспользуем). Это отдельный аналитический контур, не runtime бота.
4. **Resolution Rate / Wilson CI** (их `ROADMAP_v3` + `SELF_IMPROVEMENT.md` очередь гипотез) — честная оценка операторов с малым N. Встраивается в `/stats analytics` Фазы 2.

Инцидентная склейка (`incident_model._merge_threads_to_incidents` `:13`, окно 48 ч, тот же гражданин+тема) **частично выражается на SQL уже сейчас** — см. воронку в Части 5.

---

## Часть 5. Аналитическая панель `/stats analytics` — спецификация (чистый SQL по существующим таблицам)

Отдельный подраздел меню `📊 Статистика` (рядом с XLSX-выгрузкой, `handlers/admin_stats.py:101`). Новый `services/analytics.py` с async-функциями, каждая — один-два `text()`/`select()` запроса; рендер компактным текстом в admin-группу (как `_send_stats_xlsx`, но текстом, без файла). Все запросы параметризованы окном периода из существующего `stats.period_window` (`stats.py:29`). Объём всей панели — **L, ~2–3 дня**, разбивается на независимые блоки ниже.

Принцип: ничего не считаем в Python, что можно посчитать в Postgres. SLA-в-рабочих-часах — единственное, что нельзя выразить чистым SQL без функции календаря, поэтому для него два пути (см. блок 3).

### Блок 1. TAT (time-to-answer) — медиана, p90, среднее (объём: S)

По календарному времени — чистый SQL:
```sql
-- :start, :end — границы окна периода (UTC)
SELECT
  count(*) FILTER (WHERE answered_at IS NOT NULL)                              AS answered,
  count(*)                                                                     AS total,
  round(avg(EXTRACT(EPOCH FROM (answered_at - created_at))/3600)
        FILTER (WHERE answered_at IS NOT NULL)::numeric, 2)                    AS avg_hours,
  round((percentile_cont(0.5) WITHIN GROUP (
          ORDER BY EXTRACT(EPOCH FROM (answered_at - created_at))/3600)
        FILTER (WHERE answered_at IS NOT NULL))::numeric, 2)                   AS median_hours,
  round((percentile_cont(0.9) WITHIN GROUP (
          ORDER BY EXTRACT(EPOCH FROM (answered_at - created_at))/3600)
        FILTER (WHERE answered_at IS NOT NULL))::numeric, 2)                   AS p90_hours
FROM appeals
WHERE created_at >= :start AND created_at < :end;
```
Вывод: «Отвечено 312 из 358 (87%). TAT: медиана 3.2 ч, среднее 5.1 ч, p90 14 ч (календарных)». `percentile_cont` — нативный Postgres, индекс `ix_appeals_created` покрывает фильтр.

### Блок 2. SLA-просрочка (объём: S по календарю / M по рабочим часам)

Календарная версия (мгновенно, чистый SQL):
```sql
SELECT
  count(*) FILTER (WHERE answered_at IS NOT NULL
                    AND EXTRACT(EPOCH FROM (answered_at - created_at)) <= :sla_sec) AS in_sla,
  count(*) FILTER (WHERE answered_at IS NOT NULL)                                   AS answered,
  count(*) FILTER (WHERE answered_at IS NULL
                    AND status IN ('new','in_progress')
                    AND EXTRACT(EPOCH FROM (now() - created_at)) > :sla_sec)        AS overdue_open
FROM appeals
WHERE created_at >= :start AND created_at < :end;
```
`:sla_sec = settings.sla_response_hours*3600`. Это ровно логика `find_overdue_unanswered` (`appeals.py:431`) и `stats.py:139`, обобщённая на агрегат. Светофор по порогам из их `SIGNAL_SYSTEM_RU.md` (GREEN ≥85%, YELLOW 70–84%, ORANGE 50–69%, RED <50%).

**Рабочие-часы версия (точная, по Постановлению 2129):** чистым SQL не выразить (праздники + 9–18). Решение: после Этапа A тянуть кандидатов одним SQL (`created_at`, `answered_at`), считать `working_hours_between` в Python (это ≤N строк за период, не весь архив — дёшево). Это гибрид «SQL для выборки + work_time для SLA», тот же приём, что в их `compliance.compute_compliance` (`compliance.py:48`).

### Блок 3. Пиковые часы и дни недели (heatmap-данные) (объём: S, чистый SQL)
```sql
SELECT
  EXTRACT(ISODOW FROM created_at AT TIME ZONE 'Asia/Kamchatka') AS dow,  -- 1=Пн..7=Вс
  EXTRACT(HOUR  FROM created_at AT TIME ZONE 'Asia/Kamchatka') AS hour,
  count(*) AS n
FROM appeals
WHERE created_at >= :start AND created_at < :end
GROUP BY dow, hour
ORDER BY n DESC;
```
Вывод: «Пик обращений: Пн 10:00 (47), Вт 11:00 (41)…». `AT TIME ZONE` даёт камчатское локальное время — критично, иначе пик смещён на 12 ч. Свернуть в текст топ-5 часов + распределение по дням.

### Блок 4. Темы — топ и доля «Прочее» (объём: S, чистый SQL)
```sql
SELECT coalesce(topic,'(не указана)') AS topic, count(*) AS n,
       round(100.0*count(*)/sum(count(*)) OVER (), 1) AS pct
FROM appeals
WHERE created_at >= :start AND created_at < :end
GROUP BY topic ORDER BY n DESC;
```
Светофор «Прочее» из `SIGNAL_SYSTEM_RU.md` (RED >40%). Поскольку `topic` — свободный текст из `settings_store["topics"]`, дополнительно показать «тем вне справочника» (значения, которых нет в текущем списке настройки) — индикатор расхождения.

### Блок 5. Воронка обращения и инцидентная склейка (объём: M)

5а. Статус-воронка (чистый SQL):
```sql
SELECT status, count(*) AS n
FROM appeals
WHERE created_at >= :start AND created_at < :end
GROUP BY status;
```
→ «Новые 12 · В работе 23 · Завершено 298 · Закрыто без ответа 25». Доля closed-без-ответа = недоработка.

5б. Воронка диалога (где жители «отваливаются» в funnel) — по `users.dialog_state` (partial-индекс `ix_users_stuck_in_funnel` уже есть, `models.py:84`):
```sql
SELECT dialog_state, count(*) AS stuck
FROM users
WHERE is_blocked = false AND dialog_state <> 'idle'
  AND updated_at < now() - interval '1 hour'
GROUP BY dialog_state ORDER BY stuck DESC;
```
→ «Застряли: awaiting_summary 8, awaiting_contact 5» — видно, на каком шаге воронки теряются жители.

5в. Повторные обращения / инцидентная склейка (выражает `incident_model` на SQL, окно 48 ч):
```sql
WITH ordered AS (
  SELECT user_id, topic, created_at,
         lag(created_at) OVER (PARTITION BY user_id, topic ORDER BY created_at) AS prev
  FROM appeals
  WHERE created_at >= :start AND created_at < :end
)
SELECT
  count(*) FILTER (WHERE prev IS NOT NULL
                    AND created_at - prev <= interval '48 hours') AS repeat_within_48h,
  count(*) AS total
FROM ordered;
```
→ «Повторных по той же теме в 48 ч: 14 из 358 (3.9%)». Порог из `SIGNAL_SYSTEM_RU.md` (RED >5%). Это инцидентная склейка `incident_model._merge_threads_to_incidents` (`:42`), но средствами оконных функций Postgres — без переноса модуля.

### Блок 6. Доставляемость рассылок (объём: S, чистый SQL)

По `broadcasts` + `broadcast_deliveries` (готовая логика `count_delivery_results` `broadcasts.py:293`, обобщённая на период):
```sql
SELECT b.id, b.created_at, b.subscriber_count_at_start,
       count(d.id) FILTER (WHERE d.error IS NULL)     AS delivered,
       count(d.id) FILTER (WHERE d.error IS NOT NULL) AS failed,
       round(100.0*count(d.id) FILTER (WHERE d.error IS NULL)
             / nullif(count(d.id),0), 1)              AS delivery_rate
FROM broadcasts b
LEFT JOIN broadcast_deliveries d ON d.broadcast_id = b.id
WHERE b.created_at >= :start AND b.created_at < :end
GROUP BY b.id ORDER BY b.created_at DESC;
```
→ «Рассылка #14 (28.05): 312/340 доставлено (92%), 28 ошибок». Топ причин ошибок — `GROUP BY d.error`. Плюс агрегат подписчиков: всего подписано / отписалось за период (по `consent_broadcast_at` и факту `subscribed_broadcast=false` с аудит-логом отписки).

### Блок 7 (опц., Фаза 2). Нагрузка и качество по операторам

По `messages.operator_id` (есть FK `messages.operator_id` `models.py:199`):
```sql
SELECT o.full_name,
       count(*) FILTER (WHERE m.direction='from_operator') AS replies,
       count(DISTINCT m.appeal_id)                         AS appeals_touched
FROM messages m JOIN operators o ON o.id = m.operator_id
WHERE m.created_at >= :start AND m.created_at < :end
GROUP BY o.full_name ORDER BY replies DESC;
```
Качество (нарушения Стандарта по `answer_lint`) и SLA-по-оператору — после Этапов A,B, считаются в Python по выборке. Wilson CI для малого N — на будущее (их `ROADMAP_v3`).

### Реализация панели — порядок и оценка
1. `services/analytics.py` + блоки 1,3,4,5а,6 (чистый SQL) — **первый PR, ~1 день**. Сразу полезно, нулевой риск.
2. Этап A (`work_time`) → точный SLA в блоке 2 + блок 7-качество — **второй PR, ~1 день**.
3. Блок 5в (склейка), 5б (воронка диалога), оператор-разрез — **третий PR, ~0.5 дня**.
Рендер — текст в admin-группу с порогами-светофорами из `SIGNAL_SYSTEM_RU.md`. Защита от OOM как в `stats._XLSX_ROW_CAP` (`stats.py:26`) тут не нужна — агрегаты возвращают десятки строк, не весь архив.

---

## Часть 6. Что НЕ делать (явные анти-рекомендации)

- Не тащить natasha/CEDR/rubert/transformers в прод-бота — нарушает self-host/CPU/без-облака модель. Только keyword-части классификаторов, если вообще.
- Не превращать `suggested_templates_ranking` (частоты тем) в авто-SLA-приоритет: «высокая важность» назначает Куратор ЦУР вручную (Постановление 2129 §1.2). Рейтинг — только сортировка шаблонов.
- Не генерировать ответы жителю автоматически из tone_matrix/templates — ПДн и финальные ответы ручные (решение владельца). Всё новое — **подсказки оператору**, не автотекст.
- Не переносить «РАЙОН»/«район» из `system_prompt.json` как канон — каноничное «округ» (АЕМО); «район» остаётся только в намеренном seed `appointment_text`.
- Не дублировать праздники: `work_time` берёт их из существующего `services/calendar_ru.py` + `seed/holidays.json`, не из `config.RU_HOLIDAYS_FIXED` аналитики.
- 59-ФЗ не упоминать в SLA-логике обращений из чат-бота — они вне 59-ФЗ, только 152-ФЗ (Постановление 2129 §1.2).

---

## Ключевые файлы (абсолютные пути)

Источник наработок: `C:\Users\filat\telegram_analytics\working_hours.py`, `escalation_prevention.py`, `incident_model.py`, `situation_graph.py`, `compliance.py`, `routing_system.py` (`ORGANIZATIONS` ~стр.1093), `classifiers.py`, `keyword_dicts.py`; экспорт `C:\Users\filat\telegram_analytics\agent_export\{tone_matrix,quality_checklist,operator_templates,knowledge_base,situation_graph,system_prompt}.json`; докс `C:\Users\filat\telegram_analytics\docs\{SIGNAL_SYSTEM_RU,PRODUCT_MODEL,WORK_SYSTEM,SELF_IMPROVEMENT}.md`.

Точки встройки в боте: `C:\Users\filat\Documents\aemr-bot\bot\aemr_bot\db\models.py` (схема), `services\stats.py` (period_window, XLSX), `services\appeals.py` (find_overdue ~стр.431), `services\broadcasts.py` (count_delivery_results ~стр.293), `services\calendar_ru.py` (праздники), `services\card_format.py` (admin_card ~стр.180 — куда вешать флаги/подсказки), `handlers\operator_reply.py` (`_deliver_operator_reply` ~стр.501 — куда вешать линтер), `handlers\admin_stats.py` (меню статистики ~стр.101), `services\settings_store.py` (SCHEMA, topics ~стр.358), `config.py` (sla_response_hours ~стр.38).

Новые модули к созданию: `services\work_time.py`, `services\answer_lint.py`, `services\escalation.py`, `services\analytics.py`, `services\reply_templates.py`; миграции `0018_reply_templates`; сиды `seed\tone_matrix.json`, `seed\org_directory.json`.

---
