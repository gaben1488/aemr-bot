# SYSADMIN.md — операционное руководство

Этот документ — handover для системного администратора, который принимает aemr-bot на сопровождение. Цель: один раз установить, потом понимать что чинить и куда смотреть когда что-то идёт не так.

Дополняет, не заменяет: `docs/SETUP.md` (первичная установка), `docs/RUNBOOK.md` (повседневные операции), `docs/VPS_SMOKE_CHECKLIST.md` (проверка после деплоя), `docs/RUNBOOK_PDN_ERASURE.md` (152-ФЗ операции).

## 1. Что это вообще

Бот «Обратная связь Администрации Елизовского муниципального округа» в MAX-мессенджере. Принимает обращения жителей, передаёт операторам в служебную группу, доставляет ответы. Self-hosted, никаких облаков (Aeza, VK, Yandex Cloud — мимо). Один процесс бота + один Postgres в Docker Compose на одной VPS.

Канонический репозиторий: `https://github.com/gaben1488/aemr-bot`.

## 2. Минимальный сервер

- Ubuntu 22.04 LTS или 24.04 LTS (Debian 12 тоже годится). NTP синхронизирован — бот считает SLA в `Asia/Kamchatka`.
- 1 vCPU, 2 GB RAM. Бот в compose ограничен 512 MB; Postgres съест ~200–400 MB; остальное на cron-jobs и пиковые бэкапы.
- 20 GB SSD: ОС ~5 GB, Docker layers ~3 GB, БД ~1 GB на старте плюс ~50 MB в год, бэкапы (8 еженедельных под GPG ~50 MB каждый) ~400 MB, логи 30 MB с ротацией.
- Сеть: исходящий HTTPS обязателен (MAX API — `botapi.max.ru`, `platform-api.max.ru`). **Входящие порты не открываем в полл-режиме.** Если webhook — нужны 80/443 и публичный домен.

## 3. Технологический стек

### 3.1 Docker-слой (закреплено digest'ами)

| Образ | Тег | sha256 digest |
|---|---|---|
| python (для bot) | `3.12-slim` | `4386a385d81dba9f72ed72a6fe4237755d7f5440c84b417650f38336bbc43117` |
| postgres | `16-alpine` | `4e6e670bb069649261c9c18031f0aded7bb249a5b6664ddec29c013a89310d50` |
| nginx (webhook only) | `1.27-alpine` | без digest, под profile `webhook` |
| certbot (webhook only) | `latest` | под profile `webhook` |

Обновлять digest вручную через `docker pull <image>` → переписать digest в `infra/docker-compose.yml`. Автообновление выключено намеренно — компрометированный или сломанный минор не должен прийти в продакшен молча.

В Dockerfile дополнительно ставится apt: `postgresql-client gnupg ca-certificates curl tzdata unzip rclone`. Rclone из debian-репо, а не `curl install.sh | bash` — иначе компромет домена выполнялся бы как root в образе.

### 3.2 Python и runtime-зависимости (bot/pyproject.toml)

Требуется Python 3.12 (внутри slim-образа уже).

Пины через compatible-release `~=` — патчи свободно, минор-bump блокирован.

```
maxapi~=0.6        — клиент MAX Bot API
fastapi~=0.115     — для webhook-режима
uvicorn[standard]~=0.32
sqlalchemy~=2.0    — async ORM
asyncpg~=0.30      — драйвер Postgres
alembic~=1.14      — миграции
apscheduler~=3.10  — cron jobs в процессе
pydantic~=2.9
pydantic-settings~=2.6
openpyxl~=3.1      — XLSX-выгрузка стат
python-dotenv~=1.0
python-dateutil~=2.9  — relativedelta для retention
aiohttp~=3.10
shapely~=2.0       — point-in-polygon по поселениям ЕМО (локально)
```

Dev/CI: `pytest>=9.0.3` (CVE-2025-71176 fix), `pytest-asyncio>=1.3`, `pytest-cov~=6.0`, `ruff~=0.7`, `mypy~=1.13`, `bandit~=1.7`, `pip-audit~=2.7`, `aiosqlite~=0.20`, `types-python-dateutil~=2.9`.

## 4. Установка с нуля

