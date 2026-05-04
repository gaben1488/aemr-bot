# Гайд для разработчика

Вы только что клонировали репозиторий и хотите поднять бота локально, потом внести правку и проверить, что ничего не сломалось. Этот документ — кратчайший путь от нуля до работающего бота.

## Минимальные требования

- Windows 10/11, macOS 14+ или Linux (с Docker Desktop / docker-engine).
- **Docker Desktop** с включённым WSL2-бэкендом (для Windows). Memory не меньше 4 ГБ.
- **Git** с настроенным GitHub-доступом. Репозиторий приватный, нужен либо токен, либо SSH-ключ.
- **Python 3.12** на хосте. Нужен только для скриптов в `scripts/` и pytest. Сам бот живёт в контейнере.
- Аккаунт в мессенджере **MAX** для тестирования.

Установка Docker и WSL пошагово описаны в `docs/RUNBOOK.md` раздел 5. Здесь не дублируем.

## Шаг 1 — клонировать и подготовить .env

```bash
git clone https://github.com/gaben1488/aemr-bot.git
cd aemr-bot/infra
cp .env.example .env
```

Откройте `.env` в редакторе и заполните **только три строки** для локального теста:

```
BOT_TOKEN=<токен с max.ru/business → раздел «Боты»>
POSTGRES_PASSWORD=local-test-pass
DATABASE_URL=postgresql+asyncpg://aemr:local-test-pass@db:5432/aemr
```

Остальные параметры оставьте как есть. `BOT_MODE=polling`, `WEBHOOK_*`, `BACKUP_*`, `HEALTHCHECK_URL`, `ADMIN_GROUP_ID` пока пустые. Заполните их после первого запуска.

**Получить тестовый токен.** Откройте <https://max.ru/business>. Войдите как админ организации. Раздел «Боты» → «Создать бота» (или откройте существующего) → скопируйте Bot API token. Если у вас уже есть основной бот АЕМР, попросите у владельца сгенерировать отдельный тестовый.

## Шаг 2 — собрать и запустить

```bash
cd aemr-bot/infra
docker compose up --build bot
```

Первая сборка занимает 3–5 минут. Ставит Python-зависимости, тянет `python:3.12-slim`. Дальше пересборки занимают секунды. По окончании в логах увидите:

```
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, initial schema
INFO aemr_bot.health healthcheck listening on 0.0.0.0:8080/healthz
INFO aemr_bot.services.policy policy PDF uploaded; token cached
INFO aemr_bot Starting in long polling mode
INFO dispatcher Бот: @<ваш_бот> first_name=... id=...
```

Если что-то падает, откройте `docs/RUNBOOK.md` раздел «Что делать если бот молчит». Самые частые причины: тайм-аут pull от Docker Hub (повторить), забит диск (`docker system prune -a -f`), битый WSL2 (Settings → Troubleshoot → Clean / Purge data).

## Шаг 3 — поговорить с ботом

В MAX найдите своего тестового бота и нажмите «Старт». Должно прилететь приветствие и пять кнопок главного меню. Пройдите воронку: «Написать обращение» → «Согласен» (PDF приложен) → «Поделиться контактом» → имя → адрес → тематика → суть текстом → «Отправить». Бот ответит «Обращение #1 принято».

Чтобы карточка ушла в админ-группу, нужно настроить `ADMIN_GROUP_ID` и зарегистрировать себя как первого ИТ-оператора. Создайте в MAX группу. Добавьте туда бота. В группе напишите `/whoami`. Бот вернёт `chat_id` группы и ваш `max_user_id`. Эти числа кладёте в `.env`:

```
ADMIN_GROUP_ID=-1001234567890
BOOTSTRAP_IT_MAX_USER_ID=165729385
BOOTSTRAP_IT_FULL_NAME=Иванов И.И.
```

Перезапустите бота: `docker compose up -d --force-recreate bot`. На старте бот сам вставит запись в `operators` с ролью `it`, если её ещё нет. Теперь после прохождения воронки в группу прилетит карточка обращения. Свайп-реплай на неё — ответ пойдёт жителю. Альтернатива — команда `/reply <номер> <текст>` в группе.

