# Процедура апгрейда maxapi (и других hot-pin зависимостей)

> Дополнение к `docs/DEPS.md`. Тот документ описывает workflow обновления
> «как считать lock / sync / smoke». Этот — пошаговый чеклист именно для
> maxapi, с привязкой к нашим guard-тестам, точкам отката и пред-полётным
> проверкам. Логи апгрейдов: PR #50 (0.9.18 → 1.1.0), PR #52 (потенциально
> 1.1 → 1.2).
>
> Также — стратегии для соседних зависимостей (asyncpg, sqlalchemy,
> pydantic, apscheduler, aiohttp) в разделе «Соседи». У каждой свой
> риск-профиль.

## 1. Проверка перед апгрейдом

### 1.1. Прочитать changelog maxapi
- Открыть https://github.com/love-apples/maxapi/releases
- Свериться с changelog для целевой версии (особенно `BREAKING CHANGES`).
- Просмотреть git diff `dispatcher.py`, `bot.py`, `types/__init__.py`,
  `enums/`, `filters/` — это публичный API.
- Если есть аннотации `@deprecated` или `warnings.warn(...,
  DeprecationWarning)` — собрать список и сравнить с нашими импортами
  через `docs/_meta/MAXAPI_INVENTORY.md`.

### 1.2. Сверить с inventory
- Открыть `docs/_meta/MAXAPI_INVENTORY.md` раздел 3 (Bot-методы)
  и 4 (deprecated-паттерны).
- Если новый release удаляет метод, который мы зовём — план миграции
  ДО апгрейда, не после.

### 1.3. Проверить guard-сигнатуры
- `bot/tests/test_deps_environment.py` фиксирует:
  - точную версию (`EXPECTED_MAXAPI_VERSION`),
  - сигнатуру `DefaultConnectionProperties.__init__`.
- Если в changelog есть слова «conn properties», «timeout», «retry» —
  ждать падения этого теста.

## 2. Локальный апгрейд в отдельной ветке

```bash
git checkout -b deps/maxapi-X.Y.Z

cd bot
# Поднять верхнюю границу в pyproject.toml.
# Для патча в той же минорной версии (1.1.0 → 1.1.5) — менять не надо,
# `maxapi~=1.1` уже включает все 1.1.x. Для минорного bump'а:
sed -i 's/"maxapi~=1.1"/"maxapi~=1.2"/' pyproject.toml
# Для мажорного bump'а:
sed -i 's/"maxapi~=1.1"/"maxapi~=2.0"/' pyproject.toml

uv lock --upgrade-package maxapi
git diff uv.lock           # сверить, какая версия закрепилась
uv sync --extra dev
```

Если `uv lock` падает с конфликтом — резолвер не нашёл совместимой
комбинации. Чаще всего это `aiohttp` (maxapi требует определённой
версии). Прочитать сообщение, обновить смежный пакет в pyproject либо
откатить bump'нутый диапазон.

## 3. Поднять guard-версию

В `bot/tests/test_deps_environment.py`:

```python
EXPECTED_MAXAPI_VERSION = "X.Y.Z"
```

Если сигнатура `DefaultConnectionProperties.__init__` сменилась — обновить
список `params` в `test_default_connection_signature_matches_prod_api`.
Это первый сигнал, что main.py надо смотреть.

## 4. Проверить наш код на breaking changes

```bash
cd bot
uv run pytest tests/test_deps_environment.py -v   # сначала только guard
uv run pytest -q -x                               # потом всё
uv run ruff check aemr_bot/
uv run mypy aemr_bot/
```

Зелёное всё — переходим к smoke. Красное — фиксить в той же ветке,
пока тесты не позеленеют. Особое внимание к файлам из
`docs/_meta/MAXAPI_INVENTORY.md` раздел 1.

## 5. Прочесть наши `# Deprecated:` маркеры

Грепнуть в репо:
```bash
grep -rn "deprecated.*maxapi\|maxapi.*deprecat" bot/aemr_bot
```
Эти комментарии — наши собственные напоминания, какие пути требуют
замены при апгрейде. Сверить с changelog.

## 6. Локальный smoke в Docker

```bash
cd infra
docker compose build bot
docker compose up bot 2>&1 | head -50
# ожидание: bot стартует, livez/readyz 200, нет TypeError/ImportError
docker compose logs --tail 30 bot
docker compose down
```

Если бот сразу уходит в restart-loop с DeprecationWarning — не критично,
deprecated работает до удаления; warning виден в `journalctl`. Если
TypeError/ImportError — фиксить.

## 7. Создать PR и дождаться auto-deploy

```bash
git add bot/pyproject.toml bot/uv.lock bot/tests/test_deps_environment.py
git commit -m "deps(maxapi): bump 1.1 → X.Y"
git push -u origin deps/maxapi-X.Y.Z
gh pr create --title "deps(maxapi): bump 1.1 → X.Y" --fill
```

После merge auto-deploy на VPS подтянет образ через cron `*/10`.

## 8. Verify на VPS