```bash
# 1. Пользователь для бота, в docker group
sudo adduser --disabled-password --gecos "" aemr
sudo usermod -aG docker aemr

# 2. Клонирование
sudo -u aemr -i
git clone https://github.com/gaben1488/aemr-bot.git
cd aemr-bot

# 3. Конфигурация — см. раздел 5
cp infra/.env.example infra/.env
chmod 600 infra/.env
# заполнить infra/.env (см. ниже что обязательно)

# 4. Запуск
cd infra
docker compose up -d --build

# 5. Подождать ~60 секунд, проверить
curl -fsS http://127.0.0.1:8080/livez && echo
curl -fsS http://127.0.0.1:8080/readyz && echo

# 6. Auto-deploy под root
exit
sudo bash /home/aemr/aemr-bot/scripts/install-auto-deploy.sh
# выдаст pubkey, его в GitHub Settings → Deploy keys (read-only)

# 7. Внешний watchdog в cron */5 под root
sudo crontab -e
# добавить:
# */5 * * * * /home/aemr/aemr-bot/scripts/healthwatch.sh
```

После первого запуска нужен **один лишний рестарт `db`**, чтобы `pg_stat_statements` подхватился из `shared_preload_libraries`. Миграция 0010 создаёт extension, но статистика начнёт писаться только после рестарта Postgres.

```bash
cd /home/aemr/aemr-bot/infra
docker compose restart db
```

## 5. Конфигурация `infra/.env`

Полная документация полей — в `infra/.env.example`. Здесь — что обязательно и какие есть подводные камни.

### 5.1 Обязательные

```dotenv
BOT_TOKEN=                # выдаёт max.ru/business. БЕЗ префикса "Bearer".
POSTGRES_PASSWORD=        # минимум 24 символа случайно. Compose откажется стартовать с пустым.
DATABASE_URL=postgresql+asyncpg://aemr:<тот же пароль>@db:5432/aemr
POSTGRES_DB=aemr
POSTGRES_USER=aemr
ADMIN_GROUP_ID=           # -74181728103785, узнаётся /whoami в служебной группе
BOOTSTRAP_IT_MAX_USER_ID= # MAX user_id первого IT-оператора
BOOTSTRAP_IT_FULL_NAME=ИТ-специалист
TZ=Asia/Kamchatka
```

`POSTGRES_PASSWORD` обязателен на уровне docker-compose через `${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in infra/.env}`. Без него `docker compose up` упадёт с явной ошибкой ещё до старта контейнеров. В `.env.example` стоит небезопасный дефолт `change-me-strong` — его обязательно нужно заменить.

### 5.2 Сильно рекомендую

```dotenv
BACKUP_GPG_PASSPHRASE=    # 32+ случайных символов; иначе бэкапы plain SQL
HEALTHCHECK_URL=          # Healthchecks.io / Uptime Kuma URL, опционально
```

### 5.3 Webhook (только если `BOT_MODE=webhook`)