## Шаг 4 — структура проекта

```
aemr-bot/
├─ bot/aemr_bot/           Python-пакет
│  ├─ main.py              точка входа, переключатель polling/webhook, recover_after_restart
│  ├─ config.py            Settings из .env (Pydantic), валидаторы
│  ├─ health.py            /healthz + Heartbeat singleton
│  ├─ texts.py             все тексты, которые отправляет бот
│  ├─ keyboards.py         inline-клавиатуры (главное меню, подменю, op_help)
│  ├─ db/
│  │  ├─ models.py         9 таблиц SQLAlchemy (см. db-schema.md)
│  │  ├─ session.py        async-engine, session_scope
│  │  └─ alembic/          миграции (0001_initial, 0002_broadcast)
│  ├─ handlers/
│  │  ├─ __init__.py       register_handlers + IdempotencyMiddleware
│  │  ├─ _auth.py          ensure_operator / ensure_role / get_operator
│  │  ├─ start.py          /start, /menu, /help, /forget, /policy, /subscribe, /unsubscribe, /whoami
│  │  ├─ menu.py           главное меню, Мои обращения, подменю «Полезная информация»
│  │  ├─ appeal.py         пошаговая анкета обращения (per-user lock, _drop_user_lock)
│  │  ├─ operator_reply.py ответ через свайп и /reply, citizen-followup
│  │  ├─ broadcast.py      двухшаговый диалог /broadcast, прогресс-бар, экстренный стоп
│  │  └─ admin_commands.py /stats, /reopen, /close, /erase, /setting, /add_operators,
│  │                       /diag, /op_help, /backup, /op_help callback'и
│  ├─ services/
│  │  ├─ users.py          CRUD пользователя + операции пошаговой анкеты, find_by_phone, erase_pdn
│  │  ├─ operators.py      регистрация операторов + audit_log
│  │  ├─ appeals.py        CRUD обращений, find_active_for_user, get_by_admin_message_id
│  │  ├─ broadcasts.py     create/start/finish, deliveries, subscribers
│  │  ├─ card_format.py    форматирование карточки, обёртка официального письма для жителя
│  │  ├─ stats.py          формирование XLSX через openpyxl
│  │  ├─ policy.py         кеш токена PRIVACY.pdf, build_file_attachment
│  │  ├─ uploads.py        upload_path / upload_bytes / build AttachmentUpload
│  │  ├─ idempotency.py    отбраковка дублей Update-ов через events
│  │  ├─ settings_store.py редактируемые из админки настройки + DEFAULTS
│  │  └─ cron.py           APScheduler: db-backup, monthly-stats, healthcheck-pulse
│  └─ utils/
│     ├─ event.py          адаптер над maxapi event-объектами, is_admin_chat,
│     │                    extract_message_id, get_message_link
│     └─ attachments.py    парсинг VCF и сериализация attachments
├─ infra/
│  ├─ Dockerfile           python:3.12-slim, закреплённый по digest
│  ├─ docker-compose.yml   db + bot (+ nginx + certbot в профиле webhook)
│  ├─ nginx/feedback.conf  reverse-proxy для серверного режима связи
│  ├─ certbot/             конфиг Let's Encrypt
│  ├─ init-letsencrypt.sh  первое получение сертификата
│  └─ .env.example         шаблон со всеми ключами и комментариями
├─ seed/                   topics.json, contacts.json, transport_dispatchers.json,
│                          welcome.md, consent.md, PRIVACY.pdf
├─ scripts/
│  ├─ generate_privacy_pdf.py    повторная генерация PRIVACY.pdf из PRIVACY.md
│  └─ reset_test_data.sql        полная зачистка тестовых данных перед prod
└─ docs/                   ADR-001, PRD-mvp, PRIVACY, SETUP, RUNBOOK, DEVELOPER, db-schema
```

## Где править что

