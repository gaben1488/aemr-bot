# Гайд для разработчика

Ты только что клонировал репозиторий и хочешь поднять бот локально, потом внести правку и проверить, что ничего не сломалось. Этот документ — кратчайший путь от нуля до «работает».

## Минимальные требования

- Windows 10/11, macOS 14+ или Linux (с Docker Desktop / docker-engine).
- **Docker Desktop** с включённым WSL2-бэкендом (для Windows). Memory ≥ 4 ГБ.
- **Git** с настроенным GitHub-доступом (репозиторий приватный — нужен либо токен, либо SSH-ключ).
- **Python 3.12** на хосте — нужен только для скриптов в `scripts/` и pytest. Сам бот живёт в контейнере.
- Аккаунт в мессенджере **MAX** для тестирования.

Установка Docker и WSL пошагово описаны в `docs/RUNBOOK.md` раздел 5; здесь не дублируем.

## Шаг 1 — клонировать и подготовить .env

```bash
git clone https://github.com/gaben1488/aemr-bot.git
cd aemr-bot/infra
cp .env.example .env
```

Открой `.env` в редакторе и заполни **только три строки** для локального теста:

```
BOT_TOKEN=<токен с max.ru/business → раздел «Боты»>
POSTGRES_PASSWORD=local-test-pass
DATABASE_URL=postgresql+asyncpg://aemr:local-test-pass@db:5432/aemr
```

Остальные параметры оставь как есть. `BOT_MODE=polling`, `WEBHOOK_*`, `BACKUP_*`, `HEALTHCHECK_URL`, `ADMIN_GROUP_ID` пока пустые — заполнишь после первого запуска.

**Получить тестовый токен:** открой <https://max.ru/business> → войди как админ организации → раздел «Боты» → «Создать бота» (или открой существующего) → скопируй Bot API token. Если у тебя уже есть основной бот АЕМР — попроси у владельца сгенерировать отдельный тестовый.

## Шаг 2 — собрать и запустить

```bash
cd aemr-bot/infra
docker compose up --build bot
```

Первая сборка занимает 3–5 минут — ставит Python-зависимости, тянет `python:3.12-slim`. Дальше пересборки секунды. По окончании в логах увидишь:

```
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, initial schema
INFO aemr_bot.health healthcheck listening on 0.0.0.0:8080/healthz
INFO aemr_bot.services.policy policy PDF uploaded; token cached
INFO aemr_bot Starting in long polling mode
INFO dispatcher Бот: @<твой_бот> first_name=... id=...
```

Если что-то падает — открой `docs/RUNBOOK.md` раздел «Что делать если бот молчит». Самые частые корни: тайм-аут pull от Docker Hub (повторить), забит диск (`docker system prune -a -f`), битый WSL2 (Settings → Troubleshoot → Clean / Purge data).

## Шаг 3 — поговорить с ботом

В MAX найди своего тестового бота, нажми «Старт». Должно прилететь приветствие и три кнопки. Пройди воронку: «Написать обращение» → «Согласен» (PDF приложен) → «Поделиться контактом» → имя → адрес → тематика → суть текстом → «Отправить». Бот ответит «Обращение #1 принято».

Чтобы карточка ушла в админ-группу — нужно настроить `ADMIN_GROUP_ID` и зарегистрировать себя как первого IT-оператора. Создай в MAX группу, добавь туда бота, в группе напиши `/whoami`. Бот вернёт `chat_id` группы и твой `max_user_id`. Эти числа кладёшь в `.env`:

```
ADMIN_GROUP_ID=-1001234567890
BOOTSTRAP_IT_MAX_USER_ID=165729385
BOOTSTRAP_IT_FULL_NAME=Иванов И.И.
```

Перезапусти бот: `docker compose up -d --force-recreate bot`. На старте бот сам вставит запись в `operators` с ролью `it`, если её ещё нет. Теперь после прохождения воронки в группу прилетит карточка обращения. Свайп-reply на неё — ответ пойдёт жителю. Альтернатива — команда `/reply <номер> <текст>` в группе.

