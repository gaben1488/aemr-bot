# Security Review Delta 2026-05-27

> Дельта-аудит после PR #98–#101 поверх baseline `SECURITY_REVIEW_2026-05-26.md`.
> Скоуп: только новый поверхностный код 24 часов (admin_bus hook,
> MessageCallback isinstance fix, dual-tracker, text-length guard) +
> непокрытые baseline'ом векторы. Ничего из закрытого SEC #1–#9 не
> повторяю; уже зафиксированные C1–C6/H1–H2/M1–M10 не дублирую.

---

## TL;DR

```
🔴 Critical:  0
🟡 Medium:    2 (settings text overflow → silent send fail, 2026 AI-voice scam в SECURITY_INFO_TEXT не явно)
🟢 Low:       2 (memory grow citizen tracker, idempotency window vs cb_id)
✅ Подтверждено OK: PR #98 hook idempotency + exception swallow, PR #99 MessageCallback type-guard, PR #100 atomic kind-check, callback authz double-layer, attachments path traversal, scripts shell injection
```

Главная находка делты — **PR #101 не покрывает admin-editable текстов**
(`welcome_text`, `consent_text` через `settings_store`). SCHEMA `max_len=4000`
совпадает с MAX-API hard limit, и при добавлении placeholder'а
(`{policy_url}`) или ack-маркера на отправке бот падает в `ValueError:
text должен быть меньше 4000 символов` — тихая регрессия типа
OP_HELP_FULL_LEGACY, но через UI настроек.

Немедленной починки не требуется. Все 🔴 baseline'а закрыты или
переведены в «accept» в `SECURITY.md §10b`.

---

## Дельта vs 2026-05-26

| PR | Описание | Новый attack surface | Делта-вердикт |
|---|---|---|---|
| #98 | `admin_bus.install_outgoing_tracker_hook` — monkey-patch `bot.send_message` | tracker-sync hook, exception-swallow в post-send | OK (idempotent + best-effort) |
| #99 | `AdminChatActivityMiddleware` — isinstance(MessageCreated) guard | type-confusion risk закрыт | OK |
| #100 | `menu_tracker` dual-tracker (`ChatState`) | in-memory dict per-chat_id без эвикции | 🟢 low (рост ≪1MB на 5K жителей) |
| #101 | `test_texts_length_guard.py` — preventive guard ≤3900 char | не покрывает admin-editable текстов из БД | 🟡 medium |

Новых критических классов угроз PR'ы не открыли. Изменения локальные,
обратной совместимые, тестами покрыты.

---

## Активные находки

> **Status update 2026-05-27 (late):** D1 и D2 закрыты в PR #103
> (`sec(D1+D2): SCHEMA overflow guard + 2026 AI-voice scam в SECURITY_INFO`).
> D3 и D4 остаются принятыми (low-priority technical debt). Подробнее
> в разделе «Закрытые в PR #103» ниже. Описания D1/D2 ниже оставлены
> для исторической полноты.

### 🟢 D1 (CLOSED in PR #103): Admin-editable `welcome_text` / `consent_text` могут переполнить MAX-API limit
- **Где:** `bot/aemr_bot/services/settings_store.py:348,358` (SCHEMA
  `max_len=4000` для `welcome_text` и `consent_text`).
- **Сценарий:** IT-оператор через UI настроек сохраняет 4000-символьный
  `consent_text` с обязательным `{policy_url}` (50–200 char). При рендере
  `get_consent_request_text` подставляет policy_url → итоговая длина
  >4000. `bot.send_message` локально падает в `ValueError: text должен
  быть меньше 4000 символов` (`bot/.venv/.../maxapi/methods/send_message.py:72`).
  Аналогично для `welcome_text`, если бот добавит ack-маркер или
  event_header (на сейчас не добавляет, но любая будущая правка добавит).
- **Impact:** silent fail — житель ничего не видит, ошибка только в
  docker logs. Симметрично выявленному в PR #101 классу регрессии
  (`OP_HELP_FULL_LEGACY`), но через DB-настройки, минуя CI guard.
- **Fix:** в `SCHEMA` опустить `max_len` для текстовых ключей до 3800
  (запас 200 char под placeholder + ack-маркеры), либо при рендере в
  `get_text_with_fallback` / `get_consent_request_text` проверять
  итоговую длину после format и логировать WARNING + возвращать fallback.

### 🟢 D2 (CLOSED in PR #103): 2026-актуальные скам-векторы (AI voice clone, fake-bot canonical) явно не покрыты в welcome
- **Где:** `bot/aemr_bot/texts.py:1-8` (WELCOME), `seed/welcome.md:1-9`,
  `bot/aemr_bot/texts.py:183-228` (SECURITY_INFO_TEXT).
- **Сценарий:** `SECURITY_INFO_TEXT` строка 200 предупреждает «бот не
  звонит вам голосом», но **AI-клонирование голоса родственника /
  главы Администрации** как явный сценарий не назван. На фоне роста
  AI-voice scam успешности с 12% (2024) до 34% (2026) (см. baseline
  Vector 9) — пожилой житель не свяжет «бот не звонит» с «мне звонил
  Глава Администрации».
