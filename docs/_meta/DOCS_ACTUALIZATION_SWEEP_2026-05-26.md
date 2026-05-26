# Docs actualization sweep — 2026-05-26

Полный пасс по `README.md` корня + `docs/**`. Сверено с HEAD `main` (cron.py jobs, SECURITY §10a/§10b, OPERATOR_SECURITY новые подразделы, threat_intel.py, defang в карточках).

Главные обнаружения:
1. **Stale pulse-имена** (`pulse-workhours` / `pulse-offhours` / `pulse-sunday`) — устарели после рефакторинга в `cron.py` на `pulse-hourly` + `pulse-workhours-extra`. Засветились в 6 файлах.
2. **Новые cron-job'ы** (`broadcast-draft-reaper`, `threat-intel-refresh`, `stale-operators-cleanup`, `audit-log-retention`) — НЕ упомянуты в `RUNBOOK.md` / `HOW_IT_WORKS.md` / `COMPLIANCE_WITH_REGLAMENT_v7.md` / `_meta/AUDIT_REPORT.md` (SYSADMIN.md тоже не полон).
3. **OPERATOR_SECURITY.md** существует, но НЕ упомянут в `README.md` корня и в `docs/README.md`.
4. **SECURITY_REVIEW_2026-05-26 / SEC_SELF_REVIEW** — упомянуты только в SECURITY.md и v8-draft; остальные документы их не видят.
5. **OPERATOR_SECURITY.md §1.5 vs §1.5a**: §1.5a встроен между §1.3 и §1.5 (нумерация перепутана) — fix order.

---

## 📁 README.md (корень) — 🟡 минор — P0

**Что устарело.**
- §«Главное меню жителя» / «Состав репозитория» — не упоминает OPERATOR_SECURITY.md и SECURITY.md.
- §«Что читать дальше» — нет ссылки на `docs/OPERATOR_SECURITY.md` для операторов.
- Раздел «С чего начать на боевом сервере» — пункт 4 ссылается только на SETUP.md; нет про обязательное прочтение OPERATOR_SECURITY.md до первой смены.

**Правки.**
- Строка 56 (после RUNBOOK.md): добавить пункт `[OPERATOR_SECURITY.md](docs/OPERATOR_SECURITY.md) — инструкция оператору по ИБ, антифишингу, ответственности по 152-ФЗ (читать до первой смены).`
- Строка 57 (после DEVELOPER.md): добавить `[SECURITY.md](docs/SECURITY.md) — модель угроз, защитные механизмы (SEC #1–9, SECURITY_REVIEW 2026-05-26), ротация секретов.`
- Строка 61 (блок точечных регламентов): добавить ссылку на `SYSADMIN.md` (handover админа). Сейчас её нет.
- §«Состав репозитория» (строки 67–98): дописать `OPERATOR_SECURITY.md`, `SECURITY.md`, `SYSADMIN.md`, `MAXAPI_UPGRADE_PROCEDURE.md`, `DEPS.md` — в текущем дереве их нет.
- §«С чего начать на боевом сервере» (строка 151): пункт 4 финал — `Прочитать с операторами docs/OPERATOR_SECURITY.md (антифишинг, 152-ФЗ).`

---

## 📁 docs/README.md — 🟡 минор — P0

**Что устарело.** Не упоминает OPERATOR_SECURITY.md и SECURITY_REVIEW_2026-05-26. Раздел «Внутренние meta-документы» не перечисляет 8 новых файлов под `_meta/` (`SEC_*`, `URL_THREAT_INTEL_*`, `MAXAPI_DEEP_DIVE_*`, `SEC_SELF_REVIEW_*`).

**Правки.**
- В блок «Специалист по ИБ / аудитор» (строки 19–21): после `SECURITY.md` добавить `затем OPERATOR_SECURITY.md (operator-facing антифишинг).`
- В блок «Оператор и координатор» (строки 14–15): после `RUNBOOK.md` добавить `OPERATOR_SECURITY.md — обязательно к прочтению до первой смены, антифишинг и 152-ФЗ.`
- В §«Внутренние meta-документы» (строки 86–103): добавить новые файлы:
  - `_meta/SECURITY_REVIEW_2026-05-26.md` — сводный результат security-пасса.
  - `_meta/SEC_INVENTORY/SEC_MAX_THREATS/SEC_SCAM_VECTORS/SEC_EXPLOITS/SEC_SELF_REVIEW_2026-05-26.md` — детальные SEC-отчёты.
  - `_meta/URL_THREAT_INTEL_2026-05-26.md` — research под `services/threat_intel.py`.
  - `_meta/MAXAPI_DEEP_DIVE_2026-05-26.md` — обновлённая инвентаризация maxapi 1.1.0.

