# Backup restore-test

Цель restore-test — доказать, что backup не просто создаётся, а действительно восстанавливается в рабочую PostgreSQL-базу. Без restore-test backup считается непроверенным.

Документ описывает безопасную проверку в отдельной тестовой БД на VPS. Продовую БД не трогать.

## 1. Найти свежий backup

На VPS:

```bash
cd /home/aemr/aemr-bot/infra
. ./.env
ls -lah /home/aemr/backups 2>/dev/null || true
find /home/aemr -maxdepth 4 -type f \( -name '*.sql' -o -name '*.sql.gz' -o -name '*.sql.age' -o -name '*.dump' \) -printf '%TY-%Tm-%Td %TH:%TM %p\n' | sort | tail -20
```

Ожидаемо: есть свежий backup-файл за ожидаемый период.

## 2. Создать отдельную тестовую БД

```bash
cd /home/aemr/aemr-bot/infra
. ./.env

docker compose exec -T db psql -U "$POSTGRES_USER" -d postgres -c 'DROP DATABASE IF EXISTS aemr_restore_test;'
docker compose exec -T db psql -U "$POSTGRES_USER" -d postgres -c 'CREATE DATABASE aemr_restore_test;'
```

Если имя сервиса PostgreSQL не `db`, посмотреть:

```bash
docker compose ps
```

## 3. Восстановить plain SQL backup

Для `.sql`:

```bash
BACKUP=/path/to/latest.sql
cat "$BACKUP" | docker compose exec -T db psql -U "$POSTGRES_USER" -d aemr_restore_test
```

Для `.sql.gz`:

```bash
BACKUP=/path/to/latest.sql.gz
gzip -dc "$BACKUP" | docker compose exec -T db psql -U "$POSTGRES_USER" -d aemr_restore_test
```

Для encrypted `.age` используйте фактический ключ/команду из регламента сервера. Если ключа нет под рукой, restore-test нельзя считать пройденным.

## 4. Минимальная проверка восстановленной БД

```bash
docker compose exec -T db psql -U "$POSTGRES_USER" -d aemr_restore_test -c '\dt'
docker compose exec -T db psql -U "$POSTGRES_USER" -d aemr_restore_test -c 'select count(*) as users from users;'
docker compose exec -T db psql -U "$POSTGRES_USER" -d aemr_restore_test -c 'select count(*) as appeals from appeals;'
docker compose exec -T db psql -U "$POSTGRES_USER" -d aemr_restore_test -c 'select count(*) as messages from messages;'
docker compose exec -T db psql -U "$POSTGRES_USER" -d aemr_restore_test -c 'select count(*) as operators from operators;'
docker compose exec -T db psql -U "$POSTGRES_USER" -d aemr_restore_test -c 'select count(*) as settings from settings;'
```

Ожидаемо:

- таблицы есть;
- базовые SELECT-запросы выполняются;
- счётчики выглядят правдоподобно;
- нет ошибок отсутствующих таблиц/колонок.

## 5. Проверить alembic version

```bash
docker compose exec -T db psql -U "$POSTGRES_USER" -d aemr_restore_test -c 'select * from alembic_version;'
```

Ожидаемо: версия соответствует последней миграции `main`. Если версия старая, restore технически возможен, но после восстановления потребуется `alembic upgrade head`.

## 6. Проверить ПДн-риски после restore

После восстановления старого backup могут вернуться данные, которые уже были удалены в production после даты backup. Это нормальное свойство backup, но его нужно учитывать регламентно.

Минимальная проверка:

```bash
docker compose exec -T db psql -U "$POSTGRES_USER" -d aemr_restore_test -c "select count(*) from users where first_name = 'Удалено';"
docker compose exec -T db psql -U "$POSTGRES_USER" -d aemr_restore_test -c "select count(*) from appeals where summary is not null and closed_due_to_revoke = true;"
docker compose exec -T db psql -U "$POSTGRES_USER" -d aemr_restore_test -c "select count(*) from messages where text is not null and appeal_id in (select id from appeals where closed_due_to_revoke = true);"
```

Если после production-restore backup старше факта удаления ПДн, надо повторно применить операции удаления/обезличивания по журналу заявок. Поэтому один только restore backup не завершает incident recovery.

## 7. Удалить тестовую БД

```bash
docker compose exec -T db psql -U "$POSTGRES_USER" -d postgres -c 'DROP DATABASE IF EXISTS aemr_restore_test;'
```

## 8. Минимальный отчёт restore-test

```text
Backup restore-test:
- backup file: <path>
- backup date: <date>
- encrypted: yes/no
- restore DB created: ok/fail
- restore command: ok/fail
- tables visible: ok/fail
- users count: <n>
- appeals count: <n>
- messages count: <n>
- alembic version: <version>
- test DB dropped: ok/fail
- notes: <если есть>
```

## 9. Когда restore-test считается проваленным

Restore-test не пройден, если:

- backup-файл не найден;
- файл найден, но не удаётся расшифровать;
- restore падает на SQL-ошибках;
- ключевых таблиц нет;
- alembic version отсутствует или явно не соответствует проекту;
- тестовую БД забыли удалить после проверки.
