# ROLLBACK Playbook — откат бота при сбое после деплоя

**Когда применять:** новый деплой (через auto-deploy или ручной `tar+scp`) сломал production. Симптомы: `/healthz` 5xx или таймаут, MAX-обращения не проходят, в логах stack-trace на старте бота, оператор шлёт «бот не отвечает».

**Цель:** вернуть последнее работавшее состояние **за ≤ 10 минут** без потери данных.

---

## Шаг 0. Тревожно — но не паникуем

Бот health-down НЕ значит «потеря данных». БД (`aemr-bot-db-1`) живёт независимо от приложения. Все обращения, операторы, audit-лог сохраняются. Откатываем **только** код приложения.

Сначала **диагностируй** через 30 секунд логов:

```bash
ssh root@193.233.244.217 "docker logs aemr-bot-bot-1 --tail 50 2>&1"
```

Если ошибка очевидно временная (сеть, таймаут MAX) — не откатывай, дай 5 минут на самовосстановление через `restart: unless-stopped`.

---

## Шаг 1. Откатить код к последнему рабочему коммиту

**Если auto-deploy включён** (`/usr/local/bin/aemr-bot-autodeploy`):

```bash
ssh root@193.233.244.217 'set -e
cd /home/aemr/aemr-bot
PREV=$(git rev-parse HEAD~1)
echo "Откат на $PREV"
git reset --hard "$PREV"
chown -R aemr:aemr .
su - aemr -c "cd infra && docker compose up -d --build"
'
```

После этого в течение 10 минут cron `aemr-bot-autodeploy` снова попытается подтянуть последний main и **опять упадёт** на той же ошибке. Чтобы этого не было — **сразу делаем revert-коммит** в репозитории на машине разработчика:

```bash
# Локально на dev-машине
cd C:\Users\filat\max\aemr-bot
git revert HEAD --no-edit
git push origin main
```

Через 10 минут auto-deploy подхватит revert-коммит и сравняется на нём.

**Если auto-deploy выключен** — оставайся на старом коде на сервере, не делай новый push без фикса.

---

## Шаг 2. Откатить миграцию БД (если виновата она)

Если в логах `alembic.runtime.migration` ругается на upgrade или приложение падает на запросах к свежим колонкам:

```bash
ssh root@193.233.244.217 'docker exec aemr-bot-bot-1 alembic current'
# должен показать revision_id вашей последней успешной миграции
ssh root@193.233.244.217 'docker exec aemr-bot-bot-1 alembic downgrade -1'
# проверь что упала на одну версию назад
ssh root@193.233.244.217 'docker exec aemr-bot-bot-1 alembic current'
```

**Внимание:** не каждая миграция реально обратима. Проверь `bot/alembic/versions/<revision>.py` — есть ли в `downgrade()` реальный rollback или там `pass`/`raise NotImplementedError`. Если необратима — переходи к шагу 3.

---

## Шаг 3. Восстановить БД из бэкапа (последний рубеж)

Если миграция повредила данные и `downgrade()` не восстанавливает — нужен restore из последнего бэкапа.

```bash
# Самый свежий локальный бэкап:
ssh root@193.233.244.217 "ls -lah /var/lib/docker/volumes/aemr-bot_backups/_data/ | tail -5"
```

Если бэкап зашифрован (`.sql.gpg`):

```bash
ssh root@193.233.244.217 'set -e
cd /home/aemr/aemr-bot
# ВАЖНО: passphrase нужно прочитать из .env
PHRASE=$(grep ^BACKUP_GPG_PASSPHRASE infra/.env | sed -E "s/^[^=]+=//; s/^[\\x27\"]?(.*?)[\\x27\"]?\$/\\1/")
# Расшифровка
docker run --rm -v aemr-bot_backups:/backups alpine \
    sh -c "apk add --no-cache gnupg && \
           echo \"$PHRASE\" | gpg --batch --yes --passphrase-fd 0 \
                --decrypt /backups/aemr-XXXX.sql.gpg > /tmp/restore.sql"
# Импорт в новую БД (НЕ затирай прод!)
docker exec -i aemr-bot-db-1 psql -U aemr -d aemr_restore_test < /tmp/restore.sql
# Сначала сравни с проdом, только потом меняй основную БД (см. RUNBOOK §7)
'
```

**Никогда** не делай `psql -d aemr` (production database) с импортом без предварительной проверки в `aemr_restore_test`. Восстановление из бэкапа — терминальный шаг, теряются данные между бэкапом и моментом сбоя.

---

## Шаг 4. После восстановления

1. Убедись что `/healthz` отвечает 200:
   ```bash
   ssh root@193.233.244.217 "curl -fsS http://127.0.0.1:8080/healthz"
   ```
2. Проверь pulse в админ-чате (раз в час должен прийти «🟢 Бот работает.»).
3. Найди корневую причину: `git log <revert_sha>..HEAD` показывает что ушло в revert. Открой issue или todo на исправление.
4. Только после исправления повтори деплой.

---

## RTO / RPO

- **RTO (recovery time objective):** до 10 минут при наличии человека у клавиатуры. Auto-restart Docker при `unless-stopped` обычно восстанавливает за 1-2 минуты при временных ошибках сам. Откат кода = git reset → docker compose up = 3-5 мин. Откат миграции = alembic downgrade ≈ 1 мин.
- **RPO (recovery point objective):** до 1 недели (бэкап раз в воскресенье в 03:00 Камчатки). Между бэкапами потерь данных нет — восстановление кода с диска.
- При полной потере VPS — RPO = 1 неделя, RTO = 2-4 часа на новом хосте (нужно: новый сервер, восстановить .env вручную, восстановить БД из последнего бэкапа). **Сейчас offsite-бэкапа нет** (S3 не настроен). Если хост сгорит — потеряем всё. Это документированный риск; решается включением `BACKUP_S3_*` (см. `docs/SETUP.md`).

---

## Превентивно: что бы спасло от текущего инцидента

После каждого реального инцидента — добавь сюда строку «что бы спасло»:

| Дата | Что сломалось | Что бы спасло |
|---|---|---|
| _шаблон_ | _ml-зависимость в новой версии_ | _фиксация версий + тесты на startup_ |
