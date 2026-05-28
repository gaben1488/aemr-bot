---
status: applied
applied_in_pr: 127, 134
applied_at: 2026-05-28
note: A1+A2 (cron quiet bypass) closed PR #127; A3+A4+A7 (URL defang IDN
  расширение + whitelist mixed-case reject + mask phone digits<4 → «—»)
  closed PR #134. Low/medium findings A5–A8 + MX-4 + SC-4/5/7/8 — backlog.
---

# Security Review Delta 2026-05-28

> Дельта-аудит после PR #102–#126 поверх baseline `SECURITY_REVIEW_2026-05-27.md`.
> Скоуп: новый код последних 30 часов + MAX-bot-specific attack surface +
> современные scam-векторы 2026 (AI voice clone, deepfake, импер­сонация
> гос-брендов). Ничего из закрытых SEC #1–#9, C1–C6, H1–H2, M1–M10 не
> повторяю.

---

## TL;DR

```
🔴 Critical:  1 (alerts через send_admin_text подавляются quiet режимом)
🟡 Medium:    3 (quiet-cache initial state, defang TLD missing punycode, broadcast URL whitelist case-sensitivity)
🟢 Low:       4 (PII в маске, dual-tracker cache evict, idempotency cb_id window, monkey-patch in tests)
✅ OK:        URL defang bare-domain (#118), quiet hours sync cache (#117),
              dual-tracker (#100), SCHEMA overflow guard (#103), text length guard (#101)
```

**Главная находка делты — A1 🔴:** `_build_admin_senders` оборачивает
все cron-алёрты в `admin_bus.send(...)` без `critical=True`. Значит
в quiet режиме (default 18:00–09:00) **тихо проглатываются alert'ы**:
- фейл еженедельного backup'а (`_job_backup_with_alert`, идёт сб 03:00 — попадает в окно);
- ошибки `_job_stale_operators_cleanup`;
- ошибки `_job_pdn_retention_check` (152-ФЗ retention — критично!);
- ошибки `_job_appeals_5y_retention`;
- alert'ы `_job_funnel_watchdog`.

Worst case: backup упал в субботу 03:00, fail-alert подавлен, оператор
узнаёт в понедельник 09:00 — окно ~30ч без свежего бэкапа.
152-ФЗ retention-cron упал → жители с активным «забудь меня»
остаются с PII в БД дольше положенного — формальное нарушение
§14(4) 152-ФЗ.

---

## Дельта vs 2026-05-27

| PR | Кратко | Новый attack surface | Делта-вердикт |
|---|---|---|---|
| #102 | README aiohttp/FastAPI sync, uvd-kam удалён | docs only | OK |
| #103 | SCHEMA overflow guard (welcome 4000→3800) + AI voice clone в SECURITY_INFO | settings input validation | OK |
| #104 | timeline unification (admin card) | render path | OK |
| #105 | stale «❌ Отмена» → no-op | UX only | OK |
| #106 | citizen keyboards consistency | UX only | OK |
| #107 | admin keyboards возврат ↩️ унификация | UX only | OK |
| #108 | citizen «Мои обращения» 📎→🗂 | UX only | OK |
| #109 | docstring cleanup → CODE_DECISIONS_LOG | docs only | OK |
| #110–#114, #119 | lazy → top-level imports (admin_callback_dispatch, admin_appeal_ops, services/cron, admin_panel, wizard_registry, menu.py) | импорт-граф изменён | OK (циркуляров нет) |
| #115 | C1 docs truth pass (pulse, cron jobs, OPERATOR_SECURITY numbering) | docs only | OK |
| #116 | «адрес проблемы» vs домашний адрес | text only | OK |
| #117 | quiet hours toggle через ⚙️ Настройки | новая feature: in-memory cache + sync read | 🟡 (A1 critical, A2 medium) |
| #118 | URL defang расширен на bare domain (ya.ru / bit.ly) | новые TLD-маски | 🟡 (A3 medium) |
| #120 | meta D1/D2 closed status + superseded_by | docs only | OK |
| #121 | quiet hours UI toggle | поверх #117 | см. A1 |
| #122 | docs local vs CI test commands | docs only | OK |
| #123 | cron-registry + docs anti-drift test | новый assert mechanism | OK |
| #124 | gitignore *.stackdump | hygiene | OK |
| #125 | quiet hours edit-flow start/end через UI | intent flow + TTL 5min | 🟢 (A6 low, TTL race) |
| #126 | tests cluster D consent invariants | tests only | OK |