| Хочу сделать | Файл |
|---|---|
| Изменить текст приветствия / шага анкеты / ошибки | `bot/aemr_bot/texts.py` |
| Поменять кнопки или клавиатуры | `bot/aemr_bot/keyboards.py` |
| Добавить новый шаг в анкету обращения | `bot/aemr_bot/handlers/appeal.py`. Новый state в `DialogState`, функция `_on_<state>`, строка в `_STATE_HANDLERS` |
| Добавить операторскую команду | `bot/aemr_bot/handlers/admin_commands.py` |
| Поменять контакты, расписание, ссылки | через `/setting` в админ-группе **либо** в `seed/contacts.json` (подтянется только при пустых settings) |
| Изменить лимиты или таймауты | `bot/aemr_bot/config.py` (с alias-ом для .env) и `infra/.env.example` |
| Добавить новую таблицу или поле | `bot/aemr_bot/db/models.py` + миграция через Alembic |
| Сменить версию зависимости | `bot/pyproject.toml` (compatible-release `~=`) |
| Поменять политику конфиденциальности | `docs/PRIVACY.md` → `python scripts/generate_privacy_pdf.py` → закоммитить `docs/PRIVACY.pdf` |

## Миграции базы данных

Миграции — это изменения схемы БД с версионностью. Управляет ими Alembic.

```bash
# Сгенерировать новую миграцию из изменений в models.py
docker compose exec bot alembic revision --autogenerate -m "describe what changed"

# Накатить
docker compose exec bot alembic upgrade head

# Откатить на одну
docker compose exec bot alembic downgrade -1
```

После генерации **обязательно прочитайте файл миграции**. Автоматическая генерация иногда ошибается с типами JSONB и enum-полями. Файл попадает в `bot/aemr_bot/db/alembic/versions/`.

## Тесты

```bash
# Локально на хосте (нужен pip install -e ".[dev]" внутри bot/)
cd bot
pytest tests/ -v
```

Сейчас в `tests/` лежат тесты на сервисный слой. Они **не работают на in-memory SQLite** из-за PostgreSQL-specific JSONB. Если нужно гонять, поднимайте локальный Postgres и подменяйте `DATABASE_URL`. Альтернатива — подключить `testcontainers`. Это известное направление развития, см. [ADR-001 §11](ADR-001-architecture.md).

При добавлении новой логики пишите тесты в той же папке. Покрывать: бизнес-сценарии, граничные случаи, пути с проверками безопасности (роли, валидаторы, лимиты).

## Стиль кода

- Python 3.12, type hints везде где возможно.
- `ruff` для линта (конфиг в `pyproject.toml`, line-length 100).
- Не пишем комментарии, объясняющие что делает код. Имена переменных уже это говорят. Пишем только зачем: невидимые ограничения, тонкие инварианты, ссылки на документацию.
- Импорты сверху файла. Импорты внутри функций оправданы только при честных циклических зависимостях.
- Никогда не обращайтесь к `event.chat_id` или `event.user.user_id` напрямую. Всегда через `utils/event.py::get_chat_id` или `get_user_id`. Структуры событий в `maxapi` неоднородны, адаптер их сглаживает.

## Рабочий цикл

1. Создайте ветку: `git checkout -b feat/your-thing`.
2. Внесите правки.
3. Прогон тестов и ручная проверка через docker compose.
4. Коммит сообщением в формате `feat:` / `fix:` / `refactor:` / `docs:` / `chore:`.
5. Push в `origin`. Откройте PR на `main` через `gh pr create`.
6. После одобрения — squash merge в `main`. На сервере: `git pull && docker compose up -d --build bot && docker compose exec bot alembic upgrade head`.

## Известные особенности `maxapi`

`love-apples/maxapi` — Python-библиотека от сообщества для работы с Bot API мессенджера MAX. Удобна, но местами протекает. Модели не покрывают все поля сервера. Имена полей не совпадают с документацией. Некоторые методы возвращают объекты, которых нет в типизации. Ниже грабли, на которые мы наступали. Оставлены здесь, чтобы на них больше никто не наступал.

**1. `Message.link.message.mid`, не `Message.link.mid`.** Когда оператор делает свайп-реплай в админ-группе, в событии приходит `Message.link: LinkedMessage`. Сам `link.mid` пустой. Идентификатор исходного сообщения лежит на один уровень глубже, в `link.message.mid` (внутри вложенного `MessageBody`). Смотрите `_extract_reply_target_mid` и `_mid_from_link` в `handlers/operator_reply.py`. На отдельных версиях клиента pydantic-обёртка превращается в dict. Отсюда двойная попытка чтения.