---

## 📁 docs/RUNBOOK.md — 🔴 устарел / битые имена — P0

**Критичные ошибки.** Раздел «Cron-задачи» (строки 642–657) содержит **stale pulse-имена** и пропущены 4 новых cron-job'а.

**Правки.**
- Строки 652–654: **удалить** три строки (`pulse-workhours` / `pulse-offhours` / `pulse-sunday`), **заменить на**:
  ```
  | `pulse-hourly` | каждый час 24/7 в :05 | технический heartbeat «бот жив» |
  | `pulse-workhours-extra` | пн–пт 09:00–17:59 в :35 | дополнительный heartbeat в рабочее время |
  ```
- После строки 657 (после `healthcheck-ping`): **добавить 4 новых row'а**:
  ```
  | `audit-log-retention` | ежедневно 04:15 | очистка audit_log старше AUDIT_LOG_RETENTION_DAYS (default 365) |
  | `stale-operators-cleanup` | ежедневно 04:20 | сверка активных операторов с членами админ-группы MAX, мягкая деактивация ушедших (IT защищены) |
  | `threat-intel-refresh` | ежечасно в :17 | обновление feed'ов URLhaus/ThreatFox для warning'а оператору о фишинг-ссылках |
  | `broadcast-draft-reaper` | ежечасно в :37 | подбор orphan DRAFT-рассылок старше 30 мин → FAILED |
  ```
- Раздел «Расписание бэкапа» (строка 661): упомянуть `cooldown` рассылки (5 мин / 30 сек для `[ЧС]`) — слово `cooldown` в RUNBOOK не встречается, но это hot-path для координатора. Добавить под раздел «Рассылка».

---

## 📁 docs/HOW_IT_WORKS.md — 🟡 минор — P0

**Правки.**
- Строки 297–298: заменить устаревшие `pulse-workhours` / `pulse-offhours` на `pulse-hourly` (24/7 в :05) и `pulse-workhours-extra` (пн–пт 09–17 в :35).
- После строки 301: добавить 4 новых cron-job'а (`audit-log-retention`, `stale-operators-cleanup`, `threat-intel-refresh`, `broadcast-draft-reaper`) одной строкой каждая.
- §16 (строка 306, «Сбои MAX и повторная доставка»): дописать `fail-closed idempotency (SEC #7) — при ошибке записи idempotency-ключа событие отбрасывается, не обрабатывается».

---

## 📁 docs/SYSADMIN.md — 🟡 минор — P0/P1

**Что есть.** §12b log rotation добавлен корректно. §7 cron-таблица существует, но не полна.

**Правки.**
- Строки 231–233 (таблица cron): заменить `pulse-workhours` / `pulse-offhours` / `pulse-sunday` на `pulse-hourly` (каждый час 24/7 в :05) + `pulse-workhours-extra` (пн–пт 09–17 в :35). См. cron.py:851,861.
- В ту же таблицу (после строки 243): добавить 4 row'а — `audit-log-retention` 04:15, `stale-operators-cleanup` 04:20 (cron.py:797), `threat-intel-refresh` ежечасно :17 (cron.py:787), `broadcast-draft-reaper` ежечасно :37 (cron.py:777).
- §16 «Что НЕ настроено» (строка 488): дополнить «Threat-intel refresh» — требует исходящего HTTPS к `urlhaus.abuse.ch` и `threatfox.abuse.ch`, проверить outbound whitelist.

---

## 📁 docs/SECURITY.md — 🟢 актуален — P0

Очень свежий, §10b accept'ы корректные, ссылки на `_meta/SECURITY_REVIEW_2026-05-26.md` и `_meta/SEC_*` стоят. Серия SACRED расширена до #6 (SECURITY.md:386), §10 CI gate подробный.

**Минор-правки.**
- §3.2 «Логи» (строка 76): дописать «Ротация настроена через docker compose logging-options 10MB×3 (см. SYSADMIN §12b).» — сейчас фраза «до этого нужно проверить» оставлена без ссылки на закрывающее изменение.
- §7 после §7.6 (строка 297): добавить новый подраздел `### 7.7 Threat-intel для входящих сообщений (SEC C2/M3)` — упоминание `services/threat_intel.py`, refresh каждый час, источник URLhaus + ThreatFox, fallback при недоступности feed'а. Сейчас threat_intel живёт в коде, в SECURITY.md ссылок нет.
- §7.3.4 (строка 282): добавить подзаголовок `### 7.3.5 URL defang в карточке оператора (SECURITY_REVIEW M1/M5)` — ZWSP-разделитель между `https` и `://`, warning-блок «⚠️ Текст содержит ссылку». Сейчас это есть в OPERATOR_SECURITY §1.5, но в SECURITY.md как защита не задокументировано.

