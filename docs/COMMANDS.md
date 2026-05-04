# Шпаргалка команд администратора

Один файл со всеми командами, которые нужны для эксплуатации aemr-bot. Скопировать, выполнить, не вспоминать. Все команды рассчитаны на self-host: бот и Postgres крутятся в `docker compose` на одном сервере под Linux.

Команды сгруппированы по сценариям. Внутри сценария — порядок выполнения сверху вниз.

> **Соглашения.**
> - `~/aemr-bot` — корень репозитория на сервере (поменяй, если у тебя другой путь).
> - Все `docker compose` команды выполняются из `~/aemr-bot/infra/`.
> - `aemr` — имя БД и пользователя (значение `POSTGRES_USER` / `POSTGRES_DB`).
> - Если в команде встречается `<...>` — это плейсхолдер, замени на реальное значение.

## Содержание

1. [Генерация секретов перед первым деплоем](#1-генерация-секретов-перед-первым-деплоем)
2. [Первичная установка](#2-первичная-установка)
3. [Ежедневная эксплуатация](#3-ежедневная-эксплуатация)
4. [Логи и диагностика](#4-логи-и-диагностика)
5. [Резервное копирование](#5-резервное-копирование)
6. [Восстановление из бэкапа](#6-восстановление-из-бэкапа)
7. [Миграции БД](#7-миграции-бд)
8. [Postgres мониторинг](#8-postgres-мониторинг)
9. [Регистрация и управление операторами](#9-регистрация-и-управление-операторами)
10. [Аварийные процедуры](#10-аварийные-процедуры)
11. [Smoke-test после изменений](#11-smoke-test-после-изменений)

---

## 1. Генерация секретов перед первым деплоем

Все три значения генерируются **на твоей рабочей машине** (не на сервере), записываются в менеджер паролей и потом подставляются в `.env` на сервере.

```bash
# POSTGRES_PASSWORD — пароль пользователя aemr в Postgres-контейнере
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# BACKUP_GPG_PASSPHRASE — passphrase для gpg-AES256 шифрования pg_dump
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# WEBHOOK_SECRET — нужен только если включаешь webhook-режим (на self-host MVP не нужен)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

`BOT_TOKEN` не генерируется — берётся на портале <https://max.ru/business> → раздел «Боты» → создать или открыть бота → скопировать Bot API token.

**Правило:** ровно те же значения `POSTGRES_PASSWORD` подставляются в обе строки `.env`:

```ini
POSTGRES_PASSWORD=tQ_Y3w0c8KkS0lN8...x9xp
DATABASE_URL=postgresql+asyncpg://aemr:tQ_Y3w0c8KkS0lN8...x9xp@db:5432/aemr
```

Если расходятся — бот при старте падает с `password authentication failed`.

`BACKUP_GPG_PASSPHRASE` обязательно сохрани в **двух независимых местах** (менеджер паролей администратора + offline-копия у руководителя). Утрата passphrase = все накопленные `.sql.gpg` бесполезный шифротекст.

## 2. Первичная установка

```bash
# На сервере под пользователем aemr (не root), с уже установленным docker
git clone https://github.com/gaben1488/aemr-bot.git ~/aemr-bot
cd ~/aemr-bot/infra

# Подготовить .env
cp .env.example .env
chmod 600 .env

# Открыть в редакторе и заполнить минимум:
#   BOT_TOKEN, POSTGRES_PASSWORD, DATABASE_URL, BACKUP_GPG_PASSPHRASE, TZ
nano .env

# Собрать и поднять
docker compose build
docker compose up -d

# Проверить, что контейнеры здоровы (через ~60 секунд)
docker compose ps
# Ожидание: оба сервиса в статусе "running (healthy)"

# Проверить /healthz изнутри сервера
curl -fsS http://127.0.0.1:8080/healthz | python3 -m json.tool
# Ожидание: HTTP 200, JSON с "ok": true, "db_ok": true

# Проверить миграции
docker compose exec bot alembic current
# Ожидание: 0004 (или новее) — последняя ревизия из bot/aemr_bot/db/alembic/versions/
```

После этого нужно настроить `ADMIN_GROUP_ID` и операторов — см. [SETUP.md](SETUP.md) §3–§6, либо коротко [§9](#9-регистрация-и-управление-операторами) этого файла.

## 3. Ежедневная эксплуатация

```bash
cd ~/aemr-bot/infra

# Старт всего стека
docker compose up -d

# Стоп
docker compose stop

# Полный рестарт бота (например, после смены .env)
docker compose up -d --force-recreate bot

# Применить новый код после git pull
git pull
docker compose up -d --build bot

# Применить миграции после pull (обычно срабатывают сами в CMD контейнера)
docker compose exec bot alembic upgrade head
```

## 4. Логи и диагностика

```bash
# Хвост логов бота в реальном времени
docker compose logs -f --tail=200 bot

# Только ошибки за последние 24 часа
docker compose logs --since 24h bot | grep -iE "error|exception|warning"

# Логи Postgres
docker compose logs --tail=200 db

# Состояние контейнеров и ресурсов
docker compose ps
docker stats --no-stream

# Быстрая проверка, что бот отвечает
curl -fsS http://127.0.0.1:8080/healthz | python3 -m json.tool
```

В админ-группе MAX в любой момент:
- `/diag` — счётчики жителей, обращений, рассылок, событий + конфиг.
- `/op_help` — закрепляемая панель быстрых действий.

## 5. Резервное копирование

```bash
# Список существующих бэкапов в named-volume
docker compose exec bot ls -lh /backups/

# Снять бэкап вручную (любой момент, доступно роли it в админ-группе через /backup,
# но и из shell тоже можно)
docker compose exec bot python -c "
import asyncio
from aemr_bot.services.cron import _backup_db
print(asyncio.run(_backup_db()))
"
# Ожидание: путь к новому файлу .sql.gpg (или .sql если passphrase пустой)

# Скопировать конкретный бэкап на хост (вне контейнера) для офлайн-хранения
docker compose cp bot:/backups/aemr-20260504_030000.sql.gpg ~/backups/

# Размер named-volume
docker volume inspect infra_backups --format '{{ .Mountpoint }}' \
  | xargs -I {} du -sh {}
```

Расписание автоматических бэкапов — каждое воскресенье в 03:00 (`BACKUP_DAY_OF_WEEK`, `BACKUP_HOUR`, `BACKUP_MINUTE` в `.env`). Ротация: последние 8 файлов (`BACKUP_KEEP_LAST`).

## 6. Восстановление из бэкапа

Это та самая процедура, которую **обязательно** прогнать на тестовом стенде до go-live.

### 6.1. Расшифровать gpg-бэкап

```bash
# Если файл лежит в /backups внутри контейнера — расшифровать на месте
docker compose exec -e GPG_PASSPHRASE="$(grep ^BACKUP_GPG_PASSPHRASE= .env | cut -d= -f2-)" \
  bot sh -c '
    gpg --batch --passphrase "$GPG_PASSPHRASE" \
        --decrypt /backups/aemr-20260504_030000.sql.gpg \
        > /tmp/aemr-restore.sql
    ls -lh /tmp/aemr-restore.sql
  '

# Проверка целостности — первая строка должна содержать pg_dump-сигнатуру
docker compose exec bot head -3 /tmp/aemr-restore.sql
# Ожидание: -- PostgreSQL database dump
```

### 6.2. Восстановить в чистую БД (drill на тестовом стенде)

```bash
# Создать одноразовую тестовую БД, не трогая prod-aemr
docker compose exec db createdb -U aemr aemr_restore_test

# Залить в неё расшифрованный дамп
docker compose exec bot sh -c \
  'cat /tmp/aemr-restore.sql | psql -h db -U aemr -d aemr_restore_test'

# Sanity check — должны вернуться ненулевые цифры
docker compose exec db psql -U aemr -d aemr_restore_test -c \
  "SELECT count(*) AS users FROM users; SELECT count(*) AS appeals FROM appeals;"

# Удалить тестовую БД после проверки
docker compose exec db dropdb -U aemr aemr_restore_test

# Удалить расшифрованный SQL — он содержит ПДн в открытом виде
docker compose exec bot rm /tmp/aemr-restore.sql
```

### 6.3. Полное восстановление прода (после катастрофы)

```bash
# 1. Остановить бот, чтобы новые записи не мешали
cd ~/aemr-bot/infra
docker compose stop bot

# 2. Уронить и пересоздать БД
docker compose exec db dropdb -U aemr aemr
docker compose exec db createdb -U aemr aemr

# 3. Залить дамп
docker compose exec -e GPG_PASSPHRASE="$(grep ^BACKUP_GPG_PASSPHRASE= .env | cut -d= -f2-)" \
  bot sh -c '
    gpg --batch --passphrase "$GPG_PASSPHRASE" \
        --decrypt /backups/aemr-<выбранный_файл>.sql.gpg \
      | psql -h db -U aemr -d aemr
  '

# 4. Поднять бот — Alembic не нужен, миграции уже в дампе
docker compose start bot

# 5. Проверить, что бот завёлся
docker compose logs --tail=50 bot
curl -fsS http://127.0.0.1:8080/healthz
```

**Если бэкап без gpg** (passphrase в `.env` был пустой) — пропустить шаг с `gpg --decrypt`, вместо `gpg ... | psql` использовать `cat /backups/aemr-...sql | psql ...`.

## 7. Миграции БД

```bash
# Текущая ревизия в БД
docker compose exec bot alembic current

# Применить все pending-миграции (обычно бот делает это сам в CMD)
docker compose exec bot alembic upgrade head

# Откатить одну ревизию назад
docker compose exec bot alembic downgrade -1

# История ревизий
docker compose exec bot alembic history

# Сгенерировать новую миграцию из изменений в models.py (только для разработчика)
docker compose exec bot alembic revision --autogenerate -m "describe what changed"
```

После генерации **обязательно прочитать сгенерированный файл** — autogenerate иногда ошибается с типами JSONB и enum.

## 8. Postgres мониторинг

10 проверок, которые админ может прогнать раз в неделю-месяц.

```bash
# 1. Размер таблиц + индексов (топ-10)
docker compose exec db psql -U aemr -d aemr -c "
SELECT schemaname||'.'||relname AS table,
       pg_size_pretty(pg_total_relation_size(relid)) AS total,
       pg_size_pretty(pg_relation_size(relid)) AS heap
FROM pg_catalog.pg_statio_user_tables
ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;
"

# 2. Bloat / dead tuples
docker compose exec db psql -U aemr -d aemr -c "
SELECT relname, n_live_tup, n_dead_tup,
       round(100.0*n_dead_tup/NULLIF(n_live_tup,0),1) AS dead_pct,
       last_autovacuum
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC LIMIT 10;
"

# 3. Индексы, к которым никто не обращается (кандидаты на drop в будущем)
docker compose exec db psql -U aemr -d aemr -c "
SELECT relname, indexrelname, idx_scan
FROM pg_stat_user_indexes WHERE idx_scan = 0 ORDER BY relname;
"

# 4. Активные блокировки и долгие запросы
docker compose exec db psql -U aemr -d aemr -c "
SELECT pid, usename, state, wait_event_type, wait_event,
       query_start, left(query,80) AS q
FROM pg_stat_activity WHERE state != 'idle' ORDER BY query_start;
"

# 5. Долгие запросы (>5 секунд)
docker compose exec db psql -U aemr -d aemr -c "
SELECT pid, now()-query_start AS dur, left(query,120)
FROM pg_stat_activity
WHERE state='active' AND now()-query_start > interval '5 seconds';
"

# 6. Размер БД и количество WAL-файлов
docker compose exec db psql -U aemr -d aemr -c "
SELECT pg_size_pretty(pg_database_size('aemr')) AS db_size,
       (SELECT count(*) FROM pg_ls_waldir()) AS wal_files;
"

# 7. Принудительный VACUUM ANALYZE на горячих таблицах
docker compose exec db psql -U aemr -d aemr -c "
VACUUM (ANALYZE, VERBOSE) events;
VACUUM (ANALYZE, VERBOSE) broadcast_deliveries;
"

# 8. Проверка миграций
docker compose exec bot alembic current

# 9. Проверка целостности последнего бэкапа
docker compose exec bot sh -c 'ls -t /backups/aemr-*.gpg | head -1 | xargs -I {} stat -c "%n %s байт %y" {}'

# 10. Дисковое использование на сервере
df -h | grep -E "Filesystem|/$|docker"
```

## 9. Регистрация и управление операторами

Делается в основном через бота в админ-группе MAX, но shell-fallback есть.

```bash
# Посмотреть всех операторов
docker compose exec db psql -U aemr -d aemr -c \
  "SELECT id, max_user_id, role, full_name, active FROM operators ORDER BY id;"

# Деактивировать оператора (например, при увольнении) — мягкое удаление
docker compose exec db psql -U aemr -d aemr -c \
  "UPDATE operators SET active=false WHERE max_user_id=<их_id>;"

# Сменить роль оператора через psql (бот это сам не умеет — защита от self-promote)
docker compose exec db psql -U aemr -d aemr -c \
  "UPDATE operators SET role='it' WHERE max_user_id=<их_id>;"

# Аварийно вписать первого ИТ-оператора, если bootstrap не сработал
docker compose exec db psql -U aemr -d aemr -c \
  "INSERT INTO operators (max_user_id, full_name, role, active)
   VALUES (<id>, 'Иванов И.И.', 'it', true);"
```

В норме регистрация идёт через `/add_operators` в админ-группе — см. [RUNBOOK §2](RUNBOOK.md).

## 10. Аварийные процедуры

```bash
# Бот молчит, контейнер живой — простой рестарт
docker compose restart bot

# Бот молчит, контейнер мёртв
docker compose up -d bot

# БД недоступна
docker compose logs --tail=100 db
docker compose restart db
sleep 10
docker compose restart bot

# Откат на предыдущую версию (тег)
git fetch --tags
git log --oneline -5
git checkout <предыдущий_тег>
docker compose up -d --build bot

# Откат миграции после неудачного апгрейда
docker compose exec bot alembic downgrade -1

# Сброс тестовых данных перед прод-запуском (НЕ В ПРОДЕ!)
cat ../scripts/reset_test_data.sql | \
  docker compose exec -T db psql -U aemr -d aemr

# Полная пересборка контейнера (если что-то совсем сломано)
docker compose down
docker compose up -d --build
```

## 11. Smoke-test после изменений

Минимальный набор проверок после деплоя или рестарта.

```bash
# 1. Контейнеры здоровы
docker compose ps
# Ожидание: оба сервиса "running (healthy)"

# 2. Healthz отвечает
curl -fsS http://127.0.0.1:8080/healthz | python3 -m json.tool
# Ожидание: "ok": true, "db_ok": true

# 3. Бот аутентифицировался в MAX (видно в логах после старта)
docker compose logs --tail=50 bot | grep -iE "long polling|first_name|@"
# Ожидание: строка вида "Бот: @<имя_бота> first_name=... id=..."

# 4. APScheduler-задачи зарегистрированы
docker compose logs bot | grep -iE "scheduler|added job"
# Ожидание: 4 задачи — db-backup, events-retention, health-selfcheck, monthly-stats

# 5. Миграции на последней ревизии
docker compose exec bot alembic current

# 6. Из MAX от тестового жителя:
#    /start → главное меню (5 кнопок)
#    Воронка обращения → карточка прилетает в админ-группу
#    /reply <N> <текст> → житель получает ответ
#    /diag из админ-группы → ожидаемые счётчики
```

Если все 6 шагов прошли — деплой принят. Иначе — `docker compose logs bot` и в [RUNBOOK §5](RUNBOOK.md) (что делать, если бот молчит).
