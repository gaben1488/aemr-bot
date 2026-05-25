# FILE_INVENTORY — полный реестр файлов репо `aemr-bot`

Срез на 2026-05-25 (после зачистки stale-файлов и переименования компонентов под Регламент v7). Сверено: `git ls-files`, чтение исходников, `grep` зависимостей.

Условные обозначения статуса:
- ✅ актуален и нужен
- 🟡 нужен, но устарел / требует обновления
- 🔴 не нужен / дубль / мёртвый
- ⚪ исторический архив (нельзя удалить по compliance / истории решений)

---

## Корень репо

| Файл | Назначение | Статус | Зависят |
|---|---|---|---|
| `README.md` | Главная вывеска: что за бот, как запустить, навигация по docs/ | ✅ | человек, GitHub UI |
| `REPO_INDEX.md` | Указатель «полный индекс — `aemr-bot-index.md`», инструкция по перегенерации | ✅ | человек, `.github/workflows/repo-index.yml` |
| `aemr-bot-index.md` | Авто-генерируемый flat-индекс ~55K строк всех текстовых файлов для LLM-инструментов | 🟡 | `scripts/make_repo_index.py`, `.github/workflows/repo-index.yml`; раздувает diff'ы в main, но это сознательное решение |
| `.dockerignore` | Что НЕ копировать в образ (docs, *.md, .env*, бэкапы, «— копия.py») | ✅ | `infra/Dockerfile` |
| `.gitignore` | Стандарт + кастомные шаблоны для Windows бэкапов, `_local-backup/` | ✅ | git |

---

## `.github/workflows/`

| Файл | Назначение | Статус | Зависят |
|---|---|---|---|
| `ci.yml` | Lint+types+security+pytest+pip-audit на push/PR в main | ✅ | GitHub Actions |
| `repo-index.yml` | Перегенерация `aemr-bot-index.md` на push в main | ✅ | scripts/make_repo_index.py |

---

## `bot/` корень

| Файл | Назначение | Статус | Зависят |
|---|---|---|---|
| `pyproject.toml` | Метаданные пакета `aemr-bot`, dependencies (`maxapi~=1.1` и т.д.), dev-deps, ruff/mypy/pytest config | ✅ | `infra/Dockerfile`, `uv.lock`, CI |
| `uv.lock` | Resolved lock-файл всех зависимостей. Коммитится намеренно для drift-prevention | ✅ | docs/DEPS.md, CI guard |
| `alembic.ini` | Конфиг Alembic (script_location=aemr_bot/db/alembic) | ✅ | CMD в Dockerfile |

---

## `bot/aemr_bot/` (top-level)