```dotenv
BOT_MODE=webhook
WEBHOOK_URL=https://feedback.elizovomr.ru/max/webhook
WEBHOOK_SECRET=           # ≥16 символов, генерация: python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Без `WEBHOOK_SECRET` ≥16 символов и `WEBHOOK_URL` процесс бота не стартует в webhook-режиме (pydantic-validator в `config.py` ругается).

### 5.4 S3-бэкапы (опционально)

```dotenv
BACKUP_S3_ENDPOINT=
BACKUP_S3_BUCKET=
BACKUP_S3_ACCESS_KEY=
BACKUP_S3_SECRET_KEY=
```

Если все четыре пустые — только локальные бэкапы в named volume.

### 5.5 Неиспользуемые переменные в прод-`.env`

В прод-`.env` встречаются `COORDINATOR_MAX_USER_ID` и `BACKUP_TMP_DIR` — это **мёртвые переменные**, оставшиеся от ранних версий. Их можно и нужно удалить.

Почему они ничего не делают:

- конфиг бота (`config.py`) построен на pydantic-settings с `extra="ignore"` — любая переменная, которой нет в схеме `Settings`, просто молча игнорируется. Опечатка в имени переменной повела бы себя так же;
- ни одно из имён не встречается ни в коде бота, ни в `infra/.env.example`, ни в остальной документации.

Чем их заменили в своё время:

- `COORDINATOR_MAX_USER_ID` — реликт эпохи, когда координатор был один и «зашит» в конфиг. Сейчас роли операторов живут в таблице `operators` и назначаются через `/add_operators` в служебной группе (см. раздел 8 и `docs/SECURITY.md` §4.5). Первый `it`-оператор поднимается через `BOOTSTRAP_IT_MAX_USER_ID`. Отдельная переменная для координатора не нужна.
- `BACKUP_TMP_DIR` — реликт ранней схемы бэкапа. Сейчас бэкап пишется сразу в `BACKUP_LOCAL_DIR` (`/backups`, named volume), отдельный временный каталог нигде не читается.

**Рекомендация: удалить обе строки из прод-`.env`.** Риск нулевой — бот их и так не видит. Единственное, что стоит проверить перед удалением: не подхватывает ли эти переменные какой-нибудь сторонний shell-скрипт на хосте (в репозитории — не подхватывает; `grep -rl 'COORDINATOR_MAX_USER_ID\|BACKUP_TMP_DIR' /home/aemr /root` покажет, если где-то ещё есть). Удаление просто убирает шум — тот, кто откроет `.env` через год, не будет гадать, что эти строки значат.

## 6. Сервисы и порты

### 6.1 Карта портов

| Сервис | Bind | Виден | Назначение |
|---|---|---|---|
| `bot` 8080 | `127.0.0.1:8080` | localhost VPS | health endpoints (`/livez`, `/readyz`, `/healthz`), webhook-приёмник если включён |
| `db` 5432 | внутри Docker network | только `bot` через DNS-имя `db` | Postgres |
| `nginx` 80, 443 | `0.0.0.0` | публично | под profile `webhook`, TLS-терминация |

Postgres на хост **не пробрасывается**. Если нужно `psql` — `docker compose exec db psql -U aemr aemr`.

### 6.2 Health endpoints

- `/livez` — жив ли event-loop. Не трогает БД. Используется Docker healthcheck в compose, watchdog `healthwatch.sh`, auto-deploy health-gate. Возвращает 200, если внутренний `heartbeat` обновлялся в течение `HEALTHCHECK_STALE_SECONDS` (по умолчанию 120s).
- `/readyz` — готов ли бот к работе с данными. Делает `SELECT 1` к Postgres (кэш TTL 10s, чтобы серия проверок не порождала шквал коннектов). Падение `/readyz` при живом `/livez` означает «процесс жив, но БД лежит или зависла».
- `/healthz` — backward-compatible alias `/readyz`. Старые runbook'и могут опираться на него.

**Важно для админа**: Docker healthcheck и watchdog проверяют `/livez`, не `/readyz`. Краткая проблема Postgres не должна рестартовать живой polling-процесс — иначе теряем восстановимость диагностики.

## 7. Cron / scheduler внутри процесса бота

APScheduler в самом процессе. Все расписания в `Asia/Kamchatka`. У каждой задачи `misfire_grace_time=120` секунд — пропуски во время `docker compose up --build` догоняются.

| Задача | Когда | Что |
|---|---|---|
| `startup-pulse` | через 5 сек после старта (DateTrigger) | сообщение в служебную группу «🟢 Бот запущен/перезапущен. HH:MM» |
| `pulse-workhours` | пн–сб, 09:00–17:59, минуты :00 и :30 | «🟢 Бот работает» |
| `pulse-offhours` | пн–сб, часы 0-8 и 18-23, минута :05 | то же |
| `pulse-sunday` | вс, каждый час, минута :05 | то же |
| `health-selfcheck` | каждые `HEALTHCHECK_INTERVAL_MIN` мин (5 по умолчанию) | мониторит heartbeat, шлёт алёрт при смене healthy↔unhealthy |
| `db-backup` | каждое вс, `BACKUP_HOUR`:`BACKUP_MINUTE` (03:00 по умолчанию) | pg_dump → GPG → named volume, ротация |
| `events-retention` | ежедневно 04:00 | удаление событий старше 30 дней |
| `pdn-retention` | ежедневно 04:30 | 152-ФЗ: жителей, отозвавших согласие >30 дней назад, обезличить |
| `appeals-5y-retention` | ежедневно 04:45 | очистка summary/attachments у обращений старше 5 лет |
| `monthly-stats` | 1-го числа 09:00 | XLSX отчёт в служебную группу |
| `funnel-watchdog` | каждые 15 минут | сброс зависших воронок (житель открыл «Подать обращение» и ушёл) |
| `open-reminder-workhours` | пн–сб 09:00–17:59, минута :10 | напоминание об открытых обращениях |
| `overdue-reminder-workhours` | пн–сб 09:00–17:59, минута :40 | алёрт о просрочке по SLA |
| `healthcheck-ping` | каждые `HEALTHCHECK_INTERVAL_MIN` мин | ping на `HEALTHCHECK_URL` если задан |

Все шлются `coalesce=True, max_instances=1` — повторный тик не запустит второй экземпляр.

## 8. Команды операторов в служебной группе

11 slash-команд, регистрируются в `handlers/admin_commands.register()`:

| Команда | Кто может | Что делает |
|---|---|---|
| `/op_help` | любой оператор | памятка + кнопочное меню, закрепляется |
| `/open_tickets` | любой | список открытых обращений с кнопками действий |
| `/stats <period>` | любой | XLSX за today/week/month/quarter/half_year/year/all |
| `/reply <id> <текст>` | любой | ответ жителю на обращение #id |
| `/reopen <id>` | любой | вернуть в работу ANSWERED/CLOSED |
| `/close <id>` | любой | закрыть без ответа |
| `/diag` | любой | сводка состояния (жителей, обращений, рассылок) |
| `/broadcast` | `it` или `coordinator` | мастер рассылки, 4 шага |
| `/erase max_user_id=N` или `phone=+7...` | только `it` | удаление ПДн (см. RUNBOOK_PDN_ERASURE) |
| `/setting <key> <value>` | только `it` | правка таблицы `app_settings` (тематики, поселения, контакты) |
| `/add_operators <max_user_id> <role> <ФИО>` | только `it` | регистрация оператора |
| `/backup` | только `it` | внеплановый pg_dump |

Роли (`db/models.OperatorRole`): `coordinator`, `aemr`, `egp`, `it`. Иерархия — `it` имеет все права `coordinator`, `coordinator` имеет все права `aemr`/`egp`. Различие `aemr` vs `egp` формальное, нужно для журнала и возможной маршрутизации.

## 9. Логи

Все три контейнера — json-file driver, ротация 10 МБ × 3 файла = до 30 МБ на контейнер. Бесконечного disk-leak'а быть не может.

```bash
cd /home/aemr/aemr-bot/infra
# последние 300 строк
docker compose logs --tail 300 bot
# за 24 часа, отфильтровано
docker compose logs --since 24h bot | grep -Ei 'error|exception|traceback|misfire|pulse|startup'
# логи Postgres
docker compose logs --tail 200 db
```

Watchdog и auto-deploy пишут через `logger -t aemr-bot-watchdog` / `-t aemr-bot-deploy`, всё в journald:

```bash
journalctl -t aemr-bot-watchdog -n 100 --no-pager
journalctl -t aemr-bot-deploy -n 100 --no-pager
```

## 10. Бэкап и восстановление

### 10.1 Что бэкапится

`pg_dump` всей БД каждое воскресенье в 03:00 Камчатки. Опционально шифруется GPG `--symmetric AES256` через `BACKUP_GPG_PASSPHRASE`. Хранится 8 файлов в named volume `aemr-bot_backups` (примерно 2 месяца истории). Опционально дополнительная копия в S3.

### 10.2 Список и проверка

```bash
docker compose exec bot ls -lah /backups
```

### 10.3 Восстановление

Полный регламент с тестом на чистой БД — `docs/BACKUP_RESTORE_TEST.md`. Краткая суть:

```bash
# В отдельном временном каталоге расшифровать
cat /var/lib/docker/volumes/aemr-bot_backups/_data/aemr-YYYY-MM-DD.sql.gpg \
  | gpg --batch --passphrase "$BACKUP_GPG_PASSPHRASE" --decrypt > restore.sql