**2. `bot.send_message(...)` возвращает `SendedMessage`, не `Message`.** У результата нет `.message_id` напрямую. Идентификатор лежит в `result.message.body.mid`. Адаптер `extract_message_id` в `utils/event.py` снимает оба варианта (через цепочку `getattr`) и работает на None-входе тоже. Используется при сохранении `appeals.admin_message_id` и `messages.max_message_id`.

**3. `upload_file` возвращает `AttachmentUpload`, а не плоский dict.** Старая ревизия `maxapi` отдавала `dict`. Текущая отдаёт pydantic-модель. Для отправки в `attachments=[...]` сериализуйте через `.model_dump(by_alias=True)` или конкретные конструкторы (`PhotoAttachment`, `FileAttachment`). См. `services/uploads.py` и `services/policy.py::build_file_attachment`.

**4. Порядок регистрации хендлеров важен.** Диспетчер берёт первый совпавший. Если зарегистрировать `message_created` без фильтра раньше, чем `message_created(Command(...))`, команда не сработает. Все универсальные обработчики регистрируются в `handlers/appeal.py` последними. Команды и callback'и — раньше. Любая правка `register_handlers` в `handlers/__init__.py` требует прогнать проверочный прогон из админ-группы.

**5. Защита потока жителя через `is_admin_chat`.** Обработчики, рассчитанные на жителя (`/start`, главное меню, «Написать обращение»), оборачиваются в `if is_admin_chat(event): return` на уровне регистрации. Иначе оператор, написавший в служебной группе, попадёт в анкету как «житель». Получит приветственное меню и заведётся в `users`. Шаблон вынесен в `utils/event.py::is_admin_chat`.

**6. Защита от повторов через `events.idempotency_key`.** MAX иногда повторно шлёт один и тот же Update (например, при повторном запросе после таймаута). `IdempotencyMiddleware` (промежуточный слой обработки) пишет в `events` уникальный ключ (`update_id` или комбинация полей) и пропускает дубликат. Любое новое поле, способное менять обработку, надо включать в ключ. Иначе тихо потеряем сообщение.

**7. Восстановление застрявших сессий пошаговой анкеты.** Пошаговая анкета — это конечный автомат, она же FSM. Если бот рестартанул в середине анкеты, `dialog_state` пользователя застывает. На старте `recover_after_restart` пробегает по застрявшим сессиям пачками (`RECOVER_BATCH_SIZE`) и шлёт жителю «Бот перезапустился, давайте начнём сначала», обнуляет состояние. Без этого житель будет навсегда залипать в `AWAITING_SUMMARY`.

**8. Отладочный дамп `event.message`.** Когда что-то идёт не так с парсингом события (новые типы вложений, изменение схемы maxapi после релиза), включайте `LOG_LEVEL=DEBUG`. Диспетчер пишет полный pydantic-дамп. Раньше дампили вручную через `log.info(repr(event.message))`. Не делайте так в production-логах — ПДн утекут в журнал.

**9. Блокировка на пользователя в `appeal.py`.** Два сообщения от одного жителя за миллисекунды (свайп с фото и текст) могут параллельно изменить `dialog_data`. `_user_locks: dict[int, asyncio.Lock]` сериализует обработку на уровне пользователя. `_drop_user_lock` удаляет блокировку после завершения, чтобы dict не рос неограниченно.

**10. Тайм-аут опросного режима связи.** `POLLING_TIMEOUT_SECONDS=30` — это серверный timeout `getUpdates`, не интервал между запросами. Чем выше значение, тем меньше пустых обращений к API, когда никто ничего не пишет. Потолок MAX — 90 секунд. На 0 бот будет хлестать API на 2 RPS ограничение скорости и кончит свой бюджет за минуты.

## Известные ограничения и направления оптимизации

Список вещей, которые работают, но при росте нагрузки или объёма данных могут дать о себе знать. Каждый пункт — фиксация осознанного выбора «не сейчас», с триггером, при котором имеет смысл вернуться, и эскизом решения. На MVP с десятками подписчиков и тысячами обращений в год ни один из них не блокирует запуск.

