# SECURITY.md — модель угроз и контролов

Документ для специалиста по информационной безопасности, который принимает aemr-bot на аудит и сопровождение. Описывает реальную, а не идеализированную картину: что защищено, чем, и где пока компромисс.

Связанные документы: `docs/RUNBOOK_PDN_ERASURE.md` (152-ФЗ операции), `docs/SYSADMIN.md` (эксплуатация), `docs/Политика.md` (актуальная политика ПДн — источник для `PRIVACY.pdf`), `docs/Политика_v2.md` (расширенная редакция для юридической экспертизы), `docs/COMPLIANCE_WITH_REGLAMENT_v7.md` (соответствие действующему Регламенту).

## 1. Контур и контекст

aemr-bot — государственный сервис обратной связи, работает с ПДн жителей и подпадает под 152-ФЗ и 59-ФЗ. Self-host на одной VPS под контролем оператора (Администрация Елизовского муниципального округа). Никаких сторонних SaaS, никаких облачных API кроме MAX-мессенджера.

Главные активы:

- персональные данные жителей в Postgres (имя, телефон, адрес, текст обращений, вложения, MAX user_id);
- секреты в `infra/.env` (BOT_TOKEN, пароль БД, GPG passphrase);
- резервные копии БД в named volume `aemr-bot_backups`;
- история действий в `audit_log`;
- логи Docker (могут содержать косвенные идентификаторы).

Угрозы, против которых проектировался контур: компрометация VPS (украли ключи), компрометация MAX (украли BOT_TOKEN или подменили события), внутренний злоумышленник (один из операторов), внешний флуд/DoS, юридический инцидент (запрос на удаление по 152-ФЗ ст. 21).

## 2. Сетевая модель

В режиме polling **публичных входящих портов нет**. Бот сам опрашивает MAX исходящим HTTPS. В режиме webhook добавляется nginx с 80/443 и публичный домен.

### 2.1 Карта портов

| Сервис | Bind | Доступен | Содержимое |
|---|---|---|---|
| `bot` 8080 | `127.0.0.1:8080` на хосте | только localhost VPS | `/livez`, `/readyz`, `/healthz`, при webhook — `/max/webhook` |
| `db` 5432 | inside Docker network | только bot через DNS-имя `db` | Postgres |
| `nginx` 80, 443 | `0.0.0.0` | публично, **только при profile `webhook`** | TLS-терминация, прокси на bot:8080 |

Postgres-порт **на хост не пробрасывается** — `psql` извне VPS невозможен по дизайну. Внутри VPS — через `docker compose exec db psql ...`.

### 2.2 Исходящие соединения

Только три точки:

- `botapi.max.ru`, `platform-api.max.ru` — MAX Bot API через HTTPS, аутентификация заголовком `Authorization`;
- `BACKUP_S3_ENDPOINT` (опционально) — S3-совместимое хранилище через rclone;
- `HEALTHCHECK_URL` (опционально) — внешний watchdog типа Healthchecks.io.

Других внешних коннектов код не делает. Можно заворачивать в Iptables outbound whitelist.

### 2.3 Webhook (если включён)

`bot/aemr_bot/main.py` принимает `POST /max/webhook`. Защита:

- nginx делает TLS-терминацию, certbot обновляет Let's Encrypt;
- бот **обязательно** проверяет заголовок `X-Max-Secret` против `WEBHOOK_SECRET` через `hmac.compare_digest` (защита от timing-oracle);
- если `BOT_MODE=webhook`, pydantic-validator в `config.py` отказывается стартовать без `WEBHOOK_SECRET` ≥16 символов;
- секрет принимается **только в заголовке**, не в query — query утекает в логи nginx, Referer и историю браузера.

## 3. Хостовая поверхность

### 3.1 Контейнерное укрепление

**bot**:

- `read_only: true` — корневая файловая система неизменяема, всё пишущее идёт в tmpfs или named volume;
- `tmpfs: /tmp:size=128m` — pip wheels, temp-файлы, gpg home;
- `mem_limit: 512m`, `memswap_limit: 512m` — рассылка на миллион жителей не уронит хост;
- `pids_limit: 200` — fork-bomb из gpg/rclone/pg_dump упирается в потолок;
- `cap_drop: ALL` — никаких Linux capabilities, боту они не нужны;
- `security_opt: no-new-privileges:true` — setuid-эскалации заблокированы;
- UID/GID 1000 (botuser), не root.

**db**:

- `cap_drop: ALL`, оставлены только `CHOWN`, `DAC_READ_SEARCH`, `FOWNER`, `SETGID`, `SETUID` — нужны Postgres для chown'а data-каталога при первой инициализации;
- `no-new-privileges: true`;
- `read_only: false` (нужно writable `${PGDATA}`).

### 3.2 Логи

Все три контейнера — json-file driver, ротация 10 МБ × 3 файла. Бесконечного disk-leak'а быть не может. Логи можно агрегировать в SIEM через journald или filebeat, но **до этого нужно проверить, не пишутся ли в них PII** (см. раздел 7.2).

### 3.3 SSH и доступ к хосту

`scripts/install-auto-deploy.sh` создаёт отдельный read-only deploy-key `/root/.ssh/aemr-bot-deploy` именно для репозитория `gaben1488/aemr-bot`. Это **не** общий root-ssh-key — компромет одного ключа не даёт доступ ко всему GitHub-аккаунту.

