# Передача проекта системному администратору

Дата подготовки: 2026-05-04. HEAD: `df497da` (CI зелёный).

Этот документ — единая точка входа для админа, который принимает aemr-bot перед запуском в production. Объединяет findings из восьми параллельных аудит-проходов: deploy-checklist, RUNBOOK-review, risk-register, 152-FZ compliance, postgres-best-practices, kaizen, tech-debt register, performance, code-review-swarm, /security-review, /review.

## 1. Состояние проекта на момент передачи

- Все коммиты на `origin/main`, последний CI-прогон зелёный.
- Pre-launch findings (HIGH + MEDIUM из двух независимых ревью) закрыты — см. историю коммитов `2204970…df497da`.
- Уязвимостей с confidence ≥ 8 не найдено (см. /security-review pass).
- /review verdict — **Approve with nits** (см. §4 ниже, что осталось как nit).

## 2. Что обязательно сделать перед go-live

Это hard-gate. Без этих пунктов запуск рискованный.

1. **Заполнить `BACKUP_GPG_PASSPHRASE`** в `.env`. Без него локальные дампы лежат plain SQL — содержат имена, телефоны, тексты обращений. Хранить пароль в **двух независимых safe-местах** (1Password/KeePass администратора + offline-копия у руководителя).
2. **Прогнать restore-drill** на тестовом стенде по [RUNBOOK.md §7](RUNBOOK.md). Бэкап, который не пробовали восстанавливать, — не бэкап.
3. **Удостовериться, что внутренний `selfcheck`-крон жив** — он шлёт алерт в админ-группу, если бот перестал отвечать. Проверка: `docker compose restart bot` → дождаться `HEALTHCHECK_STALE_SECONDS` → в админ-группе должно прийти «⚠️ Бот не отвечает…», после восстановления — «✅ Бот восстановил отзывчивость». Это и есть основной мониторинг для self-host'а — внешние пингеры не нужны, наружу `/healthz` не публикуется.
4. **Юристу АЕМР проверить статус оператора ПДн** в реестре Роскомнадзора по ИНН (pd.rkn.gov.ru). Если нет — подать уведомление с указанием новой ИСПДн «Чат-бот обратной связи в MAX». См. §3.
5. **Сменить `BOT_TOKEN`** на production-токен (создаётся через `max.ru/business`, не через тестового бота).
6. **Проверить, что сервер физически в РФ** (152-FZ ст. 18.5). Поскольку self-host — это значит, что серверная стойка АЕМР находится в РФ.

## 3. Compliance 152-FZ / УЗ-4 — что закрыто, что нет