- **Где Vector 1 (Fake-bot phishing):** baseline C5-fix («указать
  реальный username бота в welcome для self-verification») по факту
  **не выполнен** — `seed/welcome.md:1-9` и `WELCOME:1-8` не содержат
  ни username, ни ссылки на elizovomr.ru как canonical-источник.
- **Где deepfake admin (item 9.3):** публичного канала верификации
  «настоящие посты Администрации только здесь» в текстах нет.
- **Impact:** социалка через незакрытые сценарии. Не код-issue, копи + продукт.
- **Fix:** в `seed/welcome.md` добавить «Проверьте: настоящий бот —
  только по ссылке с elizovomr.ru». В `SECURITY_INFO_TEXT` строкой 200
  явно: «не звонит голосом — даже если голос похож на знакомого». В
  отдельную кнопку «Защита от мошенников» — пункт «как опознать
  настоящие посты Администрации в MAX».

### 🟢 D3: `menu_tracker._state_by_chat` растёт без эвикции
- **Где:** `bot/aemr_bot/utils/menu_tracker.py:78,89` — глобальный
  `dict[int, ChatState]`. Запись добавляется в `_state_for(chat_id)`
  для **каждого** уникального chat_id.
- **Сценарий:** для citizen-чатов запись создаётся через
  `note_editable_send` (`handlers/menu.py:122`, `utils/event.py:281`).
  После многих месяцев работы dict содержит ChatState на каждого
  жителя, открывавшего меню. Эвикция вызывается только при edit-fail
  (`menu.py:107`, `event.py:268`) или ручном `clear_all()` (тесты).
- **Impact:** ~50 байт на ChatState × N жителей. На 5K жителей —
  ~250KB, на 50K — ~2.5MB. Никакой DoS-сценарий не реализуем (память
  ничтожна), но **рост неограничен**. Технический долг, не security.
- **Fix:** периодический cron (например, в `services/cron.py`)
  эвиктить запись `_state_by_chat` для chat_id, не имевших активности
  >7 дней. Альтернатива — `OrderedDict` с LRU-cap 10000.

### 🟢 D4: Idempotency window 30 дней vs callback_id «вечный»
- **Где:** `bot/aemr_bot/services/idempotency.py:122,172` — `Event`
  retention 30 дней (`services/cron.py:207`).
- **Сценарий:** теоретический replay старого `MessageCallback` >30
  дней спустя: idempotency-ключ удалён ретеншеном, повторное событие
  с тем же `cb_id`/`mid` пройдёт. Однако (a) MAX не передаёт `cb_id`
  внешним сторонам, (b) `op:close:42` всё равно отбивается
  `ensure_operator` если callback пришёл не из admin-чата, (c) `op:close`
  на уже закрытое обращение idempotent (БД-вариант).
- **Impact:** очень низкий — нужен compromised оператор, способный
  сохранить cb_id, дождаться 30+ дней, послать его в admin-группе.
  Защита `ensure_operator` + БД-инвариант close уже идемпотентен.
- **Fix:** не требуется. Зафиксировать в SECURITY.md §10b как accept.

---

## Подтверждено чисто

- **PR #98 admin_bus hook idempotency:** маркер `_aemr_admin_outgoing_tracker_installed`
  (`admin_bus.py:135,191-196`) предотвращает рекурсивную обёртку. Тестов
  на double-install нет, но guard корректен.
- **PR #98 exception swallow:** post-send sync обёрнут в `try/except`
  (`admin_bus.py:227-232`) с `log.debug(..., exc_info=False)` — не
  ломает caller, и PII не попадает в логи (только сообщение
  «post-send sync failed»).
- **PR #98 race при concurrent send:** asyncio single-loop, dict-операции
  атомарны. Wrapped `_wrapped_send` не вводит новых race-окон.
- **PR #98 positional args risk:** все вызовы `bot.send_message(chat_id=..., ...)`
  в коде — keyword-only (grep подтверждает). `kwargs.get("chat_id")` безопасен.
- **PR #99 type guard:** `isinstance(event_object, MessageCreated)`
  (`handlers/__init__.py:55-56`) корректно отбрасывает MessageCallback
  до tracker-sync.
- **PR #100 kind-check атомарность:** `can_edit` проверяет 3 условия
  одновременно (`menu_tracker.py:166-170`), kind-mismatch блокирует
  edit. Не вижу способа подделать.
- **Callback payload tampering (item 5):** двухслойная защита подтверждена.
  Citizen callback `op:close:42` из DM падает в первом фильтре
  (`appeal.py:500-503` — `is_admin_callback` отсекает не-admin-chat),
  а если каким-то путём прошёл — `run_close`/`run_reply_intent`/
  `run_erase_for_appeal`/`run_block_for_appeal` всё равно вызывают
  `ensure_operator(event)` → `is_admin_chat(event)` → False → no-op.
