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
BOT_TOKEN=<токен от @MasterBot в MAX>
POSTGRES_PASSWORD=local-test-pass
DATABASE_URL=postgresql+asyncpg://aemr:local-test-pass@db:5432/aemr
```

Остальные параметры оставь как есть. `BOT_MODE=polling`, `WEBHOOK_*`, `BACKUP_*`, `HEALTHCHECK_URL`, `ADMIN_GROUP_ID` пока пустые — заполнишь после первого запуска.

**Получить тестовый токен:** открой `@MasterBot` в MAX, отправь `/newbot`, пройди мастер. После создания бот выдаст токен (Base64-подобная строка длиной ~32+ символов). Если у тебя уже есть основной бот АЕМР — попроси у владельца отдельный тестовый.

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

Чтобы карточка ушла в админ-группу — нужно настроить `ADMIN_GROUP_ID`. Создай в MAX группу, добавь туда бота, в группе напиши `/whoami`. Бот вернёт `chat_id` группы и твой `max_user_id`. Эти числа кладёшь в `.env`:

```
ADMIN_GROUP_ID=-1001234567890
COORDINATOR_MAX_USER_ID=165729385
```

Затем зарегистрируй себя как оператора:

```bash
docker compose exec db psql -U aemr -d aemr -c \
  "INSERT INTO operators (max_user_id, full_name, role, is_active) VALUES (165729385, 'Test', 'coordinator', true);"
```

Перезапусти бот: `docker compose restart bot`. Теперь после прохождения воронки в группу прилетит карточка обращения. Реплайнешь на неё — ответ пойдёт жителю.

## Шаг 4 — структура проекта

```
aemr-bot/
├─ bot/aemr_bot/           Python-пакет
│  ├─ main.py              entrypoint, переключатель polling/webhook
│  ├─ config.py            Settings из .env (Pydantic)
│  ├─ health.py            /healthz + Heartbeat singleton
│  ├─ texts.py             все тексты, что отправляет бот
│  ├─ keyboards.py         inline-клавиатуры
│  ├─ db/
│  │  ├─ models.py         7 таблиц SQLAlchemy
│  │  ├─ session.py        async-engine
│  │  └─ alembic/          миграции
│  ├─ handlers/
│  │  ├─ __init__.py       register_handlers + IdempotencyMiddleware
│  │  ├─ start.py          /start, /menu, /help, /forget, /whoami
│  │  ├─ menu.py           главное меню, Мои обращения, Контакты
│  │  ├─ appeal.py         FSM-воронка (по функции на состояние)
│  │  ├─ operator_reply.py обработка реплая в админ-группе
│  │  └─ admin_commands.py /stats, /reopen, /close, /erase, /setting, /diag
│  ├─ services/
│  │  ├─ users.py          CRUD пользователя + FSM-операции
│  │  ├─ operators.py      регистрация операторов + audit_log
│  │  ├─ appeals.py        CRUD обращений
│  │  ├─ card_format.py    форматирование карточки в админ-группу и «Мои обращения»
│  │  ├─ stats.py          формирование XLSX через openpyxl
│  │  ├─ policy.py         кеш токена PRIVACY.pdf
│  │  ├─ uploads.py        upload_path / upload_bytes / file_attachment
│  │  ├─ idempotency.py    отбраковка дублей Update-ов
│  │  ├─ settings_store.py редактируемые из админки настройки
│  │  └─ cron.py           APScheduler: бэкап + selfcheck + healthcheck-ping + monthly stats
│  └─ utils/
│     ├─ event.py          адаптер над maxapi event-объектами
│     └─ attachments.py    парсинг VCF и сериализация attachments
├─ infra/
│  ├─ Dockerfile           python:3.12-slim, pinned by digest
│  ├─ docker-compose.yml   db + bot + nginx + certbot
│  ├─ nginx/feedback.conf  reverse-proxy (используется в webhook-режиме)
│  ├─ init-letsencrypt.sh  первое получение сертификата
│  └─ .env.example
├─ seed/                   topics.json, contacts.json, welcome.md, consent.md, PRIVACY.pdf
├─ scripts/
│  └─ generate_privacy_pdf.py   regenerate PRIVACY.pdf from PRIVACY.md
└─ docs/                   ADR, PRD, PRIVACY, RUNBOOK, DEVELOPER (этот файл)
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

Сейчас в `tests/` пять тестов на сервисный слой. Они **не работают на in-memory SQLite** из-за PostgreSQL-specific JSONB; если нужно гонять — либо поднимай локальный Postgres и подменяй `DATABASE_URL`, либо воспользуйся `testcontainers` (на рассмотрение в фазе C). Это известный tech-debt, см. `docs/PHASE_C` (если ещё не написан).

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

## Полезные ссылки

- Bot API MAX: [`dev.max.ru/docs/chatbots`](https://dev.max.ru/docs/chatbots)
- Исходники `maxapi` (community Python lib): [`github.com/love-apples/maxapi`](https://github.com/love-apples/maxapi)
- Официальные клиенты MAX: [`github.com/max-messenger`](https://github.com/max-messenger)
- Реестр операторов ПДн (Роскомнадзор): [`pd.rkn.gov.ru/operators-registry`](https://pd.rkn.gov.ru/operators-registry)
- ФЗ-152: [`consultant.ru/document/cons_doc_LAW_61801`](https://www.consultant.ru/document/cons_doc_LAW_61801/)

## Дальнейшее чтение

- `docs/ADR-001-architecture.md` — архитектурное решение и его эволюция (v1 → v4).
- `docs/PRD-mvp.md` — функциональные требования.
- `docs/RUNBOOK.md` — операционная инструкция координатору и ИТ.
- `docs/PRIVACY.md` — текст политики ПДн.
