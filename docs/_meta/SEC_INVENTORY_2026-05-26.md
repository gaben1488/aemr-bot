# Inventory: external inputs

Срез на `a2a2b87` (2026-05-26). Цель — карта всех точек, где недоверенные
данные попадают в процесс бота, чтобы security-review знал, что аудитить.

## Таблица входов

| Категория | Источник | Куда (file:line) | Trust | Validation | Sink |
|---|---|---|---|---|---|
| MAX MessageCreated (личка) | житель | bot/aemr_bot/handlers/appeal.py:534 → state-таблица :64 | external_anon | length-clamp (cfg.summary/name/address_max_chars), `_HAS_ALNUM` regex | БД (appeals.summary, users.first_name/phone/dialog_data), admin chat (admin_card), audit_log |
| MAX MessageCreated (admin chat) | оператор | bot/aemr_bot/handlers/appeal.py:545 | internal_admin | `is_admin_chat`+`ensure_operator`/`ensure_role` | reply жителю, БД (broadcasts, operators, settings), audit_log |
| MAX MessageCallback (citizen) | житель | bot/aemr_bot/handlers/appeal.py:480 → `_dispatch_citizen_callback` :461 | external_anon | exact/prefix whitelist; FSM-state guard (`_ensure_funnel_callback_state` :124) | смена DialogState, locality/topic из settings_store по индексу |
| MAX MessageCallback (admin) | оператор | bot/aemr_bot/handlers/admin_callback_dispatch.py:254 | internal_admin | префикс-таблица + `ensure_role/operator` внутри run_* | block/unblock/erase/setting_update, broadcast confirm/stop |
| MAX BotStarted | житель | bot/aemr_bot/handlers/start.py:286 | external_anon | guard `is_admin_chat` | welcome screen, users insert |
| MAX contact-attachment (vCard) | житель | bot/aemr_bot/utils/attachments.py:160 (extract_phone), :230 (name) | external_anon | TEL: linesplit, `VCF_INFO_MAX_CHARS=10_000` cap | users.phone, users.first_name |
| MAX location-attachment | житель | bot/aemr_bot/utils/attachments.py:71 → handlers/appeal_geo.py:71 | external_anon | float() coercion, lat/lon без bounds | dialog_data.detected_*, services/geo lookup (read-only из seed) |
| MAX image/video/file attachment | житель | bot/aemr_bot/utils/attachments.py:49 (collect) | external_anon | type whitelist `ALLOWED_APPEAL_TYPES` ({image,video,file}); `attachments_max_per_appeal=20` | БД (appeals.attachments JSONB), admin relay (services/admin_relay.py:157) |
| MAX swipe-reply link.message.text | оператор+бот | bot/aemr_bot/handlers/operator_reply.py:660 (`_extract_reply_target_mid`) | mixed | SEC #3: marker `🆔 №N` парсится только если sender.is_bot=True | определяет target appeal для доставки ответа жителю |
| /-команды от жителя | житель | bot/aemr_bot/handlers/start.py:292 (start/help/menu/cancel/forget/policy/rules/subscribe/unsubscribe/export) | external_anon | `_is_admin_chat` guard | reset_state, erase_pdn (audit), JSON-export всего профиля |
| /-команды от оператора | оператор | bot/aemr_bot/handlers/admin_commands.py:133 (open_tickets/stats/reply/reopen/close/erase/setting/diag/backup/op_help/add_operators) | internal_admin | `ensure_operator`/`ensure_role(IT)`; int-parse, json.loads для /setting | appeals.status flip, users.erase_pdn, settings_store, operators.upsert |
| /setting `<key> <json>` value | IT-оператор | bot/aemr_bot/handlers/admin_commands.py:314 (json.loads :337) | internal_admin | `settings_store.validate` (SCHEMA, URL whitelist :29) | settings table, audit_log (только len/kind) |
| Settings UI text edit | IT-оператор | bot/aemr_bot/handlers/admin_settings.py:875 (handle_settings_edit_text) | internal_admin | `settings_store.validate` SCHEMA по ключу; `_clip_audit_value=200` | settings.value, audit_log details (before/after clipped) |
| Settings UI obj_add (2-3 строки) | IT-оператор | bot/aemr_bot/handlers/admin_settings.py:987 (_apply_obj_add) | internal_admin | split('\n'), require ≥2 строки; SCHEMA validate | settings JSONB list emergency_contacts/transport_dispatcher_contacts |
| Broadcast wizard text | IT/COORD | bot/aemr_bot/handlers/broadcast.py:167 (`_handle_wizard_text`) | internal_admin | `cfg.broadcast_max_chars=1000`, in-memory TTL 5min | broadcasts.text → массовая рассылка всем подписчикам |
| Broadcast images | IT/COORD | bot/aemr_bot/handlers/broadcast.py:244 | internal_admin | `broadcast_max_images` из settings (1–20) | broadcasts.attachments → outbound |
| Operator reply text (свайп/intent/cmd) | оператор | bot/aemr_bot/handlers/operator_reply.py:473 (`_deliver_operator_reply`) | internal_admin | `cfg.answer_max_chars=300`; `_check_reply_dedupe`; `_reply_rejection_before_delivery` (152-ФЗ ст.21) | message.text жителю в личку, messages-table, audit_log |
| Operator reply images | оператор | bot/aemr_bot/handlers/operator_reply.py:337 | internal_admin | `limit=1` (только первая, остальные → warning) | вложение в outbound к жителю |
| Followup text («📎 Дополнить») | житель | bot/aemr_bot/handlers/appeal_funnel.py:566 | external_anon | SEC #5 rate-limit (5/час, 30с min interval); guard consent_pdn/status | appeals.messages, admin card re-render, relay вложений в admin chat |
| /add_operators body (lines) | IT-оператор | bot/aemr_bot/handlers/admin_commands.py:381 (:404) | internal_admin | line.split, role enum check, self-promotion guard | operators upsert (это RBAC primitive!) |
| /erase max_user_id=/phone= | IT-оператор | bot/aemr_bot/handlers/admin_commands.py:252 (:282 phone) | internal_admin | int parse; sentinel ANONYMOUS_MAX_USER_ID block; trim phone | users.erase_pdn (физическое удаление PII) |
| Webhook POST /max/webhook | MAX server | bot/aemr_bot/main.py:144 (`_max_webhook`) | external_authed | hmac.compare_digest на X-Max-Secret; semaphore=32 | dp.handle(event) → handlers chain |
| HTTP GET /livez,/readyz,/healthz | внешний (Docker/watchdog) | bot/aemr_bot/health.py:124,129,134 | external_anon | `_is_local_request` — внешним отдаём только {"ok":bool} | read-only heartbeat + DB ping (cached 10s) |
| GitHub REST responses | github.com | bot/aemr_bot/services/repo_sync.py:166 (`_request` → :171 await resp.json()) | external_authed (PAT) | aiohttp parses JSON; `.get()` без схемы; status==200/201 check | branch/file SHA, PR url+number, base64 decode runtime_config.json :376 |
| GitHub `seed/runtime_config.json` (fetch для diff) | github.com | bot/aemr_bot/services/repo_sync.py:358 (`fetch_main_runtime_config`) | external_authed | base64.b64decode → json.loads (try/except) | сравнение с локальным settings_store (только read-only diff display) |
| Env vars (BOT_TOKEN/DATABASE_URL/...) | оператор VPS | bot/aemr_bot/config.py:8 (pydantic Settings) | system | pydantic types + `_empty_str_to_none`, `_enforce_webhook_secret` (min 16) | runtime config во все сервисы |
| Env GITHUB_PAT | VPS .env | bot/aemr_bot/services/repo_sync.py:77 (os.environ.get) | system | strip(); `load_config_from_env_and_settings` None-check | Bearer token в GitHub Authorization header |
| Env BACKUP_GPG_PASSPHRASE | VPS .env | bot/aemr_bot/services/db_backup.py | system | pydantic; `backup_allow_unencrypted` strict gate | gpg --symmetric stdin |
| seed/*.json (topics, contacts, dispatchers) | seed файлы (репо) | bot/aemr_bot/services/settings_store.py:253 (`_read_seed_json`) | internal_trusted | json.loads (raise при битом JSON); seed_if_empty :267 проверяет existing | settings_store rows (только если ключ отсутствует) |
| seed/welcome.md, seed/consent.md | seed файлы | bot/aemr_bot/services/settings_store.py:260 | internal_trusted | read_text utf-8 | settings_store.welcome_text / consent_text |
| seed/geo/*.geojson | seed файлы | bot/aemr_bot/services/geo.py:31 (shapely shape) | internal_trusted | json.loads, shapely.geometry.shape | reverse geocoding result (locality+street+housenumber) |
| seed/PRIVACY.pdf | seed файл | bot/aemr_bot/services/policy.py:30 (ensure_uploaded → upload_path) | internal_trusted | path.exists() | upload to MAX file storage, token в settings.policy_pdf_token |
| MAX get_chat_members (admin chat) | MAX server | bot/aemr_bot/handlers/admin_operators.py:88 (`_safe_get_chat_members`) | external_authed | try/except → [] fallback | список кандидатов в operators upsert |
| Cron: events retention (delete) | system | bot/aemr_bot/services/cron.py:207 | system | cutoff = now-30d | DELETE events WHERE received_at < cutoff |
| Cron: audit_log retention | system | bot/aemr_bot/services/cron.py:232 | system | cfg.audit_log_retention_days (30..3650) | DELETE audit_log |
| Cron: pdn_retention erase | system | bot/aemr_bot/services/cron.py:376 | system | 30 дней после consent_revoked_at, `has_open_appeals` skip | erase_pdn (физ. удаление PII) |
| Cron: backup_db | system | bot/aemr_bot/services/cron.py:145 → services/db_backup.py | system | env-based; не принимает user-input | pg_dump + gpg → /backups + опц. S3 |
| Cron: external healthcheck ping | env URL | bot/aemr_bot/services/cron.py:801 | system | settings.healthcheck_url (env-supplied) | outbound aiohttp.get |
| Cron: funnel_watchdog | DB | bot/aemr_bot/services/cron.py:455 | system | recover_batch_size cap | bot.send_message(user_id) жителю + admin alert |
| MAX Update payload (idempotency) | MAX | bot/aemr_bot/services/idempotency.py:82 (`claim`) | external | summary-only payload (фильтр в :104, без attachments/text) | events.payload JSONB |
| DB users.first_name / phone | БД (записано шагом воронки) | services/users.py через current_user | internal_trusted | source — те же external_anon шаги выше | admin_events `_describe_user` mask phone (:34), admin_card |

## Suspicious patterns (без рекомендаций — только указатели)

**Логи могут содержать PII.** `operator_reply.py:752` пишет `text_len=%d` (OK), но `_dispatch_citizen_callback` в `appeal.py:494` логирует `payload_prefix`, а в :492 для geo-callback'ов — `payload=%s` целиком (geo-payload содержит lat/lon? — нет, только `geo:confirm/edit_address/other_locality`, безопасно). `admin_settings.py:923` пишет в audit_log `before/after` с clip=200 — длинный welcome/consent попадает усечённо в БД, retention 365д.

**Идентификация целевого жителя через текст сообщения.** `operator_reply.py:660` извлекает `appeal_id` из текста цитируемого сообщения (`🆔 №N`). Защита SEC #3 (`replied_sender_is_bot`) на месте, но если когда-нибудь житель пришлёт бот эту строку в свой текст обращения, а бот её процитирует жителю в админ-чат и оператор свайпнет — теоретическая разоблачается через `is_bot=True` на цитируемом сообщении. Стоит проверить, не относится ли любой relay-форвард к bot-authored.

**Settings JSON через `json.loads(raw_value)`** — `admin_commands.py:336`. Любой валидный JSON парсится, потом валидируется SCHEMA. Тип-конфьюзинг (передать число где ждут строку) ловится `isinstance(value, expected)`, но глубокая структура (вложенные dict в emergency_contacts) проверяется только наличием required ключей — не запрещены лишние ключи.

**URL whitelist white-list-by-suffix.** `settings_store.py:21` — суффикс-match: `evil.kamgov.ru.attacker.com` НЕ проходит (требуется host == suffix или endswith `.` + suffix). OK. Зато `gosuslugi.ru` shared между всеми — компромисс одного субдомена этого хоста даст phishing.

**Webhook secret в env, не в БД.** `main.py:154` сравнение через `hmac.compare_digest` — timing-safe. OK. Но если оператор задал WEBHOOK_SECRET без `BOT_MODE=webhook` — он молча игнорируется, никакой подсказки.

**Idempotency fail-CLOSED.** `idempotency.py:131` намеренно fail-closed на DB error (SEC #7). При длительной DB-проблеме MAX повторит ack много раз → handler пропустит все. Поведение задокументировано.

**`extract_message_id` через JSON-path** — `attachments.py:31` `model_dump` без strict. Сломанный pydantic-модель → пустой dict, attachment молча отбрасывается. Не security issue, но silent-drop ломает UX.

**Logs в admin chat экранируются?** `admin_settings.py:917` шлёт `f"❌ {msg}"` без эскейпа — msg из validate, безопасный (статичные строки). `admin_commands.py:341` `f"⚠️ Настройка не обновлена: {reason}"` — reason от validate, OK. Где user-text прокидывается — `admin_card` использует card_format service (отдельный обзор).

**`/export` отдаёт полный JSON жителю** — `start.py:202`. JSON включает summary + operator answer полностью. Размер не лимитирован, потенциальная DoS на больших историях (limit=500 :215).

**`_extract_reply_target_mid` через `link.message.mid`** — `operator_reply.py:194` поддерживает и Pydantic-модель, и dict. Дублирование путей всегда повышает риск пропустить guard в одном из них; SEC #3 проверка `is_bot` повторена для обоих веток.

**Cron `_job_funnel_watchdog` шлёт уведомление по `user_id` без согласия.** `cron.py:489` — `bot.send_message(user_id=max_user_id, text=...)`. Жителю пишут «вы начали оформлять обращение». Это не PII-leak, но 152-ФЗ требует cleanup согласно retention, а тут активный outbound к жителю даже после ухода с воронки.

**GitHub API response десериализация.** `repo_sync.py:171` — `await resp.json()` без схемы; используются только `.get("sha"), .get("number"), .get("html_url"), .get("content")`. Если ответ malformed, получим None и fail-soft в SyncResult. Поверхность — только при компрометации GitHub.

**Семафор=32 на webhook.** `main.py:54` — bounded, OK. Без webhook-flow (current `BOT_MODE=polling`) не активен.

**`/erase phone=...`** проходит SQL-параметризованно через services/users.py (asyncpg), но конкатенация в audit `f"user max_id={target_id}"` идёт в `target` поле — статичный формат, без user-text.

**`_safe_get_chat_members` swallow** — admin_operators.py:97. На MAX-сбой возвращает [] → UI откатится к ручному вводу id. Любой transient error превращается в degraded path без оповещения IT.