# Залить в новую БД
psql -h ... -U ... -d aemr_restore -f restore.sql
```

Не тестировать restore в production — только в отдельный экземпляр БД.

## 11. Деплой и rollback

### 11.1 Auto-deploy

`scripts/auto-deploy.sh` под root в cron `*/10`. Логика:

1. `git fetch origin main`, если новых коммитов нет — тихо выходит.
2. Запоминает текущий HEAD как `PREV_LOCAL`.
3. `git reset --hard origin/main`, `docker compose up -d --build`.
4. Health-gate: до 60 секунд опрашивает `/livez` каждые 5 секунд.
5. Если успех — лог в journald «deploy ok».
6. Если фейл — **auto-rollback**: `git reset --hard $PREV_LOCAL`, пересборка, alert в journald «DEPLOY FAILED: ROLLBACK на …».

### 11.2 Ручной деплой

```bash
sudo -u aemr -i
cd aemr-bot
git fetch origin main && git checkout main && git pull --ff-only
cd infra
docker compose build bot
docker compose up -d bot
```

После — пройти `docs/VPS_SMOKE_CHECKLIST.md`.

### 11.3 Откат вручную

```bash
sudo -u aemr -i
cd aemr-bot
git log --oneline -10
git checkout <предыдущий рабочий коммит>
cd infra
docker compose up -d --build bot
```

Миграции откатываются через `alembic downgrade <revision>`, но проверены только round-trip'ом в CI. На проде даунгрейд миграций — крайняя мера, обычно проще восстановить из бэкапа.

## 12. Watchdog (внешний)

`scripts/healthwatch.sh` под root в cron `*/5`. Опрашивает `/livez`:

- успешный ответ → сбрасывает счётчик фейлов;
- 3 фейла подряд (15 минут) → `docker compose restart bot`;
- 8 фейлов подряд (40 минут) → alert в служебную группу через MAX API напрямую через curl (минуя бота — он же мёртв).

Не путать с auto-deploy health-gate. Auto-deploy ловит сломанный новый коммит; healthwatch — повисший живой контейнер.

## 13. Аудит сервера

```bash
sudo bash /home/aemr/aemr-bot/scripts/audit_vps.sh
```

Выгрузка в `/tmp/aemr_audit_<timestamp>.tar.gz`. Внутри: системные данные, git state, env с замаскированными секретами, docker compose ps, healthchecks, последние логи бота и БД. Скрипт **не** выводит реальные значения `BOT_TOKEN`, `POSTGRES_PASSWORD`, `BACKUP_GPG_PASSPHRASE`, `WEBHOOK_SECRET`, `BACKUP_S3_*_KEY`, заголовки `Authorization`, параметры `access_token` — все заменены на `***HIDDEN***`.

Архив безопасно прислать в чат или приложить к тикету.

## 14. Типичные проблемы и куда смотреть

**Бот молчит, в служебной группе тишина**. Сначала `/livez`. Если 200 — процесс жив, проблема в MAX-сессии или сети. Если 5xx или таймаут — `docker compose logs --tail 200 bot`.

**Pulse не приходит**. С моей фиксы misfire_grace_time=120 это означает либо `BOT_TOKEN` истёк/отозван, либо MAX rate-limit. `/diag` в служебной группе подскажет.

**Pulse-startup пришёл и тишина**. Бот рестартанул, скорее всего auto-deploy. `journalctl -t aemr-bot-deploy -n 50`.

**DEPLOY FAILED в journald**. Auto-rollback сработал. Сейчас работает предыдущий коммит. Проверить новый локально, понять причину, пушнуть фикс.

**Backup не приходит в служебную группу**. Воскресенье 03:00, ждать до 03:05. Если в `_job_backup_with_alert` была ошибка — алёрт всё равно придёт.

**База растёт быстрее ожидаемого**. `docker compose exec db psql -U aemr aemr -c "\dt+"` — посмотреть размеры таблиц. `events` обычно самая жирная (idempotency), retention 30 дней должна её сдерживать.

**Restart-loop**. `docker compose ps` покажет `Restarting`. `docker compose logs --tail 500 bot` — почему. Если новый коммит — auto-rollback должен был сработать; если не сработал, ручной откат (раздел 11.3).

## 15. CI и pull requests

На каждом push в main и каждом PR прогоняется `.github/workflows/ci.yml`:

- **lint job**: `ruff check --output-format=github`, `mypy`, `bandit -ll` (medium+), `pip-audit --strict` (hard fail на любой CVE), `shellcheck` на все `*.sh`.
- **test job**: `pytest --cov-fail-under=65`, `alembic upgrade head`, `alembic check` (drift между моделями и миграциями), `alembic round-trip` (upgrade → downgrade base → upgrade head).
- **docker-build job**: смок-сборка образа без push.

Auto-deploy на сервере подтягивает только из `main`. PR-ветки не деплоятся.

## 16. Что НЕ настроено и что админ должен решить

**HA**. Single-VPS — single point of failure. Нет балансировщика, нет реплики Postgres. Решение — second VPS + Patroni/repmgr + Cloudflare для DNS-failover. Архитектура, не код.

**TLS внутри Docker network между bot↔db**. Сейчас доверенная сеть Docker. Для повышения уровня — Postgres SSL-сертификаты + asyncpg `ssl=require`.

**Внешний healthcheck**. `HEALTHCHECK_URL` в .env — опционально. Минимум: завести Healthchecks.io / Uptime Kuma, дать ботичу URL, добавить алёрт в почту/Telegram админа. Без этого пульс — единственный сигнал.

**Ротация секретов**. Регламент готов — см. `docs/SECURITY.md`, раздел 6 «Ротация секретов». Полугодовой календарь для `BOT_TOKEN`, `POSTGRES_PASSWORD`, `BACKUP_GPG_PASSPHRASE` и прочих. Задача админа — поставить напоминание в календарь (январь/июль) и вести журнал ротаций.

**Backup restore-test**. Регламент готов — см. `docs/BACKUP_RESTORE_TEST.md`. Проводить раз в квартал. Задача админа — поставить напоминание и вести журнал.

**Апгрейд ОС: Ubuntu 20.04 уже EOL**. Стандартная поддержка Ubuntu 20.04 LTS закончилась в апреле 2025 года — security-обновления для базовой системы больше не приходят (платный ESM до 2030 — отдельная история). Это нужно закрыть до сдачи в эксплуатацию.

Хорошая новость: бот живёт в Docker-контейнерах, ОС хоста для него — это только Docker, cron и SSH. Поэтому апгрейд относительно безопасен. Два пути:

1. **Апгрейд на месте** (меньше работы, есть downtime ~30–60 мин):
   - снять снапшот VPS у провайдера + свежий `/backup` БД;
   - `sudo do-release-upgrade` 20.04 → 22.04 LTS (поддержка до 2027). Перепрыгнуть сразу на 24.04 нельзя — только через 22.04, либо чистая установка;
   - после перезагрузки проверить: `docker compose ps` (контейнеры поднялись), `crontab -l` под root (healthwatch и auto-deploy на месте), SSH-доступ, `/livez`, пульс в служебной группе;
   - позже, по желанию, тем же путём 22.04 → 24.04 LTS (поддержка до 2029).
2. **Чистый сервер** (больше работы, чище результат): поднять новый VPS на Ubuntu 24.04 LTS, развернуть бота с нуля по разделу 4, восстановить БД из бэкапа (`docs/BACKUP_RESTORE_TEST.md`), переключить — и погасить старый. Рекомендуется, если VPS давно живёт и накопил «исторический мусор».

Любой путь — только после успешного restore-test, чтобы было откуда возвращаться.

**Изоляция co-located проекта `dash`**. На том же хосте, что и бот с персональными данными граждан, живёт отдельный проект `dash` (caddy/server/web на портах 80/443, смотрит в интернет). Это нарушение изоляции: уязвимость в `dash` или его зависимостях даёт злоумышленнику плацдарм на хосте, где рядом лежит БД с ПДн. Бот специально не открывает наружу ни одного порта именно ради такой изоляции — а `dash` её ломает.

Решение (по убыванию надёжности):

1. **Вынести `dash` на отдельный VPS** — полная изоляция, рекомендуется. ПДн-бот остаётся на хосте без входящих портов вообще.
2. **Жёстко разделить на одном хосте**, если отдельный VPS невозможен: `dash` и бот — в разных Docker-сетях без общих томов; firewall (ufw/nftables) запрещает контейнерам `dash` любой доступ к порту Postgres бота; у `dash` — свой непривилегированный пользователь, не в `docker` group вместе с `aemr`. Это снижает риск, но не убирает: общий core ОС остаётся.
3. **Явно принять риск** — только как временная мера, с письменной отметкой владельца проекта, что ИБ-риск co-located internet-facing сервиса рядом с ПДн осознан и принят.

Выбран путь — изолировать. До изоляции хост нельзя считать готовым к промышленной обработке ПДн.

**Регламент компетенций**. Кто имеет SSH-доступ на VPS, кто root, кто owner GitHub-репо. Без формального RBAC любая ошибка человека == продакшен.

Подробности и контактные точки — у владельца проекта (Канал управления цифровизации Администрации ЕМО).