---

## 📁 docs/OPERATOR_SECURITY.md — 🔴 минор-баг — P0

**Структурный bug.** §1.5a (строка 51) идёт **раньше** §1.4 (строка 78) и §1.5 (строка 64). Нумерация перепутана: документ читается «§1.1 → §1.2 → §1.3 → §1.5a → §1.5 → §1.4». Нужно либо переупорядочить, либо переименовать.

**Правки.**
- **Опция A (рекомендую):** Переименовать `### 1.5a` → `### 1.4 Визуальная структура карточки`, текущий `### 1.4 Что делать, если житель попался на скам` → `### 1.6`. То есть порядок 1.1 → 1.2 → 1.3 → 1.4 (визуал) → 1.5 (бдительность) → 1.6 (что делать).
- §3 «Hot-path процедуры» (строка 123): §3.1 broadcast — добавить упоминание `cooldown 5 минут / 30 секунд для [ЧС]` отсылкой к §1.5 и §1.1 (уже есть, но не явно про **технический параметр** `BROADCAST_COOLDOWN_SEC`). Сейчас оператор не знает, что cooldown настраивается.
- §5 «Куда смотреть дальше» (строка 186): добавить ссылку на `docs/SYSADMIN.md §12b` (ротация логов) — связанная гигиена sysadmin'а.
- §3.4 «Удаление ПДн жителя» (строка 156): уточнить, что docker logs тоже требуют ручного truncate (см. RUNBOOK_PDN_ERASURE «72-часовой dispatch»). Сейчас намёк есть, но без cross-link.

---

## 📁 docs/RUNBOOK_PDN_ERASURE.md — 🟡 минор — P0

**Что есть.** Актуальная модель удаления (`appeals.summary` → NULL, `messages.text` → NULL, физическое удаление `users`). Корректное упоминание трёх каналов (manual / self / auto).

**Что не хватает.**
- В тексте упомянут «72-часовой dispatch на /erase + truncate docker logs» (по контексту из claudeMd parent task), но физически такого раздела в файле нет. Нужно либо добавить §«72-часовой dispatch», либо убрать упоминание из других документов (OPERATOR_SECURITY §3.4 ссылается).
- **Правка:** после §«Резервные копии» (строка 88) добавить новый §«72-часовой dispatch для запросов жителя» с явным указанием: (1) запустить `/erase`, (2) залить руками `sudo truncate -s 0` для docker-логов бота за последние 7 дней (см. SYSADMIN §12b), (3) проверить, что свежий backup без ПДн через 7 дней (ротация 8 еженедельных).

---

## 📁 docs/Регламент_v8_draft.md — 🟢 актуален — P1

Очень свежий, §74 антифишинг полный, §60.1 SEC #1–9, §74.6 cooldown 5 мин / 30 сек для `[ЧС]`. Согласован с кодом.