## Шаг 4 — структура проекта

```
aemr-bot/
├─ bot/aemr_bot/           Python-пакет
│  ├─ main.py              entrypoint, переключатель polling/webhook, recover_after_restart
│  ├─ config.py            Settings из .env (Pydantic), валидаторы
│  ├─ health.py            /healthz + Heartbeat singleton
│  ├─ texts.py             все тексты, что отправляет бот (humanizer + clarify пройдены)
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
│  │  ├─ appeal.py         FSM-воронка обращения (per-user lock, _drop_user_lock)
│  │  ├─ operator_reply.py reply через свайп и /reply, citizen-followup
│  │  ├─ broadcast.py      /broadcast wizard, прогресс-бар, экстренный стоп
│  │  └─ admin_commands.py /stats, /reopen, /close, /erase, /setting, /add_operators,
│  │                       /diag, /op_help, /backup, /op_help callback'и
│  ├─ services/
│  │  ├─ users.py          CRUD пользователя + FSM-операции, find_by_phone, erase_pdn
│  │  ├─ operators.py      регистрация операторов + audit_log
│  │  ├─ appeals.py        CRUD обращений, find_active_for_user, get_by_admin_message_id
│  │  ├─ broadcasts.py     create/start/finish, deliveries, subscribers
│  │  ├─ card_format.py    форматирование карточки, formal letter wrap для жителя
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
│  ├─ Dockerfile           python:3.12-slim, pinned by digest
│  ├─ docker-compose.yml   db + bot (+ nginx + certbot в профиле webhook)
│  ├─ nginx/feedback.conf  reverse-proxy для webhook-режима
│  ├─ certbot/             конфиг Let's Encrypt
│  ├─ init-letsencrypt.sh  первое получение сертификата
│  └─ .env.example         шаблон со всеми ключами и комментариями
├─ seed/                   topics.json, contacts.json, transport_dispatchers.json,
│                          welcome.md, consent.md, PRIVACY.pdf
├─ scripts/
│  ├─ generate_privacy_pdf.py    regenerate PRIVACY.pdf from PRIVACY.md
│  └─ reset_test_data.sql        полная зачистка тестовых данных перед prod
└─ docs/                   ADR-001, PRD-mvp, PRIVACY, SETUP, RUNBOOK, DEVELOPER, db-schema
```

## Где править что

| Хочу сделать | Файл |
|---|---|
| Изменить текст приветствия / шага воронки / ошибки | `bot/aemr_bot/texts.py` |
| Поменять кнопки или клавиатуры | `bot/aemr_bot/keyboards.py` |
| Добавить новый шаг в воронку обращения | `bot/aemr_bot/handlers/appeal.py`: новый state в `DialogState`, функция `_on_<state>`, строка в `_STATE_HANDLERS` |
| Добавить операторскую команду | `bot/aemr_bot/handlers/admin_commands.py` |
| Поменять контакты / расписание / ссылки | через `/setting` в админ-группе **либо** в `seed/contacts.json` (подтянется только при пустых settings) |
| Изменить лимиты/таймауты | `bot/aemr_bot/config.py` (с alias-ом для .env) и `infra/.env.example` |
| Добавить новую таблицу или поле | `bot/aemr_bot/db/models.py` + миграция через Alembic |
| Сменить версию зависимости | `bot/pyproject.toml` (compatible-release `~=`) |
| Поменять политику конфиденциальности | `docs/PRIVACY.md` → `python scripts/generate_privacy_pdf.py` → закоммитить `docs/PRIVACY.pdf` |

## Миграции БД

```bash
# Сгенерировать новую миграцию из изменений в models.py
docker compose exec bot alembic revision --autogenerate -m "describe what changed"

# Накатить
docker compose exec bot alembic upgrade head

# Откатить на одну
docker compose exec bot alembic downgrade -1
```