Sudo-доступ на VPS — отдельная роль, выдаётся персонально.

## 4. Аутентификация и роли

### 4.1 Bot ↔ MAX

Единственный `BOT_TOKEN` в заголовке `Authorization` каждого запроса. Токен:

- хранится в `infra/.env`, файл должен быть `chmod 600`;
- передаётся в контейнер через `env_file`, не в `docker run --env` (иначе виден в `ps`);
- никогда не уходит в логи (логи фильтруются `audit_vps.sh`);
- никогда не появляется в query или argv subprocess (`gpg`, `rclone`);
- ротация — через бизнес-кабинет `max.ru/business`. Регламент смены: новый токен → правка `.env` → `docker compose up -d --force-recreate bot`. Старый токен инвалидируется автоматически после смены в кабинете.

### 4.2 Webhook secret

См. раздел 2.3. Хранится в `infra/.env`. Ротация — синхронно сменить в боте и на стороне MAX-кабинета настроек webhook.

### 4.3 Bot ↔ Postgres

`POSTGRES_PASSWORD` через env. URL передаётся как `DATABASE_URL` целиком — пароль внутри. Postgres недоступен снаружи Docker network, перехват возможен только при root-доступе на VPS.

Внутри Docker network — без TLS. Компромисс осознанный: для self-hosted single-VPS уровень «доверенной сети» достаточен. Для повышения — настроить Postgres SSL и `asyncpg ssl=require`.

### 4.4 Bot ↔ Admin group в MAX

Единственная идентификация служебной группы — `ADMIN_GROUP_ID` (отрицательный chat_id MAX). Любой callback с admin-payload'ом, пришедший вне этого chat_id, отбрасывается на этапе `handlers/callback_router.is_admin_callback`.

Идентификация **оператора внутри группы** — по `max_user_id` (MAX user id отправителя), сверяется с таблицей `operators` через `services/operators.get`. Не оператор → команды не выполняются, callback ack'ается без действия.

### 4.5 Роли операторов

В `db/models.OperatorRole` четыре значения:

| Роль | Может |
|---|---|
| `aemr` | отвечать на обращения, `/stats`, `/reopen`, `/close`, `/diag` |
| `egp` | то же, формальное разделение для журнала |
| `coordinator` | всё выше + `/broadcast` |
| `it` | всё выше + `/erase`, `/setting`, `/add_operators`, `/backup` |

Иерархия enforced в коде через `handlers/_auth.ensure_role(OperatorRole.IT, ...)`. Для всех опасных действий (удаление ПДн, регистрация оператора, изменение настроек, бэкап) проверяется именно `it`.

### 4.6 Bootstrap первого `it`

Без `BOOTSTRAP_IT_MAX_USER_ID` в `.env` пустая таблица `operators` не даст `/add_operators` (требует `it`). Поэтому первый `it` подсевается из env при холодном старте. На повторных запусках — no-op. После запуска переменную можно убрать из `.env`, чтобы не оставлять в файле.

## 5. Хранение и обработка ПДн

### 5.1 Где живут ПДн в БД

| Поле | Таблица | Удаляется при `/erase` или `/forget` | Срок жизни без действий |
|---|---|---|---|
| `first_name` | `users` | да (физическое удаление строки) | бессрочно до отзыва |
| `phone`, `phone_normalized` | `users` | да | то же |
| `consent_pdn_at`, `consent_revoked_at` | `users` | строка удаляется | то же |
| `max_user_id` | `users` | строка удаляется | 30 дней после revoke |
| `address` | `appeals` | очищается до NULL | 5 лет после закрытия (потом nullified) |
| `summary` | `appeals` | очищается до NULL | то же |
| `attachments` | `appeals` (JSONB) | очищается до `[]` | то же |
| `text` | `messages` | очищается до NULL | то же |
| `attachments` | `messages` (JSONB) | очищается до `[]` | то же |
| `payload` | `events` (JSONB c chat_id/user_id/mid) | не очищается напрямую | 30 дней (events-retention cron) |
| `operator_max_user_id` | `audit_log` | не очищается | бессрочно (это не житель, а оператор) |

### 5.2 Каналы удаления

**Житель в личке** — команда `/forget`. Аудит-action: `self_erase`.

**ИТ-оператор в служебной группе** — `/erase max_user_id=N` или `/erase phone=+7...`. Аудит-action: `erase`.

Обе команды выполняют одну и ту же модель очистки:

1. Открытые обращения жителя закрываются с флагом `closed_due_to_revoke=true`;
2. Все обращения жителя (любого статуса) перепривязываются к технической записи `ANONYMOUS_MAX_USER_ID = -1` через `UPDATE appeals.user_id`;
3. Свободные текстовые поля и вложения обнуляются в `appeals` и `messages`;
4. Строка жителя в `users` **физически удаляется**;
5. Запись в `audit_log` фиксирует операцию.

Подробно — `docs/RUNBOOK_PDN_ERASURE.md`. Важная оговорка: команды очищают **рабочую базу бота**. Уже отправленные сообщения в MAX-чатах не удаляются (это серверы мессенджера), резервные копии до момента удаления продолжают содержать данные.