**1. `_run_broadcast_impl` открывает три `session_scope` на каждого получателя.** В `handlers/broadcast.py` цикл отправки тратит транзакцию на проверку статуса (флаг отмены), отдельную — на запись `broadcast_deliveries`, и ещё одну — на периодический `update_progress`. На текущем масштабе (десятки подписчиков, около одной рассылки в неделю) это копейка: суммарный overhead — единицы миллисекунд. **Триггер для фикса:** устойчиво больше 1000 подписчиков или несколько одновременных рассылок. **Как чинить:** батчевать `record_delivery` пачками по 50, статус-флаг проверять не каждую итерацию, а раз в N сообщений. Альтернатива — вынести запись доставок в фоновую таску с очередью.

**2. `handlers/appeal.py::on_callback` — длинная if-цепочка.** Около 200 строк ветвлений по `payload`-префиксам (`menu:`, `consent:`, `info:`, `topic:`, `appeal:show:`, `cancel`, `appeals:page:`). Читается линейно и не тормозит. Но добавлять новый callback приходится в общую кучу. **Триггер для фикса:** появление 5+ новых callback'ов или необходимость промежуточного слоя обработки на каждый payload. **Как чинить:** вытащить в dict `{prefix: handler_fn}` с делегированием, по образцу `_STATE_HANDLERS` в том же файле. Чистый рефакторинг, без изменения поведения.

**3. `_run_broadcast_impl` — около 140 строк, плоская функция.** Содержит и подготовку (заголовок в админ-группу), и main-loop, и финализацию (mark_finished, отчёт). Можно вынести `_post_broadcast_header`, `_send_loop`, `_finalize_broadcast` в отдельные функции. Читаемость улучшится. Сейчас читается линейно сверху вниз и не имеет глубоких ветвлений, поэтому терпимо. **Триггер для фикса:** добавление перед- или после-обработки (например, проверка квоты на оператора, фильтрация подписчиков по тематике).

**4. Анонимные `_(event)` обработчики в `handlers/start.py` и других файлах.** Декораторы `@dp.message_created(Command(...))` оборачивают функции с именем `_`. Удобно для регистрации (одна строка), но в стектрейсе вместо `cmd_start` будет `start.<locals>._`. **Триггер для фикса:** ловля сложного бага с непонятным происхождением. **Как чинить:** именовать каждую функцию по команде (`cmd_start_handler`, `cmd_help_handler` и так далее) и вешать декоратор поверх. Сейчас такая же схема в `appeal.py` и `admin_commands.py`. Однородно.

**5. `_user_locks` живёт в памяти процесса и не разделяется между инстансами.** `dict[int, asyncio.Lock]` в памяти бота. При запуске нескольких реплик гонки между инстансами не закрываются. На MVP — один инстанс, это нормально. **Триггер для фикса:** переход на multi-replica deployment. **Как чинить:** advisory-locks Postgres (`pg_advisory_xact_lock(max_user_id)`) внутри `session_scope` или распределённый Redis-lock. Текущая защита от гонки внутри одного процесса остаётся.

**6. `services/policy.py::ensure_uploaded` грузит PDF при каждом старте, если токен не закеширован.** `policy_pdf_token` хранится в `settings`. Но при первой инсталляции (или после `/setting policy_pdf_token "" `) PDF загружается заново. На каждый старт это секунда лишнего ввода-вывода, копейки. **Триггер для фикса:** требование быстрой новой установки (например, проверка живости в Kubernetes).

**7. Inline русские строки в `handlers/admin_commands.py` (usage-сообщения).** Около десятка коротких подсказок типа «Используйте: /reopen <номер>» лежат прямо в коде, а не в `texts.py`. Это технические строки для оператора, не отображаются жителю. **Триггер для фикса:** третья ревизия операторских команд или появление i18n. **Как чинить:** константы `OP_USAGE_*` в `texts.py`. Сейчас оставлено для локальности — usage и парсинг аргументов читаются вместе.

### Что было в этом списке и закрыто

