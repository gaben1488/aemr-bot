# Шпаргалка команд администратора

Один файл со всеми командами для эксплуатации aemr-bot. Скопировал, выполнил, забыл. Все команды рассчитаны на размещение на собственном сервере (self-host): бот и Postgres (система управления базами данных PostgreSQL) работают в Docker Compose (оркестратор контейнеров) на одном Linux-сервере.

Команды разбиты по сценариям. Внутри сценария — порядок выполнения сверху вниз.

> **Соглашения.**
> - `~/aemr-bot` — корень репозитория на сервере. Подставь свой путь, если он другой.
> - Все команды `docker compose` запускаются из каталога `~/aemr-bot/infra/`.
> - `aemr` — имя базы данных и пользователя. Это значения переменных `POSTGRES_USER` и `POSTGRES_DB`.
> - Если в команде встречается `<...>` — это место для подстановки. Замени на реальное значение.

## Содержание

1. [Генерация секретов перед первым развёртыванием](#1-генерация-секретов-перед-первым-развёртыванием)
2. [Первичная установка](#2-первичная-установка)
3. [Ежедневная эксплуатация](#3-ежедневная-эксплуатация)
4. [Логи и диагностика](#4-логи-и-диагностика)
5. [Резервное копирование](#5-резервное-копирование)
6. [Восстановление из бэкапа](#6-восстановление-из-бэкапа)
7. [Миграции базы данных](#7-миграции-базы-данных)
8. [Мониторинг Postgres](#8-мониторинг-postgres)
9. [Регистрация и управление операторами](#9-регистрация-и-управление-операторами)
10. [Аварийные процедуры](#10-аварийные-процедуры)
11. [Проверочный прогон после изменений](#11-проверочный-прогон-после-изменений)

---

## 1. Генерация секретов перед первым развёртыванием

Все три значения генерируются **на твоей рабочей машине**, а не на сервере. Запиши их в менеджер паролей. Потом подставь в файл `.env` на сервере.

```bash
# POSTGRES_PASSWORD — пароль пользователя aemr в контейнере Postgres
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# BACKUP_GPG_PASSPHRASE — кодовая фраза для шифрования бэкапов через GPG-AES256.
# GPG — система симметричного шифрования. pg_dump — штатная утилита Postgres
# для выгрузки содержимого базы.
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# WEBHOOK_SECRET — нужен только при включении режима webhook.
# Для размещения на собственном сервере в варианте MVP не нужен.
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

`BOT_TOKEN` не генерируется. Его берут на портале <https://max.ru/business>. Раздел «Боты». Создать или открыть бота. Скопировать значение Bot API token.

**Правило:** одно и то же значение `POSTGRES_PASSWORD` подставляется в обе строки `.env`:

```ini
POSTGRES_PASSWORD=tQ_Y3w0c8KkS0lN8...x9xp
DATABASE_URL=postgresql+asyncpg://aemr:tQ_Y3w0c8KkS0lN8...x9xp@db:5432/aemr
```

Если значения расходятся, бот при старте падает с ошибкой `password authentication failed`.

`BACKUP_GPG_PASSPHRASE` сохрани в **двух независимых местах**: менеджер паролей администратора и оффлайн-копия у руководителя. Потеря этой кодовой фразы означает, что все накопленные файлы `.sql.gpg` превращаются в бесполезный шифротекст.

## 2. Первичная установка

```bash
# На сервере под пользователем aemr (не root). Docker должен быть уже установлен.
git clone https://github.com/gaben1488/aemr-bot.git ~/aemr-bot
cd ~/aemr-bot/infra

# Подготовить .env
cp .env.example .env
chmod 600 .env

# Открыть в редакторе и заполнить минимум:
#   BOT_TOKEN, POSTGRES_PASSWORD, DATABASE_URL, BACKUP_GPG_PASSPHRASE, TZ
nano .env

# Собрать и запустить
docker compose build
docker compose up -d

# Проверить, что контейнеры здоровы (через ~60 секунд)
docker compose ps
# Ожидание: оба сервиса в статусе "running (healthy)"

# Проверить /healthz изнутри сервера
curl -fsS http://127.0.0.1:8080/healthz | python3 -m json.tool
# Ожидание: HTTP 200, JSON с "ok": true, "db_ok": true

# Проверить миграции (изменения схемы базы данных с версионностью)
docker compose exec bot alembic current
# Ожидание: 0004 (или новее) — последняя ревизия из bot/aemr_bot/db/alembic/versions/
```

Дальше нужно настроить `ADMIN_GROUP_ID` и операторов. См. [SETUP.md](SETUP.md), §3–§6. Кратко — в [§9](#9-регистрация-и-управление-операторами) этого файла.

## 3. Ежедневная эксплуатация

```bash
cd ~/aemr-bot/infra

# Запуск всего стека
docker compose up -d

# Остановка
docker compose stop

# Полный перезапуск бота (например, после правки .env)
docker compose up -d --force-recreate bot

# Применить новый код после git pull
git pull
docker compose up -d --build bot

# Применить миграции после git pull. Бот делает это сам в стартовой команде контейнера.
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

# Состояние контейнеров и потребление ресурсов
docker compose ps
docker stats --no-stream

# Быстрая проверка, что бот отвечает
curl -fsS http://127.0.0.1:8080/healthz | python3 -m json.tool
```

В админ-группе MAX в любой момент:
- `/diag` — счётчики жителей, обращений, рассылок, событий и текущая конфигурация.
- `/op_help` — закрепляемая панель быстрых действий.

## 5. Резервное копирование

```bash
# Список существующих бэкапов в именованном томе (named volume) Docker
docker compose exec bot ls -lh /backups/

# Снять бэкап вручную в любой момент. Доступно роли it в админ-группе через /backup,
# но и из shell тоже работает.
docker compose exec bot python -c "
import asyncio
from aemr_bot.services.cron import _backup_db
print(asyncio.run(_backup_db()))
"
# Ожидание: путь к новому файлу .sql.gpg (или .sql, если кодовая фраза пуста)

# Скопировать конкретный бэкап на хост (вне контейнера) для оффлайн-хранения
docker compose cp bot:/backups/aemr-20260504_030000.sql.gpg ~/backups/

# Размер именованного тома
docker volume inspect infra_backups --format '{{ .Mountpoint }}' \
  | xargs -I {} du -sh {}
```

Расписание автоматических бэкапов: каждое воскресенье в 03:00. Параметры в `.env` — `BACKUP_DAY_OF_WEEK`, `BACKUP_HOUR`, `BACKUP_MINUTE`. Срок хранения: последние 8 файлов, параметр `BACKUP_KEEP_LAST`.

## 6. Восстановление из бэкапа

Это та самая процедура, которую **обязательно** надо прогнать на тестовом стенде до боевого запуска.

### 6.1. Расшифровать бэкап GPG

```bash
# Если файл лежит в /backups внутри контейнера — расшифровываем на месте.
docker compose exec -e GPG_PASSPHRASE="$(grep ^BACKUP_GPG_PASSPHRASE= .env | cut -d= -f2-)" \
  bot sh -c '
    gpg --batch --passphrase "$GPG_PASSPHRASE" \
        --decrypt /backups/aemr-20260504_030000.sql.gpg \
        > /tmp/aemr-restore.sql
    ls -lh /tmp/aemr-restore.sql
  '

# Проверка целостности. Первая строка должна содержать сигнатуру pg_dump.
docker compose exec bot head -3 /tmp/aemr-restore.sql
# Ожидание: -- PostgreSQL database dump
```

### 6.2. Восстановить в чистую БД (учебная отработка на тестовом стенде)

```bash
# Создать одноразовую тестовую базу. Боевую aemr не трогаем.
docker compose exec db createdb -U aemr aemr_restore_test

# Залить в неё расшифрованный дамп через psql — клиент командной строки PostgreSQL.
docker compose exec bot sh -c \
  'cat /tmp/aemr-restore.sql | psql -h db -U aemr -d aemr_restore_test'

# Проверка вменяемости. Должны вернуться ненулевые цифры.
docker compose exec db psql -U aemr -d aemr_restore_test -c \
  "SELECT count(*) AS users FROM users; SELECT count(*) AS appeals FROM appeals;"

# Удалить тестовую базу после проверки
docker compose exec db dropdb -U aemr aemr_restore_test

# Удалить расшифрованный SQL — он содержит персональные данные в открытом виде
docker compose exec bot rm /tmp/aemr-restore.sql
```

### 6.3. Полное восстановление продакшена после катастрофы

```bash
# 1. Остановить бота, чтобы новые записи не мешали
cd ~/aemr-bot/infra
docker compose stop bot

# 2. Удалить и пересоздать базу
docker compose exec db dropdb -U aemr aemr
docker compose exec db createdb -U aemr aemr

# 3. Залить дамп
docker compose exec -e GPG_PASSPHRASE="$(grep ^BACKUP_GPG_PASSPHRASE= .env | cut -d= -f2-)" \
  bot sh -c '
    gpg --batch --passphrase "$GPG_PASSPHRASE" \
        --decrypt /backups/aemr-<выбранный_файл>.sql.gpg \
      | psql -h db -U aemr -d aemr
  '

# 4. Запустить бота. Alembic (инструмент управления миграциями БД) запускать не надо —
#    миграции уже зашиты в дамп.
docker compose start bot

# 5. Проверить, что бот стартовал
docker compose logs --tail=50 bot
curl -fsS http://127.0.0.1:8080/healthz
```

**Если бэкап без GPG** (кодовая фраза в `.env` была пустой), пропусти шаг с `gpg --decrypt`. Вместо `gpg ... | psql` используй `cat /backups/aemr-...sql | psql ...`.

## 7. Миграции базы данных

```bash
# Текущая ревизия в базе
docker compose exec bot alembic current

# Применить все ожидающие миграции. Бот делает это сам в стартовой команде контейнера.
docker compose exec bot alembic upgrade head

# Откатить одну ревизию назад
docker compose exec bot alembic downgrade -1

# История ревизий
docker compose exec bot alembic history

# Сгенерировать новую миграцию из изменений в models.py — только для разработчика
docker compose exec bot alembic revision --autogenerate -m "describe what changed"
```

После генерации **обязательно прочитать сгенерированный файл**. Автогенерация ошибается на типах JSONB и enum.

## 8. Мониторинг Postgres

Десять проверок. Прогоняй их раз в неделю или раз в месяц.

```bash
# 1. Размер таблиц с индексами (топ-10)
docker compose exec db psql -U aemr -d aemr -c "
SELECT schemaname||'.'||relname AS table,
       pg_size_pretty(pg_total_relation_size(relid)) AS total,
       pg_size_pretty(pg_relation_size(relid)) AS heap
FROM pg_catalog.pg_statio_user_tables
ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;
"

# 2. Раздувание таблиц и устаревшие строки (bloat и dead tuples).
#    Колонка last_autovacuum показывает время последней автоматической очистки.
docker compose exec db psql -U aemr -d aemr -c "
SELECT relname, n_live_tup, n_dead_tup,
       round(100.0*n_dead_tup/NULLIF(n_live_tup,0),1) AS dead_pct,
       last_autovacuum
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC LIMIT 10;
"

# 3. Индексы, к которым никто не обращается. Кандидаты на удаление в будущем.
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

# 5. Долгие запросы — больше 5 секунд
docker compose exec db psql -U aemr -d aemr -c "
SELECT pid, now()-query_start AS dur, left(query,120)
FROM pg_stat_activity
WHERE state='active' AND now()-query_start > interval '5 seconds';
"

# 6. Размер базы и количество файлов журнала упреждающей записи (WAL)
docker compose exec db psql -U aemr -d aemr -c "
SELECT pg_size_pretty(pg_database_size('aemr')) AS db_size,
       (SELECT count(*) FROM pg_ls_waldir()) AS wal_files;
"

# 7. Принудительный VACUUM ANALYZE на активно пишущихся таблицах.
#    VACUUM — штатная команда Postgres для уборки устаревших строк.
docker compose exec db psql -U aemr -d aemr -c "
VACUUM (ANALYZE, VERBOSE) events;
VACUUM (ANALYZE, VERBOSE) broadcast_deliveries;
"

# 8. Проверка миграций
docker compose exec bot alembic current

# 9. Проверка наличия и размера последнего бэкапа
docker compose exec bot sh -c 'ls -t /backups/aemr-*.gpg | head -1 | xargs -I {} stat -c "%n %s байт %y" {}'

# 10. Использование диска на сервере
df -h | grep -E "Filesystem|/$|docker"
```

## 9. Регистрация и управление операторами

Основной путь — через бота в админ-группе MAX. Резервный путь через shell тоже работает.

```bash
# Посмотреть всех операторов
docker compose exec db psql -U aemr -d aemr -c \
  "SELECT id, max_user_id, role, full_name, active FROM operators ORDER BY id;"

# Деактивировать оператора при увольнении. Это мягкое удаление.
docker compose exec db psql -U aemr -d aemr -c \
  "UPDATE operators SET active=false WHERE max_user_id=<их_id>;"

# Сменить роль оператора через psql. Бот этого не умеет — защита от самоповышения.
docker compose exec db psql -U aemr -d aemr -c \
  "UPDATE operators SET role='it' WHERE max_user_id=<их_id>;"

# Аварийно вписать первого ИТ-оператора, если автоматическая инициализация не сработала
docker compose exec db psql -U aemr -d aemr -c \
  "INSERT INTO operators (max_user_id, full_name, role, active)
   VALUES (<id>, 'Иванов И.И.', 'it', true);"
```

Штатно регистрация идёт через команду `/add_operators` в админ-группе. См. [RUNBOOK §2](RUNBOOK.md).

## 10. Аварийные процедуры

```bash
# Бот молчит, контейнер живой — простой перезапуск
docker compose restart bot

# Бот молчит, контейнер мёртв
docker compose up -d bot

# База недоступна
docker compose logs --tail=100 db
docker compose restart db
sleep 10
docker compose restart bot

# Откат на предыдущую версию по тегу
git fetch --tags
git log --oneline -5
git checkout <предыдущий_тег>
docker compose up -d --build bot

# Откат миграции после неудачного обновления
docker compose exec bot alembic downgrade -1

# Сброс тестовых данных перед запуском в продакшен. НЕ ЗАПУСКАТЬ В ПРОДАКШЕНЕ.
cat ../scripts/reset_test_data.sql | \
  docker compose exec -T db psql -U aemr -d aemr

# Полная пересборка контейнера, если что-то совсем сломано
docker compose down
docker compose up -d --build
```

## 11. Проверочный прогон после изменений

Минимальный набор проверок после развёртывания или перезапуска.

```bash
# 1. Контейнеры здоровы
docker compose ps
# Ожидание: оба сервиса в статусе "running (healthy)"

# 2. /healthz отвечает
curl -fsS http://127.0.0.1:8080/healthz | python3 -m json.tool
# Ожидание: "ok": true, "db_ok": true

# 3. Бот авторизован в MAX. Видно в логах после старта.
docker compose logs --tail=50 bot | grep -iE "long polling|first_name|@"
# Ожидание: строка вида "Бот: @<имя_бота> first_name=... id=..."

# 4. Задачи планировщика APScheduler зарегистрированы.
#    APScheduler — встроенный планировщик задач, аналог cron внутри процесса бота.
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

Если все шесть шагов прошли — развёртывание принято. Иначе — `docker compose logs bot` и [RUNBOOK §5](RUNBOOK.md), что делать, если бот молчит.