### 5.3 Автоматическое 152-ФЗ retention

Cron `pdn-retention` ежедневно в 04:30 Камчатки: жителей, у которых `consent_revoked_at` старше 30 дней, обезличивает через ту же `erase_pdn`. Открытые обращения этих жителей **пропускаются** — сначала оператор должен ответить (право на ответ по 59-ФЗ), обращение закрывается, и тогда retention сработает на следующий день.

Cron `appeals-5y-retention` ежедневно в 04:45: обращения, закрытые более 5 лет назад, теряют `summary`, `attachments`, текст и вложения связанных `messages`. Метаданные (id, дата, статус, тема, поселение) остаются для статистики. Високосные считаются корректно через `relativedelta(years=5)`, а не `timedelta(days=365*5)`.

### 5.4 PII в admin-выводах

В выводах списков жителей в служебной группе телефоны маскируются через `_mask_phone(...)`:

- `+79991234567` → `+7***4567`;
- `89991234567` → `+7***4567`;
- пустые/короткие — пропускаются как есть.

Полный номер показывается оператору только при адресном `/erase phone=...` (он его и так уже знает, потому что вводит).

### 5.5 PII в логах

Бот пишет в логи `max_user_id`, `chat_id`, `mid` (message id) — это **косвенные идентификаторы**, не сам ПДн. Имя, телефон, текст обращения в логи не уходят. Аудит `scripts/audit_vps.sh` маскирует токены и пароли в выгрузке.

Тем не менее, если SIEM подключают — стоит дополнительно фильтровать `audit_log.target` (там могут быть `user max_id=N`) и убедиться, что Docker JSON-логи попадают в среду с разграниченным доступом.

## 6. Хранение секретов

| Секрет | Где | Как читается | Утечка в логи |
|---|---|---|---|
| `BOT_TOKEN` | `infra/.env`, `chmod 600` | env-переменная в контейнере | нет, маскируется в audit |
| `POSTGRES_PASSWORD` | то же | env | нет, маскируется |
| `BACKUP_GPG_PASSPHRASE` | то же | env → `os.pipe` → `gpg --passphrase-fd N` (не argv) | нет, маскируется |
| `WEBHOOK_SECRET` | то же | env, `hmac.compare_digest` | нет |
| `BACKUP_S3_ACCESS_KEY` / `SECRET_KEY` | то же | env → rclone через env, не argv | нет |
| `GITHUB_PAT` (опц., если включён repo-sync) | то же | env → HTTP-заголовок `Authorization: Bearer ...` в `services/repo_sync.py` через aiohttp | нет: PAT передаётся только в headers, не попадает в тело PR/коммита/audit_log; в логе фигурирует только URL созданного PR и счётчик ключей |
| SSH deploy-key | `/root/.ssh/aemr-bot-deploy` (0600), pub в GitHub Deploy keys | используется через `GIT_SSH_COMMAND` | нет |

Никаких секретов:

- в git (есть `.gitignore`);
- в `docker history` (только `COPY` файлов в layers);
- в `ps aux` (везде env, не argv);
- в Docker labels;
- в healthcheck-команде.

GPG-passphrase через `os.pipe()`: бот пишет фразу в pipe через `asyncio.to_thread(os.write, ...)`, дочка-gpg читает через `--passphrase-fd N`. Никогда не argv, никогда не файл.

S3-ключи через env-переменные rclone (`RCLONE_CONFIG_*`), не через `--access-key=...` (этот вариант светится в `ps`).

### 6.1 Ротация секретов

**Зачем.** Любой секрет со временем «протухает»: его мог увидеть бывший сотрудник, он мог осесть в истории терминала, в скриншоте или переписке. Плановая ротация раз в полгода превращает «секрет утёк, и мы не знаем» в «секрет жил максимум полгода». Это не реакция на инцидент, а гигиена.

**Календарь.** Раз в полгода — например, в первую рабочую неделю января и июля. Ответственный — администратор VPS. Каждую ротацию отмечать в журнале эксплуатации (что, когда, кто). Внеплановая ротация — немедленно при подозрении на компрометацию: уволился человек с доступом, утёк ноутбук, секрет засветился в логе или чате.

**Перед началом — снять свежий бэкап** (`/backup` в служебной группе) и убедиться, что есть SSH-доступ на VPS. Все процедуры ниже делаются на VPS под пользователем `aemr` в каталоге `aemr-bot/infra`.

**1. `BOT_TOKEN`.**
1. Зайти на max.ru/business, в настройках бота перевыпустить токен. Старый перестаёт работать сразу.
2. Вписать новый в `infra/.env` (поле `BOT_TOKEN`, без префикса `Bearer`).
3. `docker compose up -d bot` — бот перезапустится с новым токеном (downtime ~минута на пересборку контейнера).
4. Проверить: в служебной группе должен прийти `🔄 Рестарт`-пульс, затем обычный `🟢 Пульс`. Если бот молчит — токен вписан неверно, смотреть `docker compose logs --tail 100 bot`.

**2. `POSTGRES_PASSWORD`.** Пароль хранится в двух местах `.env` и внутри самой БД — менять надо синхронно.
1. Сгенерировать новый: `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`.
2. Сменить пароль внутри Postgres:
   `docker compose exec db psql -U aemr -d aemr -c "ALTER USER aemr PASSWORD 'НОВЫЙ_ПАРОЛЬ';"`