**Минор-правки.**
- §47.1 «Шаблоны рассылок» (строка 67): дописать «Применение шаблона дедуплицируется в 3-секундное окно (SEC P3 #25) — повторный тап не вызывает побочных эффектов.»
- §74.5 «Защита оператора» (строка 188): дописать «Cron `stale-operators-cleanup` запускается ежедневно в 04:20 (Asia/Kamchatka), выполняется через `get_chat_members` MAX API.»
- §74.7 (строка 192): дописать в перечне документов `docs/_meta/SECURITY_REVIEW_2026-05-26.md — сводный отчёт security-пасса 2026-05-26.`

---

## 📁 docs/COMPLIANCE_WITH_REGLAMENT_v7.md — 🔴 устарел — P1

**Битые имена.** Таблица 3 (строки 214–216) содержит `pulse-workhours` / `pulse-offhours` / `pulse-sunday`. **Также таблица "13 задач" (строка 220) уже не верна** — после новых cron'ов их 17 (+ `healthcheck-ping`).

**Правки.**
- Строки 214–216: переписать на `pulse-hourly` (cron `minute=5`, каждый час 24/7) и `pulse-workhours-extra` (cron `day_of_week=mon-fri, hour=9-17, minute=35`). Удалить `pulse-sunday` (вс покрыт `pulse-hourly`).
- После строки 219 добавить новые row'ы 14–17:
  - 14: `audit-log-retention` ежедн. 04:15.
  - 15: `stale-operators-cleanup` ежедн. 04:20.
  - 16: `threat-intel-refresh` ежечасно :17.
  - 17: `broadcast-draft-reaper` ежечасно :37.
- Строка 220: «13 задач» → **«17 задач (плюс опциональный `healthcheck-ping`)»**.
- Документ названия `v7` — он сверяется со старым регламентом; после утверждения v8 нужно сделать `COMPLIANCE_WITH_REGLAMENT_v8.md` (см. v8-draft §229).

---

## 📁 docs/_meta/AUDIT_REPORT.md — 🔴 устарел — P2

**Битые имена.** Строки 158–160 (раздел 2.12 Pulse-расписание) содержат три stale pulse-имени. **Документ от 2026-05 без явной даты** (строка 3: «текущий снимок»).

**Правки.**
- Строки 158–160: переписать на новые имена pulse + проставить дату документа явно — `Дата ревизии: 2026-05-25 (актуализация cron — 2026-05-26)`.
- Раздел 2.13 «Cron-задачи (полный список)» (строка 166): «13 cron-задач» → «17 cron-задач».
- Помечать как **superseded by** новый sweep этого документа: добавить frontmatter `superseded_by: DOCS_ACTUALIZATION_SWEEP_2026-05-26.md` в части cron.

---

## 📁 docs/PRD.md — 🟡 минор — P1

**Правки.**
- Строка 243: stale pulse-имена в разделе «SLA-мониторинг и pulse». Заменить на `pulse-hourly` (24/7 в :05) и `pulse-workhours-extra` (пн–пт 09–17 в :35). Удалить упоминание `pulse-sunday`.

---

## 📁 docs/_meta/* (research) — оценка актуальности

| Файл | Статус | Действие |
|---|---|---|
| `SECURITY_REVIEW_2026-05-26.md` | 🟢 актуален | оставить, P0 |
| `SEC_INVENTORY/MAX_THREATS/SCAM_VECTORS/EXPLOITS_2026-05-26.md` | 🟢 актуален | оставить, P2 |
| `SEC_SELF_REVIEW_2026-05-26.md` | 🟢 актуален | оставить, P2 |
| `URL_THREAT_INTEL_2026-05-26.md` | 🟢 актуален | оставить, P2 — research под threat_intel.py |
| `MAXAPI_DEEP_DIVE_2026-05-26.md` | 🟢 актуален | оставить, P2 |
| `MAXAPI_INVENTORY.md` / `MAXAPI_INSIGHTS.md` / `MAXAPI_UNUSED_FEATURES.md` | 🟡 от 2026-05-25 | DEEP_DIVE их частично перекрыл; пометить `superseded_by: MAXAPI_DEEP_DIVE_2026-05-26.md` в frontmatter |
| `AUDIT_REPORT.md` | 🔴 устарел (см. выше) | superseded |
| `FILE_INVENTORY.md` | 🟡 от 2026-05-25 | проверить — не учитывает threat_intel.py, новые cron'ы. Пометить «частично устарел». |
| `REGLAMENT_v7_GAPS.md` / `REGLAMENT_v7_COMPLIANCE.md` | 🟡 от 2026-05-25 | актуальны как основа v8-draft; пометить «valid для v7, после утверждения v8 — переименовать». |
| `COVERAGE_GAPS.md` | 🟡 от 2026-05-25 | проверить — закрылось ли что-то после SEC-фиксов |
| `ADMIN_MENU_EXPANSION_PROPOSAL.md` | 🟡 от 2026-05-25 | proposal — оставить как design-doc, P2 |

---

## 📁 docs/_extracted/REGLAMENT_v7_FULL.md — 🟢 не трогаем

Нормативный документ, по требованию не правится.

---

## docs/*.html — отсутствуют

`ls docs/*.html` — пусто. Удалены ранее, в `docs/README.md` упоминаний нет.

---

# Итоговый actionable diff (минимально для одного PR)

**P0 (читает оператор / житель регулярно), 8 файлов:**

1. **`README.md` корня:**
   - В §«Что читать дальше» добавить ссылку на `OPERATOR_SECURITY.md` и `SECURITY.md`.
   - В §«Состав репозитория» (дерево) дописать `OPERATOR_SECURITY.md`, `SECURITY.md`, `SYSADMIN.md`.
   - В §«С чего начать на боевом сервере» пункт 4 — фразу про чтение `OPERATOR_SECURITY.md`.

2. **`docs/README.md`:**
   - В блок «Оператор и координатор» добавить `OPERATOR_SECURITY.md`.
   - В блок «Специалист по ИБ» добавить `OPERATOR_SECURITY.md` после `SECURITY.md`.
   - В §«Внутренние meta-документы» — 6 новых файлов (`SECURITY_REVIEW`, `SEC_*`, `URL_THREAT_INTEL`, `MAXAPI_DEEP_DIVE`).

3. **`docs/RUNBOOK.md` строки 652–657:**
   - Удалить `pulse-workhours` / `pulse-offhours` / `pulse-sunday`, добавить `pulse-hourly` + `pulse-workhours-extra` + 4 новых cron (`audit-log-retention`, `stale-operators-cleanup`, `threat-intel-refresh`, `broadcast-draft-reaper`).

4. **`docs/HOW_IT_WORKS.md` строки 297–301:**
   - Те же замены pulse + 4 новых cron.

5. **`docs/SYSADMIN.md` строки 231–233 и после 243:**
   - Те же замены pulse + 4 новых cron. §16 — упомянуть outbound для `urlhaus.abuse.ch` / `threatfox.abuse.ch`.

6. **`docs/SECURITY.md`:**
   - §3.2 закрыть фразу ссылкой на SYSADMIN §12b.
   - §7 добавить `§7.7 Threat-intel` и `§7.3.5 URL defang`.

7. **`docs/OPERATOR_SECURITY.md`:**
   - **Перенумеровать §1.5a → §1.4, §1.4 → §1.6** (исправить порядок).
   - §3.1 broadcast — упомянуть параметр `BROADCAST_COOLDOWN_SEC`.
   - §5 ссылка на SYSADMIN §12b.

8. **`docs/RUNBOOK_PDN_ERASURE.md`:**
   - Добавить §«72-часовой dispatch» с truncate docker-логов.

**P1 (читает IT / новички), 3 файла:**

9. **`docs/Регламент_v8_draft.md`:** §47.1 дедуп шаблона, §74.5 stale-operators cron, §74.7 ссылка на SECURITY_REVIEW.

10. **`docs/COMPLIANCE_WITH_REGLAMENT_v7.md` строки 214–220:** замены pulse + 4 новых row + `«17 задач»`.

11. **`docs/PRD.md` строка 243:** замены pulse.

**P2 (research / архив), пометки superseded:**

12. **`docs/_meta/AUDIT_REPORT.md`:** строки 158–160 (pulse) + frontmatter `superseded_by: DOCS_ACTUALIZATION_SWEEP_2026-05-26.md`.

13. **`docs/_meta/MAXAPI_INVENTORY|INSIGHTS|UNUSED_FEATURES.md`:** frontmatter `superseded_by: MAXAPI_DEEP_DIVE_2026-05-26.md`.

---

**Sanity-check после применения:** `grep -r "pulse-offhours\|pulse-workhours\|pulse-sunday" docs/ README.md` должен вернуть 0 (либо только в архивных _meta файлах с пометкой superseded). `grep -rc "broadcast-draft-reaper\|threat-intel-refresh\|stale-operators-cleanup" docs/` должен покрыть ≥6 рабочих документов (RUNBOOK, SYSADMIN, HOW_IT_WORKS, SECURITY, COMPLIANCE, AUDIT).