После генерации **обязательно прочитай файл миграции** — autogenerate иногда ошибается с типами JSONB и enum-полями. Файл попадает в `bot/aemr_bot/db/alembic/versions/`.

## Тесты

```bash
# Локально на хосте (нужен pip install -e ".[dev]" внутри bot/)
cd bot
pytest tests/ -v
```

Сейчас в `tests/` тесты на сервисный слой. Они **не работают на in-memory SQLite** из-за PostgreSQL-specific JSONB; если нужно гонять — либо поднимай локальный Postgres и подменяй `DATABASE_URL`, либо подключи `testcontainers`. Это известное направление развития, см. [ADR-001 §11](ADR-001-architecture.md).

При добавлении новой логики писать тесты в той же папке. Покрывать: бизнес-сценарии, граничные случаи, security-чувствительные пути (роли, валидаторы, лимиты).

## Code style

- Python 3.12, type hints везде где возможно.
- `ruff` для линта (конфиг в `pyproject.toml`, line-length 100).
- Не пишем комментарии, объясняющие WHAT (имена переменных уже это говорят). Пишем только WHY: невидимые ограничения, тонкие инварианты, ссылки на документацию.
- Импорты сверху файла; inline-импорты только при честных циклических зависимостях.
- Никогда не обращаемся к `event.chat_id` / `event.user.user_id` напрямую — всегда через `utils/event.py::get_chat_id` / `get_user_id`. Структуры событий в `maxapi` неоднородны, адаптер их сглаживает.

## Рабочий цикл

1. Создай ветку: `git checkout -b feat/your-thing`.
2. Внеси правки.
3. Прогон тестов и ручная проверка через docker compose.
4. Коммит сообщением в формате `feat:` / `fix:` / `refactor:` / `docs:` / `chore:`.
5. Push в `origin`, открой PR на `main` через `gh pr create`.
6. После апрува — squash merge в `main`. На сервере `git pull && docker compose up -d --build bot && docker compose exec bot alembic upgrade head`.

## Известные особенности `maxapi`

`love-apples/maxapi` — community-обёртка над Bot API MAX. Удобна, но местами протекает: модели не покрывают все поля сервера, имена полей не совпадают с документацией, а некоторые методы возвращают объекты, которых нет в типизации. Вот граблищи, на которые мы наступали — оставлены здесь, чтобы на них больше никто не наступал.

**1. `Message.link.message.mid`, не `Message.link.mid`.** Когда оператор делает свайп-reply в админ-группе, в событии приходит `Message.link: LinkedMessage`. Сам `link.mid` пуст — id исходного сообщения лежит на один уровень глубже, в `link.message.mid` (у nested `MessageBody`). Видно в `_extract_reply_target_mid`/`_mid_from_link` в `handlers/operator_reply.py`. На отдельных версиях клиента pydantic-обёртка оборачивается в dict — отсюда двойная попытка.

**2. `bot.send_message(...)` возвращает `SendedMessage`, не `Message`.** У результата нет `.message_id` напрямую — id лежит в `result.message.body.mid`. Адаптер `extract_message_id` в `utils/event.py` снимает оба варианта (через `getattr` цепочку) и работает на None-входе тоже. Используется при сохранении `appeals.admin_message_id` и `messages.max_message_id`.

**3. `upload_file` возвращает `AttachmentUpload`, а не плоский dict.** Старая ревизия `maxapi` отдавала `dict`, текущая — pydantic-модель. Сериализация для отправки в `attachments=[...]` идёт через `.model_dump(by_alias=True)` или конкретные конструкторы (`PhotoAttachment`, `FileAttachment`). См. `services/uploads.py` и `services/policy.py::build_file_attachment`.