Новый attack surface — только **quiet режим** (3 PR), **URL defang
bare-domain** (1 PR), **dual-tracker** (1 PR). Остальное — UX/docs/тесты.

---

## A1 🔴 CRITICAL — алёрты cron подавляются quiet режимом

**Файл:** `bot/aemr_bot/main.py:109–118` + `bot/aemr_bot/services/cron.py:152–660`.

**Что не так:** `send_admin_text` (factory `_build_admin_senders`)
вызывает `admin_bus.send(bot, text=text)` **без `critical=True`**.
По contract'у `admin_bus.send` (см. `services/admin_bus.py:42–64`) при
включённом quiet режиме (`is_quiet_hours_now() == True`) возвращает
`None` и **не отправляет** сообщение.

Все cron-алёрты идут через этот же `send_admin_text`:

```python
# main.py:109
async def send_admin_text(text: str):
    await admin_bus.send(bot, text=text)   # critical=False по умолчанию

# services/cron.py:171  (_job_backup_with_alert)
await send_admin_text("⚠️ Еженедельный бэкап БД упал на pg_dump…")

# services/cron.py:592  (_job_pdn_retention_check)
await send_admin_text("⚠️ PDN retention error: …")  # ← 152-ФЗ!
```

**Сценарий:**
1. Default quiet режим: 18:00–09:00 (`admin_quiet_hours_*` defaults).
2. Backup-cron: `0 3 * * 6` (сб 03:00, см. `cron_registry.py`).
3. pg_dump падает → `_job_backup_with_alert` → `send_admin_text(...)`
   → `admin_bus.send(..., critical=False)` → `is_quiet_hours_now()=True`
   → log INFO «suppressed» + return None.
4. Алёрт **не доходит** до admin-чата.
5. Оператор узнаёт о фейле в пн 09:00 утра (когда pulse прошёл и появится в чате) — окно ~30 часов.

**152-ФЗ impact:** `_job_pdn_retention_check` идёт `0 6 * * *` (06:00
ежедневно — попадает в quiet окно [18, 9)). Если retention-cron
упадёт (например, БД locked, миграция в процессе), alert подавлен →
жители, требующие обезличивания через 30 дней после revoke_consent,
остаются с PII в БД дольше → формальное нарушение §14(4) ФЗ-152.

**Fix (срочный, отдельный PR):**

```python
# main.py:_build_admin_senders
async def send_admin_text(text: str, *, critical: bool = False):
    if not settings.admin_group_id:
        return
    await admin_bus.send(bot, text=text, critical=critical)
```

Затем в cron-алёртах:
```python
# cron.py:_job_backup_with_alert, _job_stale_operators_cleanup,
# _job_pdn_retention_check, _job_funnel_watchdog
await send_admin_text("⚠️ …", critical=True)
```

Pulse и periodic selfcheck НЕ менять — они правильно non-critical.

**Verification:**
```bash
grep -n "send_admin_text(" bot/aemr_bot/services/cron.py | head -20
# Каждый вызов в alert-сайте должен иметь critical=True. Pulse — нет.
```

**Test регрессии:** в `test_quiet_hours.py` добавить case «backup
alert не подавляется в quiet режиме», assert mock `bot.send_message`
был вызван.

---

## A2 🟡 MEDIUM — quiet cache initial state ≠ DB state

**Файл:** `bot/aemr_bot/services/quiet_hours.py:54–58`.

```python
_cache: dict = {
    "enabled": False,   # default: не подавляем пока БД не прочитана
    "start": 18,
    "end": 9,
}
```

`is_quiet_hours_now()` возвращает False до первого
`refresh_cache_from_db()`. Это safe default (лучше шум чем потерять
алёрт), но создаёт **window инконсистентности**:

- Бот стартует в 22:00 (quiet окно в БД).
- `refresh_cache_from_db` вызывается из startup-pulse (через 5 сек) и
  затем из cron каждый час в `:05`.
- Первые 5 секунд пока cache не прогрелся — все non-critical сообщения
  **пойдут** в чат, хотя владелец явно настроил quiet.

**Impact:** низкий. 5-секундное окно, в нём максимум один pulse или
admin-event. Не критично, но создаёт inconsistency в UX («почему
вчера ночью pulse пришёл, а сегодня нет?»).

