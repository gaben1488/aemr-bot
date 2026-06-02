# Dependency management

Один источник истины — `bot/uv.lock`. Локальная среда, CI и Docker-образ работают с **одной** версией каждого пакета. Docker идёт дальше всех: он не просто берёт версии из lock, а проверяет SHA256-хеш каждого скачанного пакета (`uv export` → `pip install --require-hashes`, подробности в разделе «Как Docker-образ ставит зависимости»). Если среды расходятся («у меня работает, на проде падает») — см. раздел «Диагностика drift'а».

## Что у нас стоит

- **`bot/pyproject.toml`** — верхняя граница пакетов через `~=` (например `maxapi~=1.1`). Это **диапазон**, не точная версия.
- **`bot/uv.lock`** — resolved транзитивные версии для всех пакетов **вместе с SHA256-хешем каждого wheel'а**. **Закоммичен в репозиторий** — без этого Docker и каждый разработчик резолвят независимо, версии расходятся. `uv sync` (локально) и `uv export` (в Docker) ставят ровно то, что в lock; lock читают обе среды, а не только разработчик.
- **`bot/tests/test_deps_environment.py`** — guard-тесты: проверяют, что установленный `maxapi` и его API-сигнатура совпадают с ожиданием.

## Как Docker-образ ставит зависимости (hash-enforced)

`infra/Dockerfile` собирает зависимости из `bot/uv.lock`, а не из диапазонов `pyproject.toml`, и при установке **криптографически проверяет** каждый пакет. Шаги:

```dockerfile
# uv берётся пиннутым бинарём из официального образа astral-sh/uv
# (по digest, не по мутабельному тегу) и в финальный рантайм не попадает.
COPY bot/pyproject.toml /app/pyproject.toml
COPY bot/uv.lock /app/uv.lock
RUN uv export --frozen --no-emit-project --no-dev \
        --format requirements-txt -o /tmp/requirements.txt \
    && pip install --require-hashes --no-deps -r /tmp/requirements.txt
RUN pip install -e /app --no-deps   # сам aemr-bot, без повторного тянуть deps
```

Что здесь происходит и зачем:

1. **`uv export --frozen`** разворачивает `uv.lock` в `requirements.txt`, где у каждого пакета проставлен `--hash=sha256:…`. `--frozen` запрещает молчаливый перерезолв (если lock рассинхронен с `pyproject.toml`, шаг падает, а не «чинит» сам). `--no-emit-project` выкидывает сам aemr-bot (он editable, ставится отдельным шагом ниже), `--no-dev` отсекает dev-зависимости (ruff, mypy, pytest, pip-audit) — в прод-образ они не нужны.
2. **`pip install --require-hashes`** работает fail-closed: pip отказывается ставить хоть что-нибудь, для чего нет совпадающего SHA256. Один неверный или отсутствующий хеш — сборка падает. Это защита от подмены пакета на зеркале PyPI или MITM между билдом и индексом: в образ попадает ровно тот байтовый артефакт, что был зафиксирован в lock.
3. **`--no-deps`** на обоих `pip install` — всё дерево зависимостей уже стоит из хеш-верифицированного requirements.txt; повторно резолвить с PyPI нечего, и случайный незапиненный пакет не подтянется «довеском».

Поэтому раньше утверждение «Docker читает только `pyproject.toml`, lock декоративный» было верным, а теперь — нет: lock читается на каждой сборке и определяет, что именно приедет в прод.

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

Защита от уязвимых зависимостей живёт в трёх местах: локальная проверка разработчика, CI-гейт на каждом PR и хеш-верификация при сборке образа.

### Локально

```bash
uv run pip-audit
```

Включён в `pyproject.toml` как `[project.optional-dependencies] dev`. Запускать перед обновлением пакета или при сообщении CVE для зависимости — это быстрый способ увидеть проблему до пуша, не дожидаясь CI.

### В CI (job `lint`, шаг «pip-audit (CVE scan; hard fail)»)

`.github/workflows/ci.yml` гоняет `pip-audit` на каждом push и pull request в `main` и **валит сборку на найденной CVE** (hard fail). Чтобы не аудитить сам aemr-bot (его нет на PyPI, он ставится editable), шаг сначала готовит список только сторонних пакетов:

```bash
pip freeze --exclude-editable > audit-requirements.txt
grep -vE '^(aemr-bot|aemr_bot)(==| @ )' audit-requirements.txt > audit-requirements.thirdparty.txt
pip_audit --strict -r audit-requirements.thirdparty.txt $IGNORE_FLAGS
```

**Список игнорируемых CVE.** Две уязвимости помечены как неприменимые и не валят CI — каждая с обоснованием и grep-проверкой прямо в комментарии шага, чтобы любой разработчик мог убедиться, что игнор всё ещё валиден:

- **PYSEC-2026-161** (starlette, реконструкция URL из заголовка Host → потенциальный обход аутентификации) — бот не реконструирует URL из Host; health-эндпоинты `/livez`, `/readyz` отдают plain-статус и не используют `request.url`. Проверка: `grep -r 'request\.url\|HTTP_HOST' aemr_bot/` → 0.
- **CVE-2025-62727** (starlette, ReDoS на заголовке Range в `FileResponse`/`StaticFiles`) — бот не использует ни `FileResponse`, ни `StaticFiles`, отдаёт только JSON. Проверка: `grep -r 'FileResponse\|StaticFiles' aemr_bot/` → 0.