Полный отчёт: см. файл [compliance-audit-report.md](#) (генерируется агентом, держим в этой сессии).

**Закрыто в коде:**
- ✅ Согласие на обработку (ст. 9) — `users.consent_pdn_at`, отзыв через `/forget`.
- ✅ Право на удаление (ст. 21) — `/forget` для жителя, `/erase max_user_id=` или `/erase phone=` для ИТ-оператора. Анонимизация затрагивает `first_name`, `phone`, `phone_normalized`, `consent_pdn_at`, `dialog_state`, `dialog_data`, `subscribed_broadcast`, `is_blocked` — всё в одной транзакции с записью в `audit_log`.
- ✅ Меры защиты (ст. 19, ПП №1119) — роли operators, audit_log, gpg-AES256 backup, ACID Postgres, идентификация по `BOT_TOKEN` + MAX `user_id`.
- ✅ Хранение в РФ (ст. 18.5) — задокументировано в ADR-001 §1.
- ✅ Минимизация (ст. 5) — собираем только нужное (имя без фамилии, телефон, адрес, тематика, текст).
- ✅ events таблица — ретенция 30 дней автоматическая.

**Не закрыто, owner — юрист АЕМР + разработчик:**

- ❌ Право на доступ (ст. 14) — `/whoami` показывает только `max_user_id`/`first_name`/`chat_id`. Полной выгрузки «всё, что бот хранит про меня» нет. Нужна команда `/mydata`. Tech debt **TD-08, P0**.
- ❌ Право на исправление (ст. 14, 21) — нет `/setname`, `/setphone` для жителя. Tech debt **TD-09, P0**.
- ⚠️ Уведомление о цели обработки (ст. 18) — `PRIVACY.md` упоминает реагирование на сообщения, но не покрывает рассылки, статистику, передачу третьим лицам (MAX). **Нужна редакция юристом** до go-live.
- ⚠️ Срок хранения (ст. 5) — `appeals`, `messages`, `audit_log` бессрочно. После закрытия обращения цель исчерпана. Нужна политика анонимизации после N лет (предлагается 5). Tech debt **TD-10, P1**.
- ⚠️ Уведомление Роскомнадзора (ст. 22) — вне кода. Юрист АЕМР проверяет статус и подаёт дополнение.

## 4. Что осталось как «known limitations» (формальный tech debt)

Полный реестр — в [DEVELOPER.md «Известные ограничения»](DEVELOPER.md). Сводка по приоритетам:

**P0 — первый месяц после запуска:**
- TD-08: команда `/mydata` для жителя (152-FZ ст. 14).
- TD-09: команда `/setname` (152-FZ ст. 14, 21).
- TD-11: `pip-audit` / `safety` в CI (CVE в зависимостях).

**P1 — первый квартал:**
- TD-10: APScheduler-job анонимизации `appeals` старше 5 лет.
- TD-12: regress-тесты на закрытые findings (erase invariants, healthz DB-ping, backup alert).
- Расширение `_diag`: один `GROUP BY` вместо 8 отдельных scalars.

**P2 — первый год:**
- TD-01: батчинг `record_delivery` в broadcast (триггер: >1000 подписчиков).
- TD-02: dict-dispatcher для `on_callback` (триггер: 5+ новых callback-префиксов).
- TD-03: декомпозиция `_run_broadcast_impl`.
- recover_stuck_funnels — заменить `asyncio.gather` на `Semaphore(2)` или последовательный цикл с throttle (предотвращает рейт-лимит-всплеск при крупном recovery).
- Graceful shutdown: `engine.dispose()` + cancel `_collect_timers` при SIGTERM (сейчас kill -9 теряет коннекты).
- Prometheus-endpoint для метрик «обращений в минуту», p95 send_message, backlog рассылок.

**P3 — по триггеру:**
- TD-04: anonymous `_(event)` handlers → именованные функции.
- TD-05: `_user_locks` → Postgres advisory locks (триггер: переход на multi-replica).
- TD-06: PRIVACY.pdf reupload на холодный старт.
- TD-07: inline RU-строки в `admin_commands.py` → `texts.py`.

## 5. Operability minimum

- **Логи.** `docker compose logs --tail=200 -f bot`. Структурированный логгинг и trace-id не настроены (P2). Для одиночного инстанса MVP grep работает.
- **Healthcheck.** Compose `healthcheck` curls `/healthz` каждые 30s; сам `/healthz` теперь делает `SELECT 1` к БД (с 10-секундным cache, чтобы не задавить пул). Если БД зависает — endpoint отдаёт 503.
- **APScheduler-задачи** при старте: `db-backup` (вс 03:00), `events-retention` (ежедневно 04:00), `health-selfcheck` (каждые `HEALTHCHECK_INTERVAL_MIN`), `monthly-stats` (1-го в 09:00). Verify через `docker compose logs bot | grep "added job"`.
- **Алерты в админ-группе:**
  - bot heartbeat stale → «⚠️ Бот не отвечает на проверку здоровья…»
  - бэкап упал → «⚠️ Еженедельный бэкап БД не выполнен…»

## 6. Postgres мониторинг — команды для админа

10 копи-пастабельных команд лежат в [DEVELOPER.md] (раздел добавляется отдельно). Минимум-минимум:

```bash
# Размер таблиц + индексов
docker compose exec db psql -U aemr -c "SELECT schemaname||'.'||relname AS table, pg_size_pretty(pg_total_relation_size(relid)) AS total FROM pg_catalog.pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;"

# Bloat / dead tuples
docker compose exec db psql -U aemr -c "SELECT relname, n_live_tup, n_dead_tup, last_autovacuum FROM pg_stat_user_tables ORDER BY n_dead_tup DESC LIMIT 10;"

# Активные запросы
docker compose exec db psql -U aemr -c "SELECT pid, state, wait_event, query_start, left(query,80) FROM pg_stat_activity WHERE state != 'idle';"

# Версия миграций (должно быть 0004 после деплоя)
docker compose exec bot alembic current
```

## 7. Risk register — топ-5

Полный реестр на 12 пунктов — в файле [risk-register.md](#) (генерируется в этой сессии). Топ-5 по urgency:

| ID | Риск | Митигация | Что делать админу |
|---|---|---|---|
| R-01 | Утечка ПДн через незашифрованный backup-файл | gpg AES-256 опт-ин | Заполнить `BACKUP_GPG_PASSPHRASE` до go-live |
| R-04 | Бот молчит (контейнер упал, OOM, зависший event-loop) | `/healthz` + heartbeat + healthcheck-block + restart policy | Внешний uptime-monitor с алертом в SMS |
| R-09 | Эксплуатационная ошибка оператора (`/erase` не туда, `/broadcast` с опечаткой) | Двухшаговый wizard, валидация JSON, audit_log | RUNBOOK §1 + первый месяц — все опасные команды через ИТ-специалиста |
| R-10 | Утрачен `BACKUP_GPG_PASSPHRASE` — все шифрованные дампы бесполезны | Хранить в двух independent safe-местах | Записать в две точки до go-live и проверить раз в полгода |
| R-03 | Случайный DROP / потеря диска / битая миграция | Еженедельный gpg-encrypted дамп, ротация 8 файлов | Restore-drill **обязателен до go-live** |

## 8. Smoke-test checklist (golden path)

Из deploy-checklist:

- [ ] Citizen `/start` → меню (5 кнопок).
- [ ] Полная воронка: имя → телефон → адрес → тема → текст → вложение → карточка в админ-группу.
- [ ] Свайп-reply оператора → формальное письмо жителю.
- [ ] `/reply N <текст>` альтернативный путь.
- [ ] `/broadcast` end-to-end + cancel mid-flight.
- [ ] `/stats today | week | month` → XLSX.
- [ ] `/erase phone=+7XXX` → анонимизация + audit_log.
- [ ] `/diag` → ожидаемые счётчики.
- [ ] `/backup` → дамп в `/backups/` с `chmod 0600`, шифрован если passphrase задан.

Acceptance: все ✓ + 24 часа без падений на тестовом стенде.

## 9. Что приложить к передаче

- **Этот документ** (`docs/HANDOFF.md`).
- **[COMMANDS.md](COMMANDS.md)** — единая шпаргалка всех CLI-команд для админа (генерация секретов, установка, бэкап и восстановление с расшифровкой gpg, миграции, мониторинг Postgres, аварийные процедуры, smoke-test).
- [README.md](../README.md), [SETUP.md](SETUP.md), [RUNBOOK.md](RUNBOOK.md), [DEVELOPER.md](DEVELOPER.md), [ADR-001-architecture.md](ADR-001-architecture.md), [PRD-mvp.md](PRD-mvp.md), [db-schema.md](db-schema.md), [architecture-diagrams.md](architecture-diagrams.md).
- Доступ к приватному репозиторию `github.com/gaben1488/aemr-bot`.
- Контакты эскалации: ИТ-координатор (имя, телефон, email), разработчик (имя, контакт).
- Учётка с правами на VPS, `BOT_TOKEN`, `BACKUP_GPG_PASSPHRASE` — переданы через защищённый канал, не email.

## 10. Финальный verdict

**Готов к передаче.** Все pre-launch findings закрыты, /security-review без находок ≥8 confidence, /review одобрил с nits (учтены либо выписаны в tech-debt). Открытые пункты по 152-FZ (TD-08, TD-09, регламент удаления через 5 лет) — не блокирующие запуск, но **должны быть взяты в работу в первый месяц** под надзором юриста АЕМР.

Запускайте по чек-листу из [SETUP.md](SETUP.md) после прохождения §2 этого документа.