**Fix:** в `main.py` сразу после `_seed_settings()` вызвать
`await refresh_cache_from_db(session)` — до старта polling. Тогда
cache горячий с 0-й секунды.

---

## A3 🟡 MEDIUM — URL defang TLD list не покрывает punycode и редкие IDN

**Файл:** `bot/aemr_bot/utils/url_defang.py:60–72`.

```python
_DEFANG_TLDS = (
    "ru", "su", "рф", "by", "kz", ...,
    "com", "org", "net", ...,
    "io", "co", "me", ...,
    "xyz", "top", "club", ...,
)
```

**Что не покрыто:**
- **Punycode-домены**: `xn--80a1acny.xn--p1ai` (= `аэмо.рф` в punycode)
  — TLD `xn--p1ai` не в списке. MAX-клиент auto-linkify'ит punycode.
- **Редкие но реальные scam-TLD 2026**: `.cn`, `.tr`, `.in`, `.ng`,
  `.mx`, `.br` — не в списке. Скам-кампании из Африки/Азии работают
  через эти TLD.
- **Новые расширения 2024-2026**: `.bot`, `.gov` (как country-prefix —
  `.gov.kz`), `.tech`, `.cloud`, `.live`, `.work`.

**Impact:** медиум. Фишинг через `phish.cn` или `xn--evil.xn--p1ai`
проходит мимо defang → auto-linkify'ится у оператора в MAX-клиенте
→ один тап = phishing-страница с правами оператора.

**Fix:** добавить:
```python
_DEFANG_TLDS += (
    # IDN/punycode
    "xn--p1ai",    # .рф punycode
    "xn--p1acf",   # .рус punycode
    "xn--90a3ac",  # .срб punycode
    # Country (расширение)
    "cn", "tr", "in", "ng", "mx", "br", "id", "vn", "ph", "th",
    # 2024-2026 расширения
    "bot", "tech", "cloud", "live", "work", "social", "world",
)
```

Расширить test_url_defang.py соответствующими кейсами.

---

## A4 🟡 MEDIUM — broadcast URL whitelist case-sensitivity edge

**Файл:** `bot/aemr_bot/services/settings_store.py` (whitelist matcher).

Whitelist хранит pattern'ы вида `*.elizovomr.ru`, `*.gosuslugi.ru`.
Match строго lowercase. Если оператор вставит URL с mixed-case host
(`https://Gosuslugi.RU`), он будет normalize'ed через `urlparse`, но
some MAX-клиенты не нормализуют host перед auto-link. Если
broadcast-URL прошёл проверку → отправляется как есть → у жителя
кликабельная ссылка с unusual casing, что **легче спуфить**:
`https://Gosuslugi.RU.evil.example.com` — visually похоже на гос.

**Impact:** низкий-средний. Зависит от поведения MAX-клиента. Нужно
протестировать на mobile/desktop клиенте.

**Fix (защитный):** в matcher для URL whitelist сначала `host = host.lower()`,
плюс reject если в host есть символ кроме `[a-z0-9.-]` (защита от
unicode-омоглифов).

---

## A5 🟢 LOW — dual-tracker ChatState не эвиктится

**Файл:** `bot/aemr_bot/utils/menu_tracker.py`.

`_state: dict[int, ChatState]` — in-memory per chat_id, без TTL/LRU.
Для бота с 1 admin chat и ~5000 жителями (каждый — свой чат с ботом)
— ~5K entries по ~50 байт = 250 KB. Безопасно. Но если когда-нибудь
бот выйдет в публику (100K+ жителей) — мониторить.

**Fix:** не требуется для текущей нагрузки. В backlog: TTL eviction
для idle chats >30 дней.

---

## A6 🟢 LOW — quiet hours edit-intent TTL race

**Файл:** `bot/aemr_bot/handlers/admin_settings.py::_start_quiet_hour_intent`.

Intent flow с TTL 5 мин. Если оператор-1 начал edit (intent), а
оператор-2 параллельно меняет тот же ключ через другую сессию —
intent'ы overwrite друг друга. Последний wins.

**Impact:** низкий. Cosmetic; владелец = 5 операторов, race
маловероятен. Audit log зафиксирует обоих.

**Fix:** не требуется. Backlog: optimistic lock через
`updated_at IS NULL OR updated_at < initial_value`.

---

## A7 🟢 LOW — _mask_phone возвращает «—» для length<4