- **Attachment path traversal (item 6):** `services/uploads.py:33,70`
  — путь только из `NamedTemporaryFile(suffix=".bin"|".pdf")`. User
  control нет.
- **OS command injection (item 7):** `scripts/audit_vps.sh`,
  `scripts/auto-deploy.sh`, `scripts/install-*.sh` — все используют
  только env-vars (PROJECT_DIR, REPO_DIR) и `git rev-parse` hex
  (auto-deploy), без user-controlled данных.
- **PR #101 length guard module-scope:** тест корректно перебирает
  все `aemr_bot.texts.*` константы; на регрессию `OP_HELP_FULL_LEGACY`
  CI падает.
- **M5 outgoing reply URL whitelist:** `handlers/operator_reply.py:333-334`
  — `find_non_whitelisted_urls` блокирует операторские ответы с не-gov
  URL.
- **Defang в admin timeline:** `services/card_format.py:146-148` —
  `defang_url_in_text` экранирует URL'ы жителя только в timeline,
  оператор не может случайно тапнуть.
- **PII в callback payload:** `appeal.py:493-494` — только `prefix` на
  info, полный payload на debug. Соответствует baseline M1-fix.
- **PII followup от жителя:** уже задефанжен через `card_format` (см. выше).

---

## Рекомендации (низкоприоритетное)

1. **D1-fix (наиболее заметный):** опустить `welcome_text`/`consent_text`
   `max_len` в `settings_store.SCHEMA` до 3800 либо добавить
   render-time check в `get_text_with_fallback`. PR ≤30 строк.
2. **D2-copy:** в `seed/welcome.md` добавить 2 строки про canonical
   username + ссылку elizovomr.ru. В `SECURITY_INFO_TEXT` явно про
   AI-voice clone. Docs-PR, не код.
3. **D3-cleanup:** опционально cron-эвикция stale tracker-records.
   Откладывается до момента, когда `_state_by_chat` начнёт занимать
   заметную память.
4. **D4-accept:** зафиксировать в `SECURITY.md §10b` 30-day callback
   replay window как known limitation (требует compromised operator +
   long-window storage).
5. **Test gap:** `test_admin_outgoing_hook.py` не покрывает double-install
   guard. Добавить regression-test (5 строк), чтобы будущая правка не
   сняла маркер.

---

## Дальше

Дельта-PR не требуется — все находки 🟡/🟢 либо технический долг,
либо копи. Главное действие — D1 в `settings_store.SCHEMA`, если/когда
admin начнёт активно редактировать `welcome_text`/`consent_text` под
4000 char.

---

## Закрытые в PR #103 (Update 2026-05-27 late)

PR #103 (`sec(D1+D2): SCHEMA overflow guard + 2026 AI-voice scam в
SECURITY_INFO`, merged 2026-05-27T01:12:11Z) закрыл оба активных 🟡
findings:

**D1 → CLOSED:**
- `settings_store.SCHEMA["welcome_text"]["max_len"]` опущен с 4000 до
  **3800** (`bot/aemr_bot/services/settings_store.py:357`).
- `settings_store.SCHEMA["consent_text"]["max_len"]` опущен с 4000 до
  **3800** (`bot/aemr_bot/services/settings_store.py:368`).
- 200 char запаса покрывают будущие ack-маркеры/event_header, плюс
  ~100 char под placeholder-подстановку (`{policy_url}` до 200 char
  заменяет 12-char шаблон → +188 char/render).
- 2 regression-теста в
  `bot/tests/test_settings_store_validation.py::test_welcome_text_max_len_below_max_api_limit`
  и `test_consent_text_max_len_below_max_api_limit` валят CI на
  попытке откатить ≤ 3800.

**D2 → CLOSED:**
- `bot/aemr_bot/texts.py::SECURITY_INFO_TEXT` (lines 200-206) расширен:
  - Строка «бот не звонит вам голосом» дополнена объяснением
    AI-клонирования голоса по 10-секундной записи + совет «положите
    трубку и перезвоните адресату сами».
  - Новый пункт списка: «не существует в виде «второго» или
    «дублирующего» бота. Открывайте только по ссылке с elizovomr.ru».
- Финальная длина `SECURITY_INFO_TEXT` после правок: 2133 char (запас
  до `MAX_LEN=3900` — 1767 char). Длина-guard PR #101 удовлетворён.

**Что осталось активным:**
- 🟢 D3 (memory grow tracker): accept как technical debt. При росте
  >50K жителей — пересмотреть.
- 🟢 D4 (idempotency 30-day replay): accept в SECURITY.md §10b как
  known limitation. Атака требует compromised оператора + multi-month
  storage cb_id.

---

> **Архивный статус:** этот документ — снимок security-review на момент
> 2026-05-27 (начало дня). Описания D1/D2 в разделе «Активные находки»
> сохранены **как историческое описание проблем до их закрытия**, не
> как актуальные TODO. Для текущего security-статуса — см. этот раздел
> «Закрытые в PR #103» + `docs/SECURITY.md` (canonical).
