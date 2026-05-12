# VPS smoke-check после деплоя

Цель документа — быстро подтвердить, что текущий `main` реально доехал до VPS, контейнер жив, БД доступна, scheduler/watchdog не молчат, а операторские команды базово работают.

Этот чек-лист выполняется после merge в `main` и деплоя на сервер. Он не заменяет CI: CI проверяет код, а smoke-check проверяет реальную среду.

## 1. Перейти в проект

```bash
cd /home/aemr/aemr-bot
```

Проверить, какой commit сейчас на сервере:

```bash
git rev-parse --short HEAD
git log -1 --oneline
```

Ожидаемо: commit должен совпадать с последним merge/squash commit в GitHub `main`.

## 2. Обновить код и контейнер вручную, если auto-deploy не применился

```bash
git fetch origin main
git checkout main
git pull --ff-only origin main
cd infra
docker compose build bot
docker compose up -d bot
```

Если используется auto-deploy, ручной блок нужен только при расхождении server HEAD и GitHub main.

## 3. Проверить контейнеры

```bash
cd /home/aemr/aemr-bot/infra
docker compose ps
docker compose logs --tail 200 bot
```

Ожидаемо:

- `bot` в состоянии `Up` / `running`;
- нет постоянного restart-loop;
- в логах нет повторяющихся traceback;
- после старта видны сообщения инициализации scheduler/бота.

## 4. Проверить health endpoints

```bash
curl -fsS http://127.0.0.1:8080/livez && echo
curl -fsS http://127.0.0.1:8080/readyz && echo
curl -fsS http://127.0.0.1:8080/healthz && echo
```

Смысл проверок:

- `/livez` — жив ли event loop / процесс бота;
- `/readyz` — готов ли бот к работе, включая БД;
- `/healthz` — совместимый readiness endpoint.

Ожидаемо: все три команды завершаются с exit code `0`. Если `/livez` ok, а `/readyz` падает — процесс жив, но проблема в БД или зависимостях. В этом случае не надо сразу рестартовать бот; сначала смотреть PostgreSQL и `DATABASE_URL`.

## 5. Проверить watchdog и deploy logs

```bash
journalctl -t aemr-bot-watchdog -n 80 --no-pager
journalctl -t aemr-bot-deploy -n 80 --no-pager
```

Ожидаемо:

- watchdog не пишет постоянные `/livez unreachable`;
- нет бесконечного `docker compose restart`;
- deploy-лог либо показывает успешный fast-forward/deploy, либо отсутствие новых изменений.

## 6. Проверить scheduler/pulse

В служебной группе MAX после старта должен появиться startup/recovery pulse. Обычный pulse зависит от расписания.

На сервере дополнительно проверить логи за последние часы:

```bash
docker compose logs --since 3h bot | grep -Ei 'pulse|scheduler|apscheduler|startup|health|traceback|exception' || true
```

Ожидаемо:

- scheduler jobs зарегистрированы;
- нет регулярных исключений в pulse/selfcheck jobs;
- если pulse не пришёл в ожидаемое окно, проверить, не был ли это off-hours gap по расписанию.

## 7. Проверить операторские команды в MAX

В служебной группе выполнить:

```text
/op_help
/diag
/open_tickets
```

Ожидаемо:

- `/op_help` показывает операторское меню;
- `/diag` возвращает счётчики без traceback;
- `/open_tickets` либо показывает открытые обращения, либо дружелюбное сообщение, что открытых нет.

Опционально для IT:

```text
/backup
```

Ожидаемо: бот сообщает о создании backup или понятной ошибке. Если включён S3/rclone, локальная копия не должна ломаться при ошибке upload.

## 8. Что считать провалом smoke-check

Smoke-check не пройден, если есть хотя бы одно из условий:

- server HEAD не совпадает с текущим `main`;
- `bot` в restart-loop;
- `/livez` падает;
- `/readyz` падает дольше кратковременного окна после старта;
- в логах есть повторяющийся traceback;
- watchdog постоянно рестартует контейнер;
- `/diag` или `/op_help` в служебной группе не отвечают;
- startup/recovery pulse не приходит после реального рестарта, при этом отправка сообщений в MAX в целом работает.

## 9. Минимальный отчёт после проверки

Скопировать в issue/чат:

```text
VPS smoke-check:
- server HEAD: <sha>
- docker compose ps: ok/fail
- /livez: ok/fail
- /readyz: ok/fail
- /healthz: ok/fail
- watchdog journal: ok/fail
- deploy journal: ok/fail
- /op_help: ok/fail
- /diag: ok/fail
- pulse after deploy: ok/fail/not observed yet
- notes: <если есть>
```