3. В `infra/.env` обновить **оба** поля: `POSTGRES_PASSWORD` и пароль внутри `DATABASE_URL` (`postgresql+asyncpg://aemr:НОВЫЙ_ПАРОЛЬ@db:5432/aemr`).
4. `docker compose up -d` — бот переподключится с новым паролем.
5. Проверить `/livez` и пульс. Если бот в restart-loop — пароль в `.env` и в БД разошлись, сверить оба места.

**3. `BACKUP_GPG_PASSPHRASE`.** Особый случай: старые бэкапы зашифрованы **старой** фразой и новой уже не расшифруются.
1. Сгенерировать новую: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. **Старую фразу не удалять**, а сохранить в надёжном месте (менеджер паролей, сейф) с пометкой даты — она нужна, пока живы бэкапы, сделанные до ротации (хранится 8 еженедельных ≈ 2 месяца).
3. Вписать новую в `infra/.env` (`BACKUP_GPG_PASSPHRASE`), `docker compose up -d bot`.
4. Дождаться ближайшего воскресного бэкапа и проверить, что он расшифровывается новой фразой (см. `docs/BACKUP_RESTORE_TEST.md`).
5. Через 2 месяца, когда все «старые» бэкапы выпали из ротации, старую фразу можно уничтожить.

**Прочие секреты.** `WEBHOOK_SECRET` (если используется webhook-режим), `BACKUP_S3_ACCESS_KEY`/`SECRET_KEY` (если включён S3), SSH deploy-key — ротируются по тому же полугодовому календарю. Webhook-secret и S3-ключи: сгенерировать новые, обновить `.env` (для S3 — ещё и в панели провайдера), перезапустить. SSH deploy-key: сгенерировать новую пару, заменить публичную часть в GitHub → Deploy keys, приватную — на VPS в `/root/.ssh/aemr-bot-deploy`.

**`GITHUB_PAT` (если включён repo-sync).** Fine-grained Personal Access Token со сроком жизни обычно 90 дней — рекомендую ротировать не реже чем раз в 90 дней (по календарному напоминанию провайдера или собственному). Процедура: GitHub → Settings → Developer settings → Fine-grained tokens → создать новый с теми же правами (Contents:RW + PullRequests:RW на `Gaben1488/aemr-bot`), вписать в `infra/.env`, `docker compose up -d bot`. Старый PAT отозвать в той же панели сразу после успешного теста синхронизации новым. Если в audit_log GitHub видны действия от истёкшего токена — это нормально (token revoked != actions disowned), но дальнейших операций он сделать не сможет.

## 7. Защита от ввода и злоупотреблений

### 7.1 Rate limiting

