# Dependency management

Один источник истины — `bot/uv.lock`. Локальная среда, CI и Docker-образ работают с **одной** версией каждого пакета. Если они расходятся («у меня работает / на проде падает») — см. раздел «Диагностика drift'а».

## Что у нас стоит

- **`bot/pyproject.toml`** — верхняя граница пакетов через `~=` (например `maxapi~=0.6`). Это **диапазон**, не точная версия.
- **`bot/uv.lock`** — resolved транзитивные версии для всех пакетов. Это **точный pin** который ставит `uv sync` и читает Docker (`pip install -e /app` в `infra/Dockerfile`).
- **`bot/tests/test_deps_environment.py`** — guard-тесты: проверяют, что установленный `maxapi` и его API-сигнатура совпадают с ожиданием.

## Workflow разработчика

```bash
cd bot
uv sync --extra dev      # синхронизировать .venv с uv.lock
uv run pytest tests/ -q  # запускать тесты ТОЛЬКО через uv run
uv run ruff check aemr_bot/
```

**Никогда** не делать `pip install <package>` глобально / в системный python — это создаёт другую версию пакета мимо `uv.lock`, тест на ноуте проходит, в Docker валится.

## Workflow обновления пакета (например, maxapi с 0.9.x на 1.x)

Делать **отдельным PR**, не вперемешку с фичами.

### 1. Сначала обновить lock

```bash
cd bot
uv lock --upgrade-package maxapi
git diff uv.lock         # сверить какая теперь версия
```

Если хочется выйти из текущего диапазона `~=0.6`:

```bash
# Поднять диапазон в pyproject.toml
sed -i 's/"maxapi~=0.6"/"maxapi~=1.0"/' pyproject.toml
uv lock --upgrade-package maxapi
```

### 2. Синхронизировать локальную среду

```bash
uv sync --extra dev
uv run python -c "import maxapi; print(maxapi.__version__)"
```

### 3. Обновить guard

В `bot/tests/test_deps_environment.py` поднять `EXPECTED_MAXAPI_VERSION`. Если изменилась сигнатура `DefaultConnectionProperties.__init__` — обновить `test_default_connection_signature_matches_prod_api`.

### 4. Проверить breaking changes

```bash
uv run pytest tests/ -q
uv run ruff check aemr_bot/
```

Если красное — фиксить вызывающий код **в том же PR**, не отдельно. Особое внимание:

- `bot/aemr_bot/main.py` — `Bot(..., default_connection=...)`, `Dispatcher(...)`.
- `bot/aemr_bot/utils/event.py` — `ack_callback`, `send_or_edit_screen`.
- `bot/aemr_bot/handlers/*` — все вызовы `bot.send_message`, `bot.edit_message`.
- `bot/aemr_bot/handlers/operator_reply.py` — relay attachments.

### 5. Локальный smoke

Перед merge — запустить бот локально хотя бы 2 минуты, отправить себе тестовое сообщение, тапнуть кнопку.

```bash
# В отдельном окне поднять docker-compose как на проде
cd infra
docker compose build bot
docker compose up bot      # проверить что стартует без TypeError, livez 200
```

### 6. Merge → auto-deploy → verify

Auto-deploy на VPS подхватит образ через cron `*/10`. После деплоя:

```bash
ssh aemr@193.233.244.217 'cd ~/aemr-bot/infra && docker compose ps'
# Ожидание: bot Up (healthy), НЕ Restarting

ssh aemr@193.233.244.217 'cd ~/aemr-bot/infra && docker compose logs --tail 30 bot'
# Ожидание: нет TypeError, нет ImportError
```

В админ-чате — серия тапов на кнопки, отклик мгновенный.

Если бот в Restarting — `git revert` сразу, разбираться отдельно.

## Диагностика drift'а

Симптом: тесты у меня зелёные, в Docker `TypeError` / `ImportError` на старте.

1. Запустить тесты через `uv run pytest tests/test_deps_environment.py` — если **они** зелёные, среда синхронна с lock.
2. Если красные — `uv sync --extra dev` пересоздаст .venv по lock'у.
3. Если кто-то поменял глобальный pip-пакет — `pip uninstall <pkg> -y`, потом `uv sync`.
4. Если расхождение версия в lock vs Docker — пересобрать образ: `docker compose build --no-cache bot`.

## Аудит уязвимостей

```bash
uv run pip-audit
```

Включён в `pyproject.toml` как `[project.optional-dependencies] dev`. Запускать раз в квартал или при сообщении CVE для зависимости.

## Принципы (kaizen)

- **Один lock-файл** для всех сред (local / CI / Docker).
- **Никаких `pip install` мимо uv** в среде разработчика.
- **Guard-тесты** на ключевые API-сигнатуры внешних пакетов — RED ловит drift раньше прода.
- **Обновление = отдельный PR** с проверкой каждого слоя (local sync → tests → docker build → prod smoke).
- **Rollback стратегия** — если деплой положил бот, revert немедленно, разбираться потом.