Обе тянутся транзитивно через `fastapi → starlette` (fastapi пинит уязвимую `starlette`). **Этот список нужно пересматривать при каждом bump'е fastapi**: если новый fastapi подтянет starlette с фиксом, соответствующий `--ignore-vuln` убрать. Отчёт `pip-audit.json` выгружается как артефакт сборки (`actions/upload-artifact`).

### При сборке образа (require-hashes)

Это не сканер уязвимостей, а смежный supply-chain контроль: `pip install --require-hashes` в `infra/Dockerfile` гарантирует, что в прод приедет ровно тот байтовый артефакт каждого пакета, что зафиксирован в `uv.lock` (см. раздел «Как Docker-образ ставит зависимости»). Сам Dockerfile собирается смоук-тестом в CI (job `docker-build`) — то есть хеш-проверка прогоняется на каждом PR, и расхождение `uv.lock` с реально доступными на PyPI хешами свалит сборку.

## Обновление зависимостей: Dependabot

Рутинные обновления версий не нужно вычитывать вручную — за ними следит Dependabot (`.github/dependabot.yml`). Раз в неделю (понедельник 06:00 Asia/Kamchatka) он открывает PR'ы на обнаружённые апдейты по трём экосистемам:

| Экосистема | Каталог | Лимит PR | Префикс коммита |
|---|---|---|---|
| `pip` (Python-зависимости через `uv.lock`) | `/bot` | 5 | `chore(deps)` |
| `github-actions` (версии actions в workflow'ах) | `/` | 5 | `chore(ci)` |
| `docker` (base image `python:3.12-slim`) | `/infra` | 3 | `chore(docker)` |

Лимиты держат число одновременных PR разумным, чтобы не завалить трекер. Каждый PR получает label'ы `dependencies` + (`python` / `github-actions` / `docker`).

**Что Dependabot НЕ трогает (ignore-правила) и почему.** Не всякое обновление безопасно применять автоматически:

- **`maxapi` исключён полностью** — это критическая зависимость от SDK платформы MAX, на которой держится весь транспорт бота. Любое обновление (даже минорное) требует ручного аудита breaking changes по процедуре из единой базы знаний (`docs/site/index.html`, раздел «Разработчику» → обновление зависимостей; в комментарии `dependabot.yml` — отсылка к истории `0.9.18 → 1.1.0`). Поэтому его обновляют только вручную, отдельным PR (см. «Workflow обновления пакета» выше).
- **`sqlalchemy` — игнорируется только `semver-major`** — минорные апдейты в пределах текущего major считаются безопасными и проходят автоматически; переход на следующий major требует ручной проверки async-API.
- **`pydantic` — игнорируется только `semver-major`** — то же: минор проходит, major-bump требует ручной проверки моделей (settings, `ChatMembersManager`).

### Как ревьюить Dependabot-PR

1. **Минорный апдейт стороннего пакета** — посмотреть changelog, дождаться зелёного CI (там уже отработают `pip-audit`, guard-тесты, pytest на Postgres, docker-build). Если всё зелёное — мерджить.
2. **Мажорный апдейт** (sqlalchemy/pydantic прошли мимо ignore по ошибке, либо вручную сняли игнор) — относиться как к обновлению `maxapi`: прогнать `uv run pytest tests/test_deps_environment.py`, поднять guard, проверить вызывающий код, сделать локальный docker smoke. Шаги — в «Workflow обновления пакета».
3. **GitHub Actions** — Dependabot бампит уже SHA-пиннутые actions (в workflow'ах они зафиксированы по commit-SHA с комментарием версии, не по перемещаемому тегу). Проверить, что новый SHA соответствует заявленному в комментарии тегу, и что CI зелёный.
4. **Docker base image** — обновляется digest `python:3.12-slim`; убедиться, что `docker-build` job собрался.

**Связка с supply-chain.** После мерджа `pip`-PR Dependabot обновляет `bot/uv.lock` (новые версии + новые хеши). Дальше всё работает само: CI проверит окружение (`pip-audit` по установленным зависимостям в job `lint`) и хеш-верифицированную сборку образа из lock в job `docker-build`, а прод-образ соберётся из обновлённого lock через `uv export` + `--require-hashes`. То есть в прод приедет ровно то, что прошло проверку и было хеш-верифицировано — никакого «дрейфа» между тем, что одобрили в PR, и тем, что запустилось на VPS.

## Принципы (kaizen)

- **Один lock-файл** для всех сред (local / CI / Docker); в Docker он ещё и хеш-верифицируется (`--require-hashes`).
- **Никаких `pip install` мимо uv** в среде разработчика.
- **Guard-тесты** на ключевые API-сигнатуры внешних пакетов — RED ловит drift раньше прода.
- **Рутинные апдейты — через Dependabot**, ручной аудит — только для `maxapi` и мажоров sqlalchemy/pydantic.
- **Обновление = отдельный PR** с проверкой каждого слоя (local sync → tests → docker build → prod smoke).
- **Rollback стратегия** — если деплой положил бот, revert немедленно, разбираться потом.