- **Новые обращения**: 3 в час на жителя. Защита от спам-волны и от случайного фарма обращений. При превышении бот предлагает дополнить открытое или подождать.
- **Followup-флуд (SEC #5)**: на одно обращение — не больше `FOLLOWUP_MAX_PER_HOUR_PER_APPEAL` (default 5) дополнений в час, минимум `FOLLOWUP_MIN_INTERVAL_SECONDS` (default 30) секунд между двумя. Каждое дополнение публикует ПОЛНУЮ admin-карточку с relay вложений — без лимита один житель мог бы залить чат сотнями карточек за минуту.
- **Webhook concurrency**: `asyncio.Semaphore(32)` — одновременно обрабатывается до 32 событий. Защита от unbounded task spawn при флуде MAX → OOM при `mem_limit:512m`.
- **Broadcast rate-limit**: `BROADCAST_RATE_LIMIT_PER_SEC=1.0` по умолчанию (ниже потолка MAX 2 RPS, чтобы рассылка не упиралась в лимит одновременно с обычной работой).
- **Template apply dedupe (SEC P3 #25)**: повторный тап «📨 Применить» на одном шаблоне в 3-секундное окно — тихий ack без побочных эффектов (без двойного `record_usage`).

### 7.2 Дедупликация и идемпотентность

- Каждое входящее событие MAX сохраняется в `events.idempotency_key` (UNIQUE). Повтор того же `update_id` — no-op.
- **Fail-closed (SEC #7)**: при любой ошибке записи idempotency-ключа (DB stall, transient timeout) событие **отбрасывается** и не обрабатывается. До SEC #7 был fail-open («default to process»): это позволяло атакующему через искусственный DB stall реплеить мутирующие callback'и.
- Reply-dedup: ответ оператора с тем же `(operator_id, appeal_id, text)` в окне 10 секунд игнорируется. Защита от двойного клика «Ответить» + параллельной команды `/reply`.

### 7.3 HTML escape

В прогресс-карте воронки `services/progress.py` все значения от жителя (`name`, `address`, `locality`, `topic`) пропускаются через `html.escape` перед вставкой в HTML-разметку. Имя `<script>alert(1)</script>` рендерится как видимый текст, а не активный тег.

MAX использует `format=ParseMode.HTML` — без escape это была бы уязвимость к подмене разметки/контента.

### 7.3.1 Защита от marker spoofing в swipe-reply (SEC #3)

`handlers/operator_reply` поддерживает fallback-определение appeal'а по маркеру `🆔 №N` в тексте сообщения, на которое сделан swipe-reply. До SEC #3 marker принимался из любого сообщения — оператор-соседа в чате, citizen-текста (вставлено в текст обращения), служебной заметки. Это позволяло перенаправить операторский ответ на чужого жителя.

После SEC #3 marker принимается **только если** sender оригинального сообщения — это сам бот (`sender.is_bot=True`). Если sender неизвестен или не-бот — marker игнорируется, в лог пишется warning. Для legacy-сценариев есть явный путь через `/reply <appeal_id> <текст>`.

### 7.3.2 Whitelist URL-настроек (SEC #4)

`services/settings_store._is_whitelisted_url` ограничивает значения URL-ключей (`policy_url`, `electronic_reception_url`, `udth_schedule_url`, `udth_schedule_intermunicipal_url`) хостами из gov-домена: `elizovomr.ru`, `kamgov.ru`, `gosuslugi.ru`, `kamchatka.gov.ru` (включая поддомены). Защита от phishing-URL, который мог бы подставить rogue/compromised IT-оператор через UI настроек — citizens click trusted govbot link и попадают на чужой сайт. Новые официальные домены добавляются правкой `_URL_HOST_WHITELIST_SUFFIXES` в коде, не через UI.

### 7.3.3 Operator deactivation re-check (SEC #6)

`handlers/operator_reply._deliver_operator_reply` ре-проверяет `operators_service.get(max_user_id).is_active` непосредственно перед доставкой ответа жителю. До SEC #6 деактивированный оператор с активным reply-intent мог отправить ещё один ответ — intent кешировал старый operator-объект. После SEC #6 свежее чтение из БД на каждый ответ; deactivated оператор получает «⚠️ Ваша роль оператора деактивирована. Ответ НЕ отправлен», житель ничего не получает.

### 7.3.4 Защита от self-unblock через stale consent button (SEC #1)

`handlers/appeal._cb_consent_yes` явно проверяет `user.is_blocked` перед запуском воронки. До SEC #1 нажатие старой кнопки «✅ Согласен» (оставшейся в истории чата с прошлого визита) сбрасывало `is_blocked=False` через `set_consent` — blocked житель снимал себе блок сам. После SEC #1 `set_consent` больше не трогает `is_blocked`; разблокировка только через IT-операцию (audience UI или admin appeal card).

### 7.4 Защита состояния FSM

`services/users.update_dialog_data` использует `pg_advisory_xact_lock(max_user_id)` (Postgres-блокировка по числовому ключу в рамках транзакции) перед read-modify-write на `dialog_data`. Два одновременных callback'а одного жителя сериализуются Postgres'ом — никакого lost update.

### 7.5 Postgres-уровень

Миграция 0010 устанавливает на уровне БД:

- `statement_timeout = '30s'` — любой запрос дольше 30 секунд абортится автоматически;
- `idle_in_transaction_session_timeout = '60s'` — забытая открытая транзакция (баг или crash на Python-стороне) убивается через минуту, освобождая локи;
- `pg_stat_statements` extension — видимость в топ-N медленных запросов.

### 7.6 Misfire grace для cron

У всех scheduled jobs `misfire_grace_time = 120` секунд — параметр APScheduler, сколько времени cron-задача готова «догнать» пропущенный запуск (см. `services/cron._MISFIRE_GRACE_SEC`). Дефолт APScheduler — 1 секунда; пропуски во время `docker compose up --build` (30–90 сек) молча терялись. С 120-секундным окном пропущенный тик догоняется при возвращении процесса.

## 8. Аудит и наблюдаемость

### 8.1 Audit log

Таблица `audit_log` пишется на каждое чувствительное действие оператора: `block`, `unblock`, `erase`, `self_erase`, `reopen`, `close`, `broadcast_started`, `operator_upsert`, `setting_changed` и т. п.

Поля:

- `operator_max_user_id` — кто сделал;
- `action` — что;
- `target` — над кем/над чем (например `user max_id=123`, `appeal #45`);
- `details` JSONB — параметры;
- `created_at`.

Audit-log автоматически очищается cron-job'ом `audit-log-retention` (ежедневно 04:15 Камчатки) по `AUDIT_LOG_RETENTION_DAYS` (default 365, диапазон 30–3650). Записи старше cutoff удаляются вместе с любым PII в `details` (например, `setting_update` хранит `before`/`after` значения настроек — для расследования инцидента в окне ретеншена, но не вечно).

Глубина retention рассчитана на типовое расследование инцидента (год). При необходимости — поднять `AUDIT_LOG_RETENTION_DAYS` через env.

### 8.2 События входящих апдейтов

Таблица `events` — все входящие апдейты MAX за последние 30 дней (cron `events-retention` чистит старше). Используется как idempotency-store и для дебага.

### 8.3 Внешний мониторинг

`HEALTHCHECK_URL` в `.env` — опциональный URL внешнего ping-сервиса (Healthchecks.io, Uptime Kuma и аналоги). Бот пингует его раз в `HEALTHCHECK_INTERVAL_MIN` минут. Отсутствие пинга в течение N интервалов = alert админу через канал внешнего сервиса (не через бот, который мёртв).

`scripts/healthwatch.sh` — внешний watchdog в cron `*/5`, опрашивает `/livez`. 8 фейлов подряд = алёрт прямо в служебную группу через MAX API, в обход бота.

## 9. Резервные копии

Полная политика — `docs/RUNBOOK_PDN_ERASURE.md` §6, `docs/BACKUP_RESTORE_TEST.md`.

Ключевое:

- backups шифруются GPG `--symmetric AES256`. Если `BACKUP_GPG_PASSPHRASE` пустой ИЛИ короче 12 символов — бэкап **отказывается** запускаться (SEC #2): дамп содержит phones / имена / тексты обращений / audit-log, plain `.sql` на диске или в S3 = breach 152-ФЗ. Открыть plain-режим можно только явным `BACKUP_ALLOW_UNENCRYPTED=1` (dev/local-only). В production passphrase обязателен;
- хранится 8 последних файлов (≈ 2 месяца истории);
- опционально дополнительная копия в S3-совместимое хранилище;
- restore-тестирование — в отдельной БД, не в production. Тест регулярно по графику (минимум раз в квартал).

**Юридический риск**: backup от даты до `/erase` всё ещё содержит данные жителя. После восстановления из старого backup'а необходимо повторно применить все актуальные удаления (`erase_pdn`) для жителей с `consent_revoked_at`, иначе данные возвращаются в production. Это формальное нарушение 152-ФЗ.

Минимальное правило: после restore из backup'а старше суток — сразу прогнать `pdn_retention_check` вручную через psql или дождаться cron в 04:30.

## 10. CI как security gate

`.github/workflows/ci.yml` запускается на каждый push в main и каждый PR. **Hard fail** на любом из:

- `ruff check` — линт со стилем кода;
- `mypy` — типы;
- `bandit -ll` — статический security-анализатор Python (medium+);
- `pip-audit --strict` — CVE-сканер зависимостей. Падает на любой known CVE в `pip freeze`;
- `shellcheck` — все `*.sh`;
- `pytest --cov-fail-under=65` — coverage gate;
- `alembic upgrade head` — миграции применяются на чистую БД;
- `alembic check` — соответствие модели Python и схемы БД;
- `alembic round-trip` — upgrade head → downgrade base → upgrade head без ошибок (ловит сломанный `downgrade()`).

Артефакт `pip-audit-report` сохраняется на каждый run для последующего ревью.

Auto-deploy на сервере подтягивает **только** из `main`. PR-ветки не деплоятся. Это значит, что красный CI блокирует деплой на уровне ветки.

## 10a. Закрытые уязвимости и инварианты (history)

Перечисляются явно, чтобы при следующем аудите видно было, какие угрозы уже закрыты в коде и регрессионно покрыты тестами.

### Серия SEC (security audit, май 2026)

| # | Класс | Что было | Что закрыто |
|---|---|---|---|
| 1 | 🔴 escalation | Заблокированный житель мог нажать на старую кнопку «✅ Согласен» в кэше клиента MAX и сбросить себе `is_blocked` через consent-flow | `services/users.set_consent` отказывается выставлять согласие, если `is_blocked=true`; кнопка консента в свежей карточке скрывается |
| 2 | 🔴 152-ФЗ | `pg_dump` записывался в named volume **plain `.sql`** — содержит телефоны и тексты обращений; нарушение 152-ФЗ при snapshot'е VPS | `services/db_backup.backup_db` требует `BACKUP_GPG_PASSPHRASE` (≥12 симв) и пишет только `.sql.gpg`; явный bypass `BACKUP_ALLOW_UNENCRYPTED=1` доступен только для dev-машины |
| 3 | 🔴 spoofing | Оператор мог пристроить произвольный «🆔 №N» в текст ответа жителю свайпом — citation-marker не валидировался | `services/card_format` строит маркер из `Appeal.id` строго на сервере; рекомендации `ANSWER_MAX_CHARS` обрезаются перед публикацией |
| 4 | 🟡 SSRF | `policy_url`, `electronic_reception_url`, `udth_schedule_url` принимали любой URL без валидации — IT-оператор мог сослать жителя на phish-домен | `services/settings_store.validate_url` использует whitelist (https-only, doc-allowlist хостов АЕМО/elizovomr/max.ru); ошибка валидации показывается в UI без редеплоя |
| 5 | 🟡 DoS | Дополнение (followup) к закрытому обращению можно было присылать без rate-limit — флуд оборачивался флудом карточек в служебной группе | `handlers/operator_reply._enforce_followup_rate_limit` ограничивает житель → 5 followup/час; над лимитом — сообщение «лимит исчерпан, дождитесь» без новой карточки |
| 6 | 🟡 race | При параллельной деактивации оператора `services/operators.deactivate_operator` мог пропустить проверку «единственный активный `it`» | Транзакция переведена в `SELECT FOR UPDATE` по таблице `operators`, проверка `active_it_count` идёт строго под локом; UI показывает явный отказ |
| 7 | 🟡 fail-open | `services/idempotency.claim` при ошибке БД пропускал событие как «новое» — двойная обработка update | Сбой БД конвертируется в `IdempotencyUnavailable`; вышестоящий middleware ack'ает callback и **не запускает handler** — fail-closed |
| 8 | 🟡 PII | Exception при backup'е форматировался с полным traceback (потенциально путь к `pg_dump` с DSN) и отправлялся в служебную группу | `services/db_backup` категоризирует ошибки (`pg_dump`/`gpg`/`config`/`s3`) и передаёт админу только enum + safe message; полный traceback — только в локальные docker-логи |
| 9 | 🟡 authz | `/reply` имела две точки проверки прав (callback + текст) с расходящейся логикой — coordinator мог получить отказ на свайпе, но ответить через команду | `handlers/admin_appeal_ops.cmd_reply` и `operator_reply.handle_operator_reply` теперь проходят единый `_auth.ensure_operator`; разница ролей только при `/broadcast` |

### Серия SACRED (admin-bus invariants, май 2026)

| # | Класс | Что было | Что закрыто |
|---|---|---|---|
| 1 | 🔴 invariant | admin-карточки и админ-уведомления рендерились разными путями (`menu.send_to_admin_card`, `admin_card.render`, `admin_events.send_*`) — freshness-tracker терял часть сообщений | Единая шина `services/admin_card.render` + `services/admin_events.send_event`; menu-tracker автоматически записывает каждое исходящее сообщение в admin-группу |
| 2 | 🔴 broadcast | Progress-бар рассылки пытался редактировать «вечный» admin-mid — после нескольких рассылок MAX начинал возвращать ошибки edit; freshness нарушался | `handlers/broadcast` отправляет progress отдельным sacred-сообщением с новым `mid`; финальная сводка edit'ит **только** этот mid |
| 3 | 🔴 sync | Входящие сообщения операторов в группе не попадали в menu-tracker — следующий `render_admin_card` мог попытаться edit'нуть старый mid вместо отправки нового | `utils/menu_tracker.note_incoming_admin_message` вызывается из `handlers/appeal.on_message` и `operator_reply.handle_operator_reply` |
| 4 | 🔴 UX | Нажатие «Ответить» edit'ило ту же карточку с prompt'ом — оператор терял контекст | `handlers/admin_appeal_ops.reply_intent` отправляет prompt **отдельным новым сообщением** (force_new_message=True); freshness переключается на него |
| 5 | 🔴 empty | `list_unanswered_with_messages` отсекал обращения с пустой связкой `messages` — карточка приходила без timeline | Запрос переведён на `LEFT JOIN` + `selectinload(Appeal.messages)`, фильтр по последнему оператору в timeline |
| 6 | 🔴 freshness | Freshness-rule учитывал только сообщения в админ-группе — сообщения жителя в личке после reply не сбрасывали freshness, edit карточки уходил в «протухший» MID | `utils/menu_tracker.note_incoming` теперь принимает все источники: житель в личке, оператор в группе, callback в группе |

Все 15 фиксов покрыты регрессионными тестами в `bot/tests/test_reliability_pass.py`, `test_appeal_card_edit_policy.py`, `test_admin_card_render.py`, `test_idempotency.py`, `test_db_backup.py`, `test_funnel_state_hardening.py`, `test_admin_events.py`, `test_operator_reply_with_image.py`.

### Operator-facing документация

Практическая инструкция оператору по ИБ, антифишингу, ответственности по 152-ФЗ и комплаенсу с Регламентом v7 — отдельный документ: [`docs/OPERATOR_SECURITY.md`](OPERATOR_SECURITY.md). Обязателен к прочтению до первой смены. UI бота в hot-path (broadcast wizard, reply intent) содержит ссылки на конкретные параграфы этого документа.

### Серия SECURITY_REVIEW 2026-05-26 (полный пасс с 4 параллельными агентами)

Сводный отчёт: [`docs/_meta/SECURITY_REVIEW_2026-05-26.md`](_meta/SECURITY_REVIEW_2026-05-26.md). Детальные находки по доменам: `SEC_INVENTORY`, `SEC_MAX_THREATS`, `SEC_SCAM_VECTORS`, `SEC_EXPLOITS`.

| # | Категория | Что | Как защищено |
|---|---|---|---|
| H1 | 🟠 PR injection | `operator_name` в GitHub PR body — markdown stuffing `## Maintainer note` | `_sanitize_for_pr_body` (newline→space, backtick→ˋ, trunk 120) |
| H2 | 🟠 root-cron shell injection | `healthwatch.sh` тянет BOT_TOKEN / ADMIN_GROUP_ID из `.env` без quote | regex validate перед curl (exit 2 если формат сломан) |
| M1 | 🟡 PII в логах | geo-callback payload (координаты жителя) на info-уровне docker | `appeal.py:490,492` → debug-only, без значения payload |
| M2 | 🟡 stale operators | оператор покинул admin-группу — остаётся `is_active=true` | новый cron `stale-operators-cleanup` 04:20, IT-роль защищена от self-lock-out |
| M3 | 🟡 outgoing URL фишинг | оператор может вписать жителю любую URL в ответе | `find_non_whitelisted_urls` в `operator_reply` — блокировка доставки + admin notice |
| M4 | 🟡 phone format | `emergency_contacts.phone` принимал любой текст (premium-номер) | regex `^[\d\s\+\-\(\)\.]{2,40}$` в validate |
| M5 | 🟡 followup URL warning | житель/оператор кликает на ссылку в admin-карточке | warning «не открывайте напрямую» если URL в summary/followup |
| M7 | 🟡 letsencrypt shell | `init-letsencrypt.sh` DOMAIN/EMAIL в `--entrypoint "..."` | regex validate до docker compose run |
| C1 | 🔴 welcome dormant | IT редактирует welcome/consent через UI, житель видит hardcoded | `get_text_with_fallback` + `sanitize_settings_text` (HTML/JS вырезаются, не-whitelisted URL → label only) |
| C2 | 🔴 broadcast spoofing/ошибка | один confirm = моментальная рассылка всем подписчикам | URL-whitelist на текст + cooldown 5 мин (30 сек для `[ЧС]`-маркера) с возможностью отмены |
| C4-6 | 🔴 социалка | scam/impersonation/MAX attachment leak | `WELCOME` блок «Что бот НИКОГДА не запрашиваем» + Политика §6.7 |

### Accept / known limitations 2026-05-26

| # | Что | Почему accept |
|---|---|---|
| C3 | Operator 2FA / PIN отсутствует | Принят владельцем 2026-05-26 как избыточное усложнение для гос-канала, где компрометация оператора маловероятна. Mitigation: audit_log + быстрая деактивация другим IT через wizard. Возврат к решению — при изменении threat model. |
| M1b/c | max_user_id в логах + docker json-file logs переживают `/erase` | max_user_id — псевдоидентификатор по 152-ФЗ, нужен для дебага. Docker logs — настроена log rotation 10MB×3 (см. SYSADMIN §12b), ручной truncate при `/erase` в RUNBOOK. |
| M6 | TLS pinning к `*.max.ru` отсутствует | Системный CA + ICA достаточны для self-host'а. Pinning добавляет операционный риск при ротации сертификата. |
| M8 | GitHub API response без full schema | `.get()` с дефолтами безопасен; full schema = overengineering для одного использования. |
| M9 | `/export` без size-limit | LIMIT 500 уже есть в коде; реальный риск OOM минимален. Streaming-export — отдельный track. |
| M10 | `/setting json.loads` extras | Известное проектное решение (forward-compat: новые поля в seed не должны ломать validate). |

## 11. Известные ограничения и компромиссы

Перечисляю явно, чтобы при аудите не было сюрпризов:

1. **Single-replica HA отсутствует**. Одна VPS, одна реплика бота, одна Postgres. Падение хоста = недоступность сервиса до восстановления. Восстановление: либо новый VPS + restore из backup, либо ручное вмешательство.

2. **In-memory state частично переживает рестарт, частично нет**. Wizard-state операторов (регистрация нового сотрудника, мастер рассылки) сохраняется в таблицу `wizard_state` (миграция 0011) и восстанавливается на старте. Дедуп-окно (10 сек) и кэш-блокировки на жителя — только в RAM, при рестарте обнуляются.

3. **TLS внутри Docker network не настроен**. Bot↔db общаются по нешифрованному TCP внутри `aemr-bot_default`. Уровень доверия — «доверенная сеть Docker». Для повышения — Postgres SSL.

4. **Backup до `/erase`**. Юридический разрыв между моментом удаления ПДн в рабочей БД и наличием тех же данных в backup'е. Документировано в `RUNBOOK_PDN_ERASURE.md`.

5. **MAX-чат бота с жителем**. Бот не может удалить уже отправленные сообщения из MAX, они остаются на серверах мессенджера и подчиняются политике MAX, а не нашему 152-ФЗ обязательству. Это нужно явно сказать жителю в политике приватности.

6. **`mypy --strict` не включён**. Типы проверяются базово; full-strict потребует hint'ы в 50+ функциях, отложено.

7. **DAST/penetration test не проведён**. Static-analysis (`bandit`, `pip-audit`) есть; динамического тестирования (фаззинг webhook, brute callback-payload'ов) нет. Рекомендую при бюджете.

8. **Compliance аудит формальный документ**. Соответствие 152-ФЗ описано в коде и в `RUNBOOK_PDN_ERASURE.md`. Формального чек-листа Роскомнадзора, подписанного юристом, в репозитории нет. Это задача компетенции владельца.

## 12. Что хотелось бы получить от ИБ-команды

Если документ читает безопасник, у которого есть бюджет и желание помочь:

- **Iptables outbound whitelist** на VPS: разрешить только три исходящих адреса (раздел 2.2), всё остальное — drop. Защита от exfiltration в случае компрометации.
- **Аудит .env** на ротацию: BOT_TOKEN, POSTGRES_PASSWORD, GPG passphrase менялись ли когда-нибудь? Заведите календарь ротации (раз в полгода).
- **Penetration test webhook** (если включён): фаззинг payload'ов, попытки обхода `hmac.compare_digest`, brute force `WEBHOOK_SECRET`.
- **Compliance аудит**: формальное соответствие 152-ФЗ и 59-ФЗ. Юрист, не разработчик.
- **DR plan**: что делает админ, если VPS уничтожена полностью. Сейчас плана нет, кроме «развернуть из репо и restore последний бэкап» — это правильно, но не проверено учением.

## 13. Контакты

Компрометация секретов или критический инцидент → владелец проекта (управление цифровизации Администрации ЕМО). Ответственный за ПДн — на стороне Администрации, контакт в политике приватности.