**Файл:** `bot/aemr_bot/services/admin_events.py:38–46`.

```python
def _mask_phone(phone: str | None) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 4:
        return phone   # ← возвращает оригинал!
```

Если phone короткий (`+71` test-input), маска вернёт полный текст,
включая `+`. Не критично т.к. реальный phone всегда ≥11 цифр. Но
guarantee «PII не светится в admin chat» нарушен на edge case.

**Fix (defensive):** `return "—"` если digits<4.

---

## A8 🟢 LOW — monkey-patch admin_bus hook в продовом коде

**Файл:** `bot/aemr_bot/services/admin_bus.py:152–189`.

`install_outgoing_tracker_hook` подменяет `bot.send_message`
динамически. Идемпотентный guard через `_aemr_admin_outgoing_tracker_installed`
есть. Но если **другой код** (например, тестовая фикстура с моком)
заменит `bot.send_message` ПОСЛЕ install — hook потерян → tracker
не двигается → freshness может ошибаться.

**Impact:** низкий. Это test-environment hazard, не prod. Тесты
уже учитывают (`test_admin_outgoing_hook.py`).

**Fix:** не требуется. Документировать в `CODE_DECISIONS_LOG §3`.

---

## MAX-bot specific attack vectors (2026 update)

Базируется на `docs/SECURITY.md §3` (MAX threat model) + новые
наблюдения 2026.

### MX-1 — Callback payload injection

**Статус:** ✅ закрыто (`callback_router.route_for(payload)` валидирует
строго через EXACT_ROUTES + PREFIX_ROUTES; unknown payload → no-op).

### MX-2 — Event sender role spoofing

**Статус:** ✅ закрыто через `services/operators.ensure_role`. Каждая
admin-команда проверяет роль из БД, не из event.senderRole.

### MX-3 — MessageCallback type confusion

**Статус:** ✅ закрыто в PR #99 (isinstance MessageCreated guard).

### MX-4 — Attachment MIME confusion (новый вектор 2026)

Житель посылает файл с `.exe`-payload но MIME `image/jpeg` (или
наоборот). `image_attachments_from_event` сейчас trust'ит MAX-API
MIME без re-verification.

**Текущее покрытие:** MAX-API serverside фильтрует extension'ы (?
не documented). Бот reuploads через `uploads.upload_bytes(suffix=…)`,
suffix берётся из `Path(filename).suffix` — если житель прислал
`evil.jpg.exe` с MIME image, mid берётся `.exe`.

**Recommendation:** в backlog — добавить explicit MIME→suffix
re-validation через `python-magic` (libmagic) для attachments которые
бот reuploads.

### MX-5 — Broadcast amplification via SSRF (не релевантно)

Broadcast URL whitelist уже защищает: только `*.elizovomr.ru`,
`*.gosuslugi.ru`, `*.kamgov.ru`, `*.kamchatka.gov.ru`. SSRF невозможен
— бот не fetcher.

### MX-6 — Token leak via logs

**Статус:** частично. `LOG_LEVEL=INFO` в проде. `BOT_TOKEN` нигде в
коде не логируется напрямую. Но `cfg.dump()` (если бы был вызван
при ошибке) — light leak. Сейчас не зову нигде. ✅ accept.

### MX-7 — Replay attacks через dual-tracker

Если злоумышленник перехватит callback (man-in-the-middle на канале
MAX), он может replay — например, оператор-IT нажал «Заблокировать
жителя», атакующий replay'ит callback через час. Но MAX-клиенты
используют TLS; replay требует доступа к auth token. Если auth-token
скомпрометирован — replay наименьшая проблема. ✅ accept.

---

## Современные scam-векторы 2026 (новое)

### SC-1 — AI voice clone scam

**Статус:** ✅ закрыто в PR #103 (D2). `SECURITY_INFO_TEXT` явно
предупреждает про звонки «голосом главы администрации», созданные
через AI voice clone.

### SC-2 — Deepfake video для ID verification

**Статус:** N/A. Бот не делает ID verification через видео.

### SC-3 — SIM swap → MAX account takeover

**Статус:** mitigated by MAX itself (двухфакторная аутентификация на
их стороне). Бот может только полагаться на платформу.

### SC-4 — Phishing через QR-коды

Жители получают QR от «сотрудника администрации» с ссылкой
«подписать заявление через бот». QR ведёт на phishing-сайт.

