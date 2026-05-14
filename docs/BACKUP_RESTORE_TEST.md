# Backup restore-test

Цель restore-test — доказать, что backup не просто создаётся, а действительно восстанавливается в рабочую PostgreSQL-базу. Без restore-test backup считается непроверенным.

Документ описывает безопасную проверку в отдельной тестовой БД на VPS. Продовую БД не трогать.

## 0. Когда проводить

**Раз в квартал** — например, в первый рабочий день января, апреля, июля и октября. Дополнительно — после любого изменения схемы БД (новая alembic-миграция) или процедуры бэкапа. Проверка занимает ~15 минут.

Отметку о проведении (кто, когда, результат — см. отчёт в разделе 8) заносить в журнал эксплуатации. Если квартальный restore-test пропущен — бэкапы официально считаются непроверенными, и об этом должен знать владелец проекта.

## 1. Найти свежий backup

Бэкапы лежат в Docker named volume `aemr-bot_backups`. Еженедельный бэкап делается каждое воскресенье в 03:00 по Камчатке. Посмотреть список:

```bash
cd /home/aemr/aemr-bot/infra
. ./.env
# изнутри контейнера бота:
docker compose exec bot ls -lah /backups
# либо напрямую с хоста (нужен root):
sudo ls -lah /var/lib/docker/volumes/aemr-bot_backups/_data/
```

Ожидаемо: есть свежий файл `aemr-YYYY-MM-DD.sql` (или `aemr-YYYY-MM-DD.sql.gpg`, если включено GPG-шифрование через `BACKUP_GPG_PASSPHRASE`) за ожидаемый период.

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

## 3. Восстановить backup

Если бэкап незашифрован (`.sql`):

```bash
BACKUP=/var/lib/docker/volumes/aemr-bot_backups/_data/aemr-YYYY-MM-DD.sql
sudo cat "$BACKUP" | docker compose exec -T db psql -U "$POSTGRES_USER" -d aemr_restore_test
```

Если бэкап зашифрован GPG (`.sql.gpg` — так и настроено, когда в `.env` задан `BACKUP_GPG_PASSPHRASE`):

```bash
BACKUP=/var/lib/docker/volumes/aemr-bot_backups/_data/aemr-YYYY-MM-DD.sql.gpg
sudo cat "$BACKUP" \
  | gpg --batch --passphrase "$BACKUP_GPG_PASSPHRASE" --decrypt \
  | docker compose exec -T db psql -U "$POSTGRES_USER" -d aemr_restore_test
```

Если GPG-passphrase утеряна — зашифрованный бэкап восстановить **невозможно**, restore-test провален. Это и есть главная причина хранить passphrase отдельно от сервера (см. SECURITY.md, раздел 6 «Ротация секретов»).

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