| Файл | Назначение | Статус | Зависят |
|---|---|---|---|
| `__init__.py` | Пустой пакет-маркер | ✅ | — |
| `main.py` | Entry-point: bot+dp, preflight token, seed, hydrate wizards, scheduler, polling/webhook loop | ✅ | Dockerfile CMD |
| `config.py` | Pydantic Settings: BOT_TOKEN, DB_URL, лимиты, backup, broadcast, retention | ✅ | весь код |
| `health.py` | aiohttp `/livez` / `/readyz` / `/healthz` + heartbeat_pulse | ✅ | main.py, infra/docker-compose.yml, healthwatch.sh |
| `keyboards.py` | 1248 строк inline-клавиатур MAX (consent, menu, op_help, broadcast и пр.) | ✅ | все handlers/* |
| `texts.py` | 723 строки статических текстов (WELCOME, OP_HELP, CITIZEN_COMMAND_IN_ADMIN_CHAT) | ✅ | handlers/* |

---

## `bot/aemr_bot/db/`

| Файл | Назначение | Статус | Зависят |
|---|---|---|---|
| `__init__.py` | пакет-маркер | ✅ | — |
| `models.py` | SQLAlchemy: User, Operator, Appeal, Message, Broadcast, BroadcastTemplate, AuditLog, Setting, DialogState enum (10 состояний) | ✅ | весь код, alembic env |
| `session.py` | engine + session_scope() async ctx | ✅ | все services/handlers |

---

## `bot/aemr_bot/db/alembic/`

| Файл | Назначение | Статус | Зависят |
|---|---|---|---|
| `env.py` | стандартный async env.py для alembic upgrade head | ✅ | alembic.ini |
| `script.py.mako` | шаблон для `alembic revision` | ✅ | alembic |

### Миграции `versions/`

Все миграции линейные (0001 → 0017), каждая backward-compatible. Удалять нельзя — порушит linear chain в проде.

| Файл | Что делает | Статус |
|---|---|---|
| `0001_initial.py` | начальная схема (users, operators, appeals, messages, settings, audit_log, dialog_states) | ✅ |
| `0002_broadcast.py` | таблицы рассылок + подписчиков | ✅ |
| `0003_phone_normalized.py` | колонка users.phone_normalized + btree-индекс для `/erase phone=...` | ✅ |
| `0004_indexes_and_autovacuum.py` | индексы на FK appeals.assigned_operator_id, messages.operator_id + autovacuum tuning | ✅ |
| `0005_appeals_locality.py` | колонка appeals.locality (10 поселений ЕМО) | ✅ |
| `0006_consent_revoked_at.py` | отделить «никогда не давал согласие» от «явно отозвал» | ✅ |
| `0007_consent_broadcast_anonymous.py` | consent_broadcast_at + closed_due_to_revoke + anonymous pattern | ✅ |
| `0008_backfill_consent_broadcast.py` | backfill consent_broadcast_at для жителей до 0007 | ✅ |
| `0009_partial_indexes_for_hot_paths.py` | три partial-индекса hot-path запросов | ✅ |
| `0010_pg_ops_hardening.py` | statement_timeout=30s + pg_stat_statements (зависит от shared_preload в compose) | ✅ |
| `0011_wizard_state_persistence.py` | таблица op_wizard_state + broadcast_wizard_state — пережить рестарт | ✅ |
| `0012_messages_appeal_created_index.py` | композитный индекс messages(appeal_id, created_at) | ✅ |
| `0013_settings_synced_at.py` | settings.synced_at + commit_author ключи для repo_sync | ✅ |
| `0014_broadcasts_attachments.py` | broadcasts.attachments JSONB | ✅ |
| `0015_broadcast_templates.py` | таблица broadcast_templates | ✅ |
| `0016_broadcast_template_usage.py` | use_count + last_used_at в templates | ✅ |
| `0017_appeals_last_card_mid.py` | last_admin_card_mid (DDD pivot — event-log карточек) | ✅ |

---

## `bot/aemr_bot/handlers/`

| Файл | Назначение | Статус | Зависят |
|---|---|---|---|
| `__init__.py` | `register_handlers(dp)` + IdempotencyMiddleware | ✅ | main.py |
| `_auth.py` | get_operator / ensure_operator / ensure_role helper'ы | ✅ | все admin_*.py, broadcast*.py |
| `_common.py` | `current_user` async ctx-manager (открыть transaction, get_or_create user) | ✅ | start.py, menu.py |
| `admin_appeal_ops.py` | reply_intent / reopen / close / block / erase / show_attachments по конкретному appeal_id | ✅ | admin_commands.py, callback_router |
| `admin_audience.py` | меню «📊 Аудитория и согласия» (IT-only выборки + точечные block/unblock/erase) | ✅ | admin_commands.py |
| `admin_callback_dispatch.py` | таблицы _EXACT/_PREFIX для `broadcast:*` / `op:*` callback'ов | ✅ | appeal.py:on_callback |
| `admin_commands.py` | тонкий entry-point: register() 11 slash-команд + re-exports для appeal.py | ✅ | __init__.py |
| `admin_operators.py` | 991 строка — wizard добавления оператора (members API → role → name → confirm) | ✅ | admin_commands.py |
| `admin_panel.py` | show_op_menu, /op_help, /open_tickets, /diag, /backup | ✅ | admin_commands.py |
| `admin_settings.py` | 1031 строка — иерархическое меню «⚙️ Настройки бота» по 11 ключам settings_store | ✅ | admin_commands.py |
| `admin_stats.py` | /stats XLSX за период (today/week/month/quarter/half_year/year/all) | ✅ | admin_commands.py |
| `appeal.py` | главный entry-point: один message_callback + один message_created (state-таблица + admin-flow) | ✅ | __init__.py register |
| `appeal_funnel.py` | FSM-шаги воронки (contact → name → locality → address → topic → summary) + followup | ✅ | appeal.py |
| `appeal_geo.py` | reverse-geocoding flow (location attachment → услышим адрес из seed/geo) | ✅ | appeal_funnel.py |
| `appeal_runtime.py` | locks, recover_stuck_funnels, persist_and_dispatch_appeal (импортится из main.py) | ✅ | main.py, appeal_funnel.py |
| `broadcast.py` | мастер рассылок + фоновая отправка с rate-limit | ✅ | __init__.py |
| `broadcast_templates.py` | UI шаблонов рассылок (PR H): список / preview / apply / rename / edit / delete | ✅ | broadcast.py, admin_callback_dispatch |
| `callback_router.py` | declarative-таблица known callback payload-групп (на ввод новой кнопки) | ✅ | admin_callback_dispatch.py |
| `menu.py` | 1042 строки — общее меню жителя (кнопки) + контактные подменю | ✅ | appeal.py |
| `operator_reply.py` | swipe-reply и followup-логика жителя (вызывается из appeal.on_message) | ✅ | appeal.py |
| `start.py` | /start, /menu, /help, /policy, /rules, /subscribe, /unsubscribe, /forget, /cancel, /export, /whoami | ✅ | __init__.py |

---

## `bot/aemr_bot/services/`

| Файл | Назначение | Статус | Зависят |
|---|---|---|---|
| `__init__.py` | пакет-маркер (пустой) | ✅ | — |
| `admin_card.py` | render() admin appeal card с freshness-rule (edit vs new) | ✅ | admin_appeal_ops, operator_reply, appeal_runtime |
| `admin_events.py` | короткие текст-уведомления в служебную группу (block/unblock/consent/erase) | ✅ | start.py, menu.py |
| `admin_relay.py` | перенос вложений жителя в admin group, выделено из appeal.py | ✅ | operator_reply, appeal |
| `appeals.py` | 563 строки — CRUD по appeals, поиск, статусы, история | ✅ | handlers/* |
| `broadcast_templates.py` | persistence для broadcast_templates (CRUD) | ✅ | handlers/broadcast_templates |
| `broadcasts.py` | подписки + отправка муниципальных рассылок, reap_orphaned_sending | ✅ | main.py, handlers/broadcast |
| `calendar_ru.py` | производственный календарь РФ из seed/holidays.json (подавление SLA в выходные) | ✅ | cron.py |
| `card_format.py` | сборка карточек обращения (суть → ответ → дополнение → ...) | ✅ | admin_card.py, menu.py |
| `cron.py` | 797 строк — build_scheduler (13 cron-jobs: pulse, monthly, backup, SLA, retention) | ✅ | main.py |
| `db_backup.py` | pg_dump + gpg + локальный том + S3, BackupResult | ✅ | cron.py, admin_panel.py |
| `geo.py` | local reverse-geocoding по seed/geo/*.geojson (shapely) | ✅ | appeal_geo.py |
| `idempotency.py` | claim() отпечатка update — защита от повторов polling/webhook | ✅ | handlers/__init__.py |
| `operators.py` | CRUD по operators + bootstrap_it_from_env | ✅ | main.py, _auth.py, admin_operators |
| `policy.py` | ensure_uploaded() кэширует token PRIVACY.pdf в settings | ✅ | main.py, start.py |
| `progress.py` | прогресс-карта FSM-воронки (визуализация шагов жителю) | ✅ | appeal_funnel.py |
| `repo_sync.py` | PR с актуальным seed/runtime_config.json через GitHub API | ✅ | admin_settings.py |
| `settings_store.py` | DEFAULTS + SCHEMA (11 ключей) + get/set с url-whitelist | ✅ | main.py, admin_settings |
| `stats.py` | XLSX-отчёт за период через openpyxl | ✅ | admin_stats.py, cron.py |
| `uploads.py` | upload_bytes + file_attachment для двухшагового MAX upload | ✅ | main.py, broadcast |
| `users.py` | 630 строк — CRUD по users, normalize_phone, erase, audit | ✅ | весь admin-стек |
| `wizard_persist.py` | DB persistence для wizard state (миграция 0011) | ✅ | main.py hydrate, wizard_registry |
| `wizard_registry.py` | in-memory cache wizard state + intent оператора (replied_to) | ✅ | admin_operators, broadcast, operator_reply |

---

## `bot/aemr_bot/utils/`

| Файл | Назначение | Статус | Зависят |
|---|---|---|---|
| `__init__.py` | пакет-маркер (пустой) | ✅ | — |
| `attachments.py` | разбор Attachments из MAX-событий, deserialize_for_relay | ✅ | operator_reply, admin_relay |
| `background.py` | spawn_background_task с GC-защитой (вынесено из main.py) | ✅ | main.py, broadcast |
| `event.py` | адаптер над maxapi event-types: get_user_id, is_admin_chat, ack_callback, send_or_edit_screen | ✅ | весь handler-слой |
| `image_attachments.py` | тонкая обёртка над attachments для image-only фильтра | ✅ | broadcast |
| `menu_tracker.py` | per-chat tracker «какая карточка-меню сейчас актуальна» (freshness-rule) | ✅ | admin_audience, menu |

---

## `bot/tests/` (62 файла, ~24K строк)

| Файл | Назначение | Статус |
|---|---|---|
| `__init__.py` | пакет-маркер | ✅ |
| `conftest.py` | env BOT_TOKEN setdefault, sqlite+aiosqlite fallback | ✅ |
| `_helpers.py` | общие фабрики `_make_event`/`_make_callback_event`/`_fake_session_scope` (был копипаст в 14 файлов) | ✅ |

**Сервисы и handlers (по доменам, все ✅ актуальны):**

- Admin: `test_admin_appeal_ops.py`, `test_admin_callback_dispatch.py`, `test_admin_card_detached_safety.py`, `test_admin_card_render.py`, `test_admin_events.py`, `test_admin_events_descriptor.py`, `test_admin_handlers_small.py`, `test_admin_operators.py`, `test_admin_panel.py`, `test_admin_settings_audit.py`
- Appeal: `test_appeal_card_edit_policy.py`, `test_appeal_card_timeline.py`, `test_appeal_dispatcher.py`, `test_appeal_flow.py`, `test_appeals_service_pg.py`, `test_handlers_appeal_funnel.py`, `test_handlers_funnel.py`, `test_funnel_state_hardening.py`, `test_extract_location.py`
- Broadcast: `test_broadcast_handlers.py`, `test_broadcast_history_card.py`, `test_broadcast_templates_handlers.py`, `test_broadcast_templates_service_pg.py`, `test_broadcast_with_image.py`, `test_broadcasts_service_pg.py`
- Cron / backup: `test_cron_jobs.py`, `test_db_backup.py`, `test_db_backup_extra.py`
- Misc: `test_calendar_ru_full.py`, `test_card_format.py`, `test_callback_router.py`, `test_callback_router_coverage.py`, `test_geo.py`, `test_health.py`, `test_idempotency.py`, `test_image_attachments.py`, `test_keyboards.py`, `test_main_helpers.py`, `test_menu_tracker_edit_policy.py`, `test_progress.py`, `test_pure_functions.py`, `test_reliability_pass.py`, `test_repo_sync.py`, `test_services_no_db.py`, `test_settings_store_validation.py`, `test_uploads_policy_admin_relay.py`, `test_users_service_pg.py`, `test_wizard_registry.py`, `test_attachments_helpers.py`, `test_bot_init_concurrency.py`, `test_deps_environment.py`, `test_diag_extended.py`, `test_event_helpers.py`, `test_handlers_auth_broadcast.py`, `test_handlers_common.py`, `test_handlers_menu.py`, `test_handlers_menu_extra.py`, `test_handlers_operator_reply.py`, `test_handlers_start.py`, `test_operator_reply_closed_guard.py`, `test_operator_reply_with_image.py`, `test_final_p1_regressions.py`

Все 62 теста актуальны и покрывают как минимум по одному модулю handlers/services. Гонимы в CI.

---

## `infra/`

| Файл | Назначение | Статус | Зависят |
|---|---|---|---|
| `Dockerfile` | python:3.12-slim + non-root botuser + alembic upgrade head + run main | ✅ | docker-compose.yml |
| `docker-compose.yml` | services: db (postgres:16-alpine + pg_stat_statements), bot (read_only+cap_drop+mem_limit), профиль webhook (nginx+certbot) | ✅ | auto-deploy.sh, ROLLBACK.md |
| `init-letsencrypt.sh` | первичная выписка SSL через certbot (webhook режим) | ✅ | docs/SETUP.md (для webhook) |
| `.env.example` | шаблон всех env-переменных с описаниями + supercomment-блок по каждой переменной (расшифровка, дефолты, что произойдёт без значения) | ✅ | docs/SETUP.md |
| `.env` | реальный .env (через .gitignore исключён) | 🟡 | в `git ls-files` НЕ виден — OK; локальный файл существует |
| `nginx/feedback.conf` | reverse-proxy MAX webhook на bot:8080 + Let's Encrypt | ✅ | docker-compose webhook профиль |
| `aemr-bot.service` | systemd-юнит для автозапуска бота после reboot VPS (`docker compose up -d` после старта системы) | ✅ | docs/SYSADMIN.md §12a, scripts/install-systemd-autostart.sh |

---

## `scripts/`

| Файл | Назначение | Статус | Зависят |
|---|---|---|---|
| `auto-deploy.sh` | cron на VPS: pull → build → health-gate с rollback | ✅ | docs/SETUP.md, install-auto-deploy.sh |
| `install-auto-deploy.sh` | однократная установка deploy-key + crontab | ✅ | docs/SETUP.md |
| `healthwatch.sh` | внешний watchdog: N подряд /livez fail → restart → пост в служебную группу | ✅ | install-healthwatch.sh, docs/SETUP.md |
| `install-healthwatch.sh` | однократная установка healthwatch в crontab | ✅ | docs/SETUP.md |
| `install-systemd-autostart.sh` | однократная установка systemd-юнита `aemr-bot.service` для автозапуска бота после reboot | ✅ | docs/SYSADMIN.md §12a, infra/aemr-bot.service |
| `audit_vps.sh` | технический отчёт по VPS без вывода секретов | ✅ | docs/SYSADMIN.md |
| `make_repo_index.py` | генератор `aemr-bot-index.md` | ✅ | repo-index.yml, REPO_INDEX.md |
| `generate_privacy_pdf.py` | docs/Политика.md → docs/PRIVACY.pdf через reportlab | ✅ | services/policy.py имя файла |
| `extract_reglament.py` | извлечение текста Регламента из `.docx` в machine-readable markdown `docs/_extracted/REGLAMENT_v7_FULL.md` | ✅ | docs/_extracted/, docs/README |
| `build_geo_database.py` | OSM overpass → seed/geo/*.geojson (10 поселений ЕМО) | ✅ | seed/geo, services/geo.py |
| `verify_geo.py` | проверка геоданных против Wikidata + point-in-polygon | ✅ | build_geo_database.py |
| `cross_verify_geo.py` | расширенная cross-verification (5 свойств) | ✅ | verify_geo.py |
| `reset_test_data.sql` | wipe данных кроме operators+settings для test-восстановления | ✅ | docs/BACKUP_RESTORE_TEST.md |

---

## `seed/`

| Файл | Назначение | Статус | Зависят |
|---|---|---|---|
| `welcome.md` | дефолтный приветственный текст (грузится в settings при пустой БД) | ✅ | services/settings_store.py:_read_seed_text |
| `consent.md` | дефолтный текст согласия ПДн | ✅ | services/settings_store.py:_read_seed_text |
| `contacts.json` | экстренные службы для меню жителя | ✅ | settings_store.py |
| `topics.json` | список тематик обращения (Дороги/Мусор/...) | ✅ | settings_store.py |
| `transport_dispatchers.json` | контакты транспортных диспетчерских | ✅ | settings_store.py |
| `holidays.json` | производственный календарь РФ 2026-2027 | ✅ | services/calendar_ru.py |
| `geo/buildings.geojson` | здания с addr:housenumber из OSM | ✅ | services/geo.py |
| `geo/localities.geojson` | полигоны 10 поселений ЕМО | ✅ | services/geo.py |
| `geo/streets.geojson` | линии улиц | ✅ | services/geo.py |

---

## `docs/` корень

| Файл | Назначение | Статус | Зависят |
|---|---|---|---|
| `README.md` | навигация по docs/ | ✅ | docs/README.md ссылается на все остальные |
| `BACKUP_RESTORE_TEST.md` | процедура restore-test pg_dump на отдельной БД | ✅ | docs/README, RUNBOOK |
| `COPY.md` | все тексты бота для редакторского аудита | ✅ | docs/README |
| `DEPS.md` | uv.lock дисциплина, drift diagnosis | ✅ | docs/README, CI |
| `DEVELOPER.md` | архитектура+DB+миграции+maxapi+тесты — для разработчика | ✅ | docs/README |
| `HOW_IT_WORKS.md` | простое описание всех механизмов для оператора/новичка | ✅ | docs/README |
| `PRD.md` | продуктовые требования и приёмочные критерии | ✅ | docs/README |
| `RULES.md` | правила пользования ботом для жителей (источник: texts.RULES_TEXT) | ✅ | docs/README |
| `RUNBOOK.md` | ежедневный справочник оператора+ИТ | ✅ | docs/README, ROLLBACK |
| `RUNBOOK_PDN_ERASURE.md` | актуальный регламент `/erase` и `/forget` (override части RUNBOOK) | ✅ | docs/README, SECURITY |
| `SECURITY.md` | модель угроз + контролы | ✅ | docs/README |
| `SETUP.md` | пошаговая установка с нуля | ✅ | docs/README |
| `SYSADMIN.md` | операционное руководство сисадмина | ✅ | docs/README |
| `VPS_SMOKE_CHECKLIST.md` | smoke-checklist после деплоя | ✅ | docs/README |
| `ROLLBACK.md` | откат бота при сбое (≤10 мин) | ✅ | docs/README |
| `COMPLIANCE_WITH_REGLAMENT_v7.md` | верхнеуровневая матрица соответствия кода утверждённому Регламенту v7 (с дельтой и комментариями) | ✅ | docs/README, Регламент_v8_draft, _meta/REGLAMENT_v7_COMPLIANCE |
| `Регламент_v8_draft.md` | проект Регламента v8 — дельта v7 → v8 с подписями процедур ИТ-обращений, SEC/SACRED инвариантов, доп. cron'ов, шаблонов рассылок | ✅ | автор продукта, юрист АЕМО, docs/README |
| `Регламент.docx` | утверждённый Регламент v7 как `.docx` (источник истины compliance-матрицы) | ⚪ | docs/README, COMPLIANCE_WITH_REGLAMENT_v7 — правовой документ |
| `PRIVACY.pdf` | PDF политики, который бот раздаёт жителям | ✅ | infra/Dockerfile COPY, services/policy.py |
| `Политика.md` | актуальный исходник политики ПДн (источник для generate_privacy_pdf.py) | ✅ | scripts/generate_privacy_pdf.py, docs/README |
| `Политика_v2.md` | расширенная редакция для юр.экспертизы; технические факты сверены повторно на 2026-05-25 | 🟡 | docs/README, SECURITY; статус «ждёт юриста» |
| `MAXAPI_UPGRADE_PROCEDURE.md` | пошаговая процедура обновления `maxapi` | ✅ | docs/README, DEPS |

---

## `docs/_meta/`

| Файл | Назначение | Статус | Зависят |
|---|---|---|---|
| `AUDIT_REPORT.md` | разовый аудит документов против кода (сводка расхождений → правок) | ⚪ | автор-разработчик; разовый отчёт |
| `FILE_INVENTORY.md` | этот документ — реестр всех файлов | ✅ | автор-разработчик |
| `REGLAMENT_v7_COMPLIANCE.md` | детальная build-time матрица соответствия кода Регламенту v7 (98 пунктов с file:line ссылками) | ✅ | docs/README, COMPLIANCE_WITH_REGLAMENT_v7 |
| `REGLAMENT_v7_GAPS.md` | двусторонний разрыв «есть в Регламенте, нет в коде» / «есть в коде, нет в Регламенте» — источник правок для v8 | ✅ | Регламент_v8_draft, COMPLIANCE_WITH_REGLAMENT_v7 |
| `ADMIN_MENU_EXPANSION_PROPOSAL.md` | проект расширения админ-меню (UX-предложение) | ⚪ | автор-разработчик |
| `COVERAGE_GAPS.md` | пробелы в тестовом покрытии с приоритетами P0/P1/P2 | ✅ | docs/README, разработчик |
| `MAXAPI_INVENTORY.md` | реестр публичных API `maxapi`, фактически используемых в коде | ✅ | docs/README, разработчик |
| `MAXAPI_INSIGHTS.md` | разовые выводы по `maxapi` (best practices, gotchas) | ⚪ | разработчик |
| `MAXAPI_UNUSED_FEATURES.md` | возможности `maxapi`, не задействованные в коде (бэклог) | ⚪ | docs/README, разработчик |

---

## `docs/archive/`

Все 9 файлов — `⚪` исторический архив. Заявлены как «не для эксплуатации» в `docs/README.md`. Ни один не ссылается из актуальных docs (только `aemr-bot-index.md` индексирует).

| Файл | Назначение | Статус |
|---|---|---|
| `CHAT_AUDIT.md` | аудит истории работы над ботом (audit-extract логов) | ⚪ |
| `COMPETITIVE_BRIEF.md` | сравнение с госботами РФ (2026-05-09) | ⚪ |
| `COMPETITIVE_DEEP_DIVE.md` | расширенный конкурентный анализ (2026-05-10) | ⚪ |
| `COPY_AUDIT.md` | аудит текстов бота (закрыт) | ⚪ |
| `CRON_REFACTOR_PLAN.md` | план рефакторинга cron.py (реализован) | ⚪ |
| `DOC_AUDIT.md` | аудит docs (закрыт) | ⚪ |
| `IDEAS.md` | идеи на брейншторм P3 | ⚪ |
| `TELEGRAM_ANALYTICS_INSIGHTS.md` | выводы из анализа 60K сообщений Telegram-чата АЕМР | ⚪ |
| `WEBHOOK_PLAN.md` | план перехода на webhook через Caddy `dash` | ⚪ |

---

## Выполнено в зачистке 2026-05-25

### 🔴 Удалены через `git rm`

1. **`_local-backup/PRODUCT_BRIEF_internal.md`** — закоммичен ДО появления правила в `.gitignore`. Противоречил собственному ignore-pattern, никто не ссылался. Сам бриф (имя бота, логотип, оценка стоимости от 2026-05-07) остался локально в `_local-backup/`, который игнорится.
2. **`docs/handover.html`** — 57 632 байта HTML, дублировал `SYSADMIN.md` / `RUNBOOK.md` / `SETUP.md`. Никаких живых ссылок.
3. **`docs/it.html`** — 111 283 байта HTML, дубликат `RUNBOOK.md` для ИТ. Никаких живых ссылок.
4. **`docs/PRIVACY_DRAFT.md`** — первая редакция от 2026-05-09. Преемник — `docs/Политика_v2.md`. Ссылки в `docs/README.md`, `docs/SECURITY.md`, `docs/Политика_v2.md` обновлены на актуальные документы. Упоминания в `docs/archive/CHAT_AUDIT.md` и `docs/archive/DOC_AUDIT.md` сохранены как часть исторического архива.

### 🟢 Закреплены как оставленные намеренно

- **`docs/Регламент.docx`** — утверждённый Регламент v7 (правовой документ). Машиночитаемое извлечение поддерживается в `docs/_extracted/REGLAMENT_v7_FULL.md`, ссылки из `docs/README.md` и `docs/COMPLIANCE_WITH_REGLAMENT_v7.md` добавлены.

### ⚪ Кандидаты на удаление, которые НЕЛЬЗЯ удалять

- Все `docs/archive/*` (9 файлов) — заявлены как исторический архив в `docs/README.md` §«Архив». Compliance-нагрузки нет, но они описывают «откуда взялись» сегодняшние решения. Оставить.
- `docs/_meta/AUDIT_REPORT.md` — одноразовый отчёт о синхронизации docs ↔ код. Полезен как доказательство аудита 2026-05.

---

## Кандидаты на ОБНОВЛЕНИЕ

### P0 — критично

1. **`docs/Политика_v2.md`** — заголовок и статус обновлены 2026-05-25 (технические факты повторно сверены после PR SACRED/SEC #1-9, PR #74-77, новых отклонений нет). Содержательно остаётся «ждёт юриста». Если в коде появятся новые поля ПДн или поменяется retention — обновить разделы 2 и 7 ещё раз.

2. **`docs/PRD.md`** — нужна сверка Ф-5/ИК-1/приёмочных критериев после DDD pivot (event-log карточки) и серий SACRED/SEC фиксов. Сделано частично в этой итерации; остатки — пройти все Ф-N по списку и сверить с фактом.

### P1 — высокий приоритет

3. **`docs/HOW_IT_WORKS.md`** — раздел про admin card должен описывать новый event-log поток (миграция 0017 `last_admin_card_mid` + DDD pivot, freshness-rule учитывает сообщения жителя в личке и оператора в группе).

4. **`docs/RUNBOOK.md`** — сверить команды `/op_help`, лимиты, cron-jobs (broadcast_templates, SEC-фиксы 1-9, SACRED 1-6, новая систd-юнит для автозапуска).

5. **`docs/COPY.md`** — последняя проверка 2026-05-14. С тех пор тексты в `texts.py` и `card_format.py` сдвинулись (intermediate-reply, followup → admin-card, новые admin-events уведомления).

6. **`docs/COMPLIANCE_WITH_REGLAMENT_v7.md`** — переименован 2026-05-25. После утверждения v8 повторить процедуру (`_v8.md`).

### P2 — фоновое

7. **`docs/_meta/AUDIT_REPORT.md`** — одноразовый отчёт, помечен датой ревизии «текущий снимок». Не обновлять, оставить как исторический. Альтернатива — переместить в `docs/_meta/audits/2026-05/`.

8. **`aemr-bot-index.md`** — авто-генерируется. Обновится сам после следующего push в main. Раздувает diff'ы (55K строк), но это сознательно.

9. **`docs/SECURITY.md`** — добавить упоминания всех SEC-фиксов (SEC #1..#9) и SACRED #1..#6, которые закрыли в последние недели — сейчас документ их не перечисляет.

10. **`docs/RUNBOOK_PDN_ERASURE.md`** — обновлён 2026-05-25: добавлена секция «Команды и автоматизированный канал» с тремя `audit_log.action` (`erase` / `self_erase` / `auto_erase_pdn_retention`) и блок «Автозакрытие открытых обращений при ручном `/erase`» (P1 #21).

---

## Заметки

- Тесты покрывают ВСЕ handlers/* (62 теста на 22 handler-файла). Явных пропусков целых модулей нет.
- Все 17 миграций нужны — линейная цепочка, удалить любую = сломать `alembic upgrade head` в проде.
- Все 9 seed-файлов используются (читаются `services/settings_store.py` или `services/calendar_ru.py` / `services/geo.py`).
- `infra/.env` в `.gitignore`, не в `git ls-files` — корректно. На репо влияет только `.env.example`.
- `aemr-bot.egg-info/` (в `bot/`) — артефакт `pip install -e`, в `.gitignore`. Не трогать.
- `infra/aemr-bot.service` + `scripts/install-systemd-autostart.sh` — новая страховка для автозапуска бота после reboot VPS (см. SYSADMIN.md §12a).
- `docs/_meta/REGLAMENT_v7_GAPS.md` + `docs/Регламент_v8_draft.md` — пара документов для подготовки следующей версии Регламента: gap-список → нормативные правки.