**Защита:** OPERATOR_SECURITY.md §1.5 содержит инструкцию «никогда
не передавайте QR/ссылки от имени бота». Контент-фильтр в боте не
позволяет — бот не показывает QR.

**Recommendation:** документировать явно в `OPERATOR_SECURITY.md`
секция «QR-фишинг» (отдельный пункт).

### SC-5 — Импер­сонация гос-брендов через домены

Атакующий регистрирует `elizovomr-portal.ru` (lookalike
`elizovomr.ru`). Жителю присылается ссылка через social, он считает,
что это официально.

**Текущая защита:**
- URL defang в admin chat — защищает оператора, не жителя.
- Broadcast URL whitelist — защищает житель от спуфа через бот.
- НО: если житель сам пришёл в бот по фишинг-ссылке, бот ничего не
  сделает.

**Recommendation:** в backlog — рассылка от бота с напоминанием
«официальный домен администрации — `elizovomr.ru`, остальное —
фишинг». Раз в квартал.

### SC-6 — «Второй бот» scam

**Статус:** ✅ закрыто в PR #103 (D2). `SECURITY_INFO_TEXT` явно
предупреждает «администрация не запускает второго бота для
финансовых операций».

### SC-7 — Социальная инженерия через комментарии в обращениях

Житель в `summary` пишет «Я оператор Сидоров, мне нужно срочно
заблокировать пользователя X». Если новый оператор-стажёр случайно
выполнит запрос — атака succeed.

**Текущая защита:** все admin-команды требуют `ensure_role(OP|SH|IT)`
для max_user_id, не из текста. Текст в `summary` — только для чтения,
не парсится как команда. ✅ OK.

### SC-8 — Steal-session через социальную инженерию операторов

Атакующий пишет оператору в личку «привет, я IT-админ, помоги
протестировать /erase на тестовом аккаунте, пришли мне result».
Оператор вставляет в чат с ботом → /erase реального жителя
выполняется. Compliance issue.

**Защита:** `ensure_role` + audit_log. Но social-engineering
обходит технический контроль.

**Recommendation:** в `OPERATOR_SECURITY.md` усилить раздел
«IT-команды через приватные DM подозрительны». Текущая редакция
описывает фишинг от имени жителя, не от имени оператора.

---

## Verification commands

```bash
# A1 проверка
grep -nE "send_admin_text\(" bot/aemr_bot/services/cron.py
# Каждый alert site должен иметь critical=True после fix.

# A3 проверка
python -c "from aemr_bot.utils.url_defang import _BARE_DOMAIN_PATTERN; print(_BARE_DOMAIN_PATTERN.findall('phish.cn xn--evil.xn--p1ai'))"
# Должно возвращать оба после fix.

# A7 проверка
python -c "from aemr_bot.services.admin_events import _mask_phone; print(_mask_phone('+71'))"
# После fix: '—'.

# Полная сверка cron-registry vs реальность
python -c "from aemr_bot.services.cron_registry import JOB_REGISTRY; print(len(JOB_REGISTRY))"
# Должно быть 17.
```

---

## Recommendations / priority

| Finding | Severity | Effort | Когда |
|---|---|---|---|
| A1 — alerts через quiet | 🔴 Critical | 30 min | **Срочный PR #127, до Cluster A** |
| A2 — cache initial state | 🟡 Medium | 10 min | Можно в одном PR с A1 |
| A3 — TLD list расширить | 🟡 Medium | 20 min | Отдельный PR |
| A4 — case-sensitivity whitelist | 🟡 Medium | 30 min | Отдельный PR |
| A5–A8 | 🟢 Low | — | Backlog |
| SC-7, SC-8 operator security | 🟢 Low | 1ч | После Cluster G |
| MX-4 MIME re-validation | 🟢 Low | 2ч | Backlog (нужен `python-magic` dep) |

**Срочный action — A1.** Каждая ночь quiet режима = потенциальный
тихий failure backup'а и retention-cron'ов. Это **прод-проблема**.

---

## Status update

| Item | Status |
|---|---|
| Baseline 2026-05-27 closure | ✅ D1/D2 закрыты в PR #103 |
| Новые findings 2026-05-28 | A1 🔴 + 3 🟡 — fixes планируются |
| MAX-bot threat model | актуализирован |
| Modern scam vectors 2026 | актуализирован |

После fix'а A1 + A2 — следующая делта будет минимальной.