**4. Порядок регистрации хендлеров важен.** `Dispatcher` берёт первый матч. Если зарегистрировать `message_created` без фильтра раньше, чем `message_created(Command(...))` — команда не сработает. Все универсальные обработчики регистрируются в `handlers/appeal.py` последними; команды и callback'и — раньше. Любая правка `register_handlers` в `handlers/__init__.py` требует прогнать smoke-тест из админ-группы.

**5. Citizen-flow guard через `is_admin_chat`.** Обработчики, рассчитанные на жителя (`/start`, главное меню, «Написать обращение»), оборачиваются в `if is_admin_chat(event): return` на уровне регистрации. Иначе оператор, написавший в служебной группе, попадёт в воронку как «житель», получит welcome-меню и заведётся в `users`. Шаблон вынесен в `utils/event.py::is_admin_chat`.

**6. Идемпотентность через `events.idempotency_key`.** MAX иногда повторно шлёт один и тот же Update (например, при повторном запросе после таймаута). `IdempotencyMiddleware` пишет в `events` уникальный ключ (`update_id` или комбинация полей) и пропускает дубликат. Любое новое поле, способное менять обработку, надо включать в ключ — иначе тихо потеряем сообщение.

**7. Recovery застрявших FSM-сессий.** Если бот рестартанул в середине воронки, `dialog_state` пользователя застывает. На старте `recover_after_restart` пробегает по застрявшим сессиям пачками (`RECOVER_BATCH_SIZE`) и шлёт жителю «Бот перезапустился, давайте начнём сначала», обнуляет FSM. Без этого житель будет навсегда залипать в `AWAITING_SUMMARY`.

**8. Отладочный дамп `event.message`.** Когда что-то идёт не так с парсингом события (новые типы вложений, изменение схемы maxapi после релиза), включай `LOG_LEVEL=DEBUG` — `dispatcher` пишет полный pydantic-дамп. Раньше дампили вручную через `log.info(repr(event.message))`; не делай так в production-логах, ПДн утекут в журнал.

**9. Per-user FSM lock в `appeal.py`.** Два сообщения от одного жителя за миллисекунды (свайп-share-photo + текст) могут параллельно изменить `dialog_data`. `_user_locks: dict[int, asyncio.Lock]` сериализует обработку на уровне пользователя; `_drop_user_lock` удаляет lock после завершения, чтобы dict не рос неограниченно.

**10. Long-polling timeout.** `POLLING_TIMEOUT_SECONDS=30` — серверный timeout `getUpdates`, не интервал между запросами. Чем выше — тем меньше пустых round-trip'ов в idle, потолок MAX — 90 сек. На 0 бот будет хлестать API на 2 RPS rate-limit и кончит свой бюджет за минуты.

## Полезные ссылки

- Bot API MAX: [`dev.max.ru/docs/chatbots`](https://dev.max.ru/docs/chatbots)
- Исходники `maxapi` (community Python lib): [`github.com/love-apples/maxapi`](https://github.com/love-apples/maxapi)
- Официальные клиенты MAX: [`github.com/max-messenger`](https://github.com/max-messenger)
- Реестр операторов ПДн (Роскомнадзор): [`pd.rkn.gov.ru/operators-registry`](https://pd.rkn.gov.ru/operators-registry)
- ФЗ-152: [`consultant.ru/document/cons_doc_LAW_61801`](https://www.consultant.ru/document/cons_doc_LAW_61801/)

## Дальнейшее чтение

- `docs/ADR-001-architecture.md` — архитектурное решение и его уточнения после первичной реализации.
- `docs/PRD-mvp.md` — функциональные требования (v6 — production-ready).
- `docs/SETUP.md` — пошаговая настройка админ-группы и регистрация операторов.
- `docs/RUNBOOK.md` — операционная инструкция координатору и ИТ.
- `docs/db-schema.md` — ER-диаграмма базы и инварианты.
- `docs/PRIVACY.md` — текст политики ПДн.