Несколько ограничений из ранних версий MVP исправлены и теперь не считаются ограничениями.

- **`find_by_phone` full-table scan.** Закрыто миграцией `0003_phone_normalized`. Добавлена индексированная колонка `users.phone_normalized`. `set_phone` и `erase_pdn` поддерживают её в синхронизации. Поиск теперь O(log n) по индексу.
- **`cmd_stats` и `run_stats_today` дублировали обвязку XLSX.** Закрыто общим хелпером `_send_stats_xlsx(event, period, target_chat_id)` в `handlers/admin_commands.py`. Оба пути делегируют ему.
- **Таблица `events` без авто-чистки.** Закрыто APScheduler-job'ом `events_retention` в `services/cron.py`. Ежедневно в 04:00 удаляет записи старше 30 дней.
- **Фиксированный шаг прогресс-бара рассылки.** Закрыто адаптивной формулой `min(progress_update_sec, estimated_total / 10)` в `_run_broadcast_impl`. На короткой рассылке бар обновляется чаще, на длинной — реже, чтобы уложиться в ограничение скорости edit-запросов в MAX.
- **Молчаливый сбой бэкапа.** Закрыто обёрткой `backup_with_alert`. Еженедельный pg_dump, упавший в воскресенье ночью, утром всплывает алертом в админ-группу.
- **`/healthz` отдавал OK при зависшей БД.** Закрыто SELECT 1 в обработчике `/healthz`. Endpoint теперь даёт 503, если БД недоступна, даже когда heartbeat свежий.
- **Bot-контейнер запускался как root, без read-only fs, без resource limits.** Закрыто в `infra/Dockerfile` и `docker-compose.yml`: непривилегированный пользователь `botuser` (UID 1000), `read_only: true`, `tmpfs:/tmp`, `mem_limit: 512m`, `pids_limit: 200`, `cap_drop: ALL`, `no-new-privileges`. Compose-блок `healthcheck` добавлен.
- **Мёртвые экспорты `iter_subscribers`, `is_operator`.** Удалены. Заменены на `list_subscriber_targets` и `_auth.get_operator` соответственно.
- **`/diag` отдавал минимум метрик.** Расширено: жители (всего, подписаны, заблокированы), обращения (всего, в работе), рассылки (✅ done, ⚠️ failed), события (всего, последнее), плюс конфиг.

## Архитектурные диаграммы

Полный набор схем — в [architecture-diagrams.md](architecture-diagrams.md):

- BPMN-схема жизненного цикла обращения от первого `/start` до закрытия.
- Mermaid-flowchart: путь события от MAX до записи в БД (citizen, operator-reply, broadcast).
- Схема базы данных (канонический вариант — в [db-schema.md](db-schema.md)).
- Sequence-диаграмма доставки операторского ответа: свайп-реплай и `/reply N`.

## Полезные ссылки

- Bot API MAX: [`dev.max.ru/docs/chatbots`](https://dev.max.ru/docs/chatbots)
- Исходники `maxapi` (Python-библиотека от сообщества): [`github.com/love-apples/maxapi`](https://github.com/love-apples/maxapi)
- Официальные клиенты MAX: [`github.com/max-messenger`](https://github.com/max-messenger)
- Реестр операторов ПДн (Роскомнадзор): [`pd.rkn.gov.ru/operators-registry`](https://pd.rkn.gov.ru/operators-registry)
- Закон 152-ФЗ «О персональных данных»: [`consultant.ru/document/cons_doc_LAW_61801`](https://www.consultant.ru/document/cons_doc_LAW_61801/)

## Дальнейшее чтение

- `docs/ADR-001-architecture.md` — архитектурное решение и его уточнения после первичной реализации.
- `docs/PRD-mvp.md` — функциональные требования (v6 — production-ready).
- `docs/SETUP.md` — пошаговая настройка админ-группы и регистрация операторов.
- `docs/RUNBOOK.md` — операционная инструкция координатору и ИТ.
- `docs/architecture-diagrams.md` — BPMN, flowchart, sequence-диаграммы.
- `docs/db-schema.md` — схема базы и инварианты.
- `docs/PRIVACY.md` — текст политики ПДн.