```bash
ssh aemr@193.233.244.217 'cd ~/aemr-bot/infra && docker compose ps'
# Ожидание: bot Up (healthy), НЕ Restarting

ssh aemr@193.233.244.217 'cd ~/aemr-bot/infra && docker compose logs --tail 50 bot'
# Ищем DeprecationWarning, TypeError, ImportError — должно быть пусто.

# В админ-чате:
# - тапнуть «📊 Статистика» → должен прийти XLSX
# - тапнуть «📋 Открытые обращения» → должен прийти список (или «нет открытых»)
# - отправить в личку себе /start → меню жителя должно прийти
```

Если что-то из этого не работает в течение 5 минут после деплоя — **revert
немедленно**.

## 9. Откат

```bash
# На вашей машине
git revert <merge-commit-hash>
git push origin main
# auto-deploy через 10 минут вернёт предыдущую версию
```

Если бот в restart-loop И auto-deploy не успевает — на VPS:
```bash
ssh aemr@193.233.244.217
cd ~/aemr-bot
git fetch origin
git checkout <previous-good-commit>
cd infra && docker compose up -d --build bot
```

Заодно — открыть issue с трасой стека из `docker compose logs bot`.

## 10. После успешного апгрейда

- Обновить `docs/_meta/MAXAPI_INVENTORY.md` (раздел 1 — добавить новые
  импорты, если использовали новые фичи; раздел 4 — удалить
  закрытые deprecated-паттерны).
- Обновить `docs/_meta/MAXAPI_UNUSED_FEATURES.md` (если использовали
  ранее «неиспользуемую» фичу — убрать из списка).
- В `.mulch/` (через `ml record …`) — `decision`, какие новые
  возможности появились и от чего отказались.

---

## Соседи: процедуры для других зависимостей

### `asyncpg ~= 0.30`

Драйвер Postgres. Major bump (0.30 → 0.40+) бывает раз в полгода.
- **Риск:** медленные query при изменении prepared-cache.
- **Что критично проверить:** все SQL-запросы в `aemr_bot/db/` (есть
  `text(...)` в SQLAlchemy 2.0).
- **Smoke:** `pytest tests/ -q -k "test_db"` + миграции
  `uv run alembic upgrade head` на пустой Postgres.

### `sqlalchemy ~= 2.0`

ORM. Stable, мажорки не ожидаются.
- **Риск:** изменения в `select(...).where(...)` / `Result.scalars()`
  API между минорками.
- **Что критично проверить:** все запросы в `aemr_bot/db/` и `services/`.
- **Smoke:** `pytest -q` (полный набор, у нас 964 теста его покрывают).

### `pydantic ~= 2.9`

Валидация моделей и конфига. Pydantic 2 → 3 будет breaking, но не
скоро.
- **Риск:** breaking changes в `model_validate`, `model_dump`,
  `TypeAdapter` (мы используем последний в `utils/attachments.py`).
- **Что критично проверить:** `config.py` (Settings), `utils/attachments.py`,
  `services/idempotency.py`.
- **Smoke:** `pytest -q` + проверка, что `from aemr_bot.config import
  settings` не падает с ValidationError на свежем .env.

### `pydantic-settings ~= 2.6`

Загрузка env-переменных в pydantic-модели.
- **Риск:** изменения `SettingsConfigDict`, `env_prefix`.
- **Что критично проверить:** `config.py` импорты и `Field(...,
  alias=...)`.
- **Smoke:** `python -c "from aemr_bot.config import settings;
  print(settings.bot_mode)"`.

### `apscheduler ~= 3.10`

Cron-планировщик. APScheduler 4.x объявлен, миграция нетривиальная.
- **Риск:** в 4.x — async-first API, текущие декораторы поменяются.
- **Что критично проверить:** `services/cron.py` (все `scheduler.add_job`).
- **Стратегия:** **остаться на 3.x до полноценного релиза 4.x с миграц.
  гайдом**. Не апгрейдить «потому что есть».

### `aiohttp ~= 3.10`

HTTP-клиент. Используется maxapi внутри и нашим preflight-`get_me`.
- **Риск:** изменения в `ClientSession`, `ClientTimeout`.
- **Что критично проверить:** все наши `aiohttp.ClientSession(...)` в
  `main.py` (PATCH /me) и в тестах.
- **Smoke:** `pytest -q -k "test_main_helpers"`.

### Общее правило для майоров

- **Один пакет — один PR.** Никогда не bump'ить maxapi + sqlalchemy
  + pydantic одновременно. Если что-то ломается — невозможно понять
  что именно.
- **CHANGELOG обязателен.** Если в репо пакета нет публичного
  changelog — апгрейд только под сильную причину (CVE, нужный bugfix).
- **`pip-audit` раз в квартал.** `uv run pip-audit` в `bot/`.
  Если выйдет CVE для maxapi или соседей — приоритет апгрейда
  поднимается до P0.

## Шпаргалка: команды одной строкой

```bash
# Перед апгрейдом
cd bot && uv run pip-audit

# Сам апгрейд
cd bot && uv lock --upgrade-package maxapi && uv sync --extra dev

# Проверки
cd bot && uv run pytest -q && uv run ruff check aemr_bot/

# Smoke в Docker
cd infra && docker compose build bot && docker compose up bot

# Verify на VPS
ssh aemr@193.233.244.217 'cd ~/aemr-bot/infra && docker compose logs --tail 50 bot'

# Откат
git revert <merge-commit> && git push origin main
```
