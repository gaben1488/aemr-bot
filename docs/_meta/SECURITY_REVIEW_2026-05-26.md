---
status: superseded
superseded_by: docs/_meta/SECURITY_REVIEW_2026-05-27.md
note: |
  Этот snapshot — security-review на 2026-05-26. Все 🔴/🟡-finding'и
  закрыты (SEC #1-#9, SACRED #1-#5, M1-M10). Свежий delta-аудит +
  closure status — в `SECURITY_REVIEW_2026-05-27.md` (D1/D2 закрыты
  PR #103, D3/D4 — accepted technical debt).
---

# Security Review 2026-05-26

> Сводный итог security-пасса по aemr-bot (HEAD `a09c2a4` → main).
> Скоуп: MAX-специфика бот-инфраструктуры + социальная инженерия через
> гос-канал + классические эксплоиты (SSRF / SQL / timing / shell /
> injection / PII / whitelist bypass).
>
> Глубокая аналитика — в четырёх parallel-отчётах:
> - [`SEC_INVENTORY_2026-05-26.md`](SEC_INVENTORY_2026-05-26.md) — все
>   точки ввода извне, suspicious patterns.
> - [`SEC_MAX_THREATS_2026-05-26.md`](SEC_MAX_THREATS_2026-05-26.md) —
>   spoofing / replay / token-leak в контексте MAX SDK 1.1.0.
> - [`SEC_SCAM_VECTORS_2026-05-26.md`](SEC_SCAM_VECTORS_2026-05-26.md) —
>   социальная инженерия с использованием гос-бота.
> - [`SEC_EXPLOITS_2026-05-26.md`](SEC_EXPLOITS_2026-05-26.md) —
>   конкретные эксплоиты в коде (input→sink цепочки).
>
> Этот файл — итоговая матрица + план-фиксы. Стиль — как `SECURITY.md §10a`.

---

## Состояние

```
🔴 Critical:  6 (4 социалка + 1 dormant capability + 1 платформа)
🟠 High:      2 (PR-injection, root-cron shell-injection)
🟡 Medium:   ~10 (мелкие гэпы, не критично, но накопится)
✅ Защищено: SEC #1–9 + SACRED #1–6 + 5 классических классов из этого пасса
```

Бот по большинству классических классов **уже защищён**. Все 🔴 — про
**социальную инженерию**, площадку и **product-decisions** (что показывать жителю,
кто может что нажать), не про код-инъекции. Это означает, что фикс-PR
будут смешанные: код + копи (welcome/политика) + операционные изменения
(cleanup-cron, two-man approval).

---

## Матрица находок

### 🔴 Critical

| ID | Что | Источник | Где |
|---|---|---|---|
| C1 | **Welcome/consent — dormant capability**. БД хранит, UI редактирует, но житель видит **hardcoded `texts.WELCOME`** в `menu.py:120-124`, `start.py:79,92`. False security: IT думает что отредактировал — на деле ничего не изменилось | scams.md V3 | `services/settings_store.py:99-100` + `handlers/menu.py:120-124` |
| C2 | **Broadcast spoofing**. Один скомпрометированный coordinator/it = моментальная фишинг-рассылка всем подписчикам. Нет two-man approval, нет URL-фильтра на `broadcast.text`, нет cooldown между рассылками | scams.md V4 | `handlers/broadcast.py` |
| C3 | **Operator takeover**. Идентификация только по `max_user_id`. Нет 2FA / PIN-кода для критических операций (`/broadcast`, `/op_add`, `/erase`) | scams.md V5 | `handlers/_auth.py`, `handlers/admin_commands.py` |
| C4 | **PII-фишинг через support-impersonation**. Бот никогда не предупреждает «мы НИКОГДА не запрашиваем паспорт / СНИЛС / банк». Жертва не различает legit-вопрос и атаку | scams.md V7 | `seed/welcome.md`, `texts.py` |
| C5 | **Fake-bot impersonation**. В welcome нет реального username бота — житель не может проверить, тот ли бот ему пишет в личке | scams.md V1 | `seed/welcome.md` |
| C6 | **Attachment URL leak в MAX** (платформенный, не fix-able). Direct URL вложений доступен без авторизации, паттерн предсказуем, ссылка живёт ~неделю после удаления. Mitigation — только warning жителю в политике | max-threats.md CVE-3 | внешний |

### 🟠 High

| ID | Что | CVSS | Где |
|---|---|---:|---|
| H1 | **PR body injection через `operator_name`**. `full_name` оператора попадает в GitHub PR body без sanitize newline/markdown. Скомпрометированный IT может вписать в `full_name` `\n## Maintainer note\n**Auto-approve:** YES` → визуально валидный note для reviewer'а | 6.1 | `services/repo_sync.py:121-142` |
| H2 | **Shell injection в `healthwatch.sh`** (root-cron). `BOT_TOKEN` и `ADMIN_GROUP_ID` парсятся из `.env` через `awk` и unquoted-подставляются в URL+Authorization header. Любой с write-доступом на `.env` → root RCE через cron | 6.5 | `scripts/healthwatch.sh:66-79` |

### 🟡 Medium

| ID | Что | Источник | Где |
|---|---|---|---|
| M1 | **PII в логах**: callback `payload` пишется на info-уровне | exploits.md F5 | `handlers/appeal.py:490,492` |
| M1b | **PII в логах**: `max_user_id` в `appeal_geo.py:57,146` — псевдо-идентификатор, формально ПДн по 152-ФЗ при возможности соотнесения | exploits.md F5 | `handlers/appeal_geo.py:57,146` |
| M1c | **Docker json-file logs (10MB×3) переживают `/erase`** — нарушает требование удаления по запросу субъекта 152-ФЗ | exploits.md F5 | infra-level |
| M2 | **Stale operators**. Оператор, покинувший admin-группу MAX, остаётся `is_active=true` в БД до ручной чистки. Auth защищён `is_admin_chat`, но cleanup-cron отсутствует | max-threats.md CVE-9 | `services/operators.py` |
| M3 | **Outgoing operator reply без URL-фильтра**. Оператор может вписать жителю любую URL в тексте ответа — нет whitelist (как у SEC #4 для настроек) | scams.md V6, inventory | `handlers/operator_reply.py:316-384` |
| M4 | **`emergency_contacts.phone` без формат-валидации**. IT может подменить на платную линию (`+7-900-911-XXXX` premium) | scams.md, inventory | `services/settings_store.py:108-113` |
| M5 | **Followup link injection (incoming)**. URL-whitelist применяется только к OUTGOING URL-настройкам. Текст followup от жителя с фишинговой ссылкой → попадает в admin chat → оператор может кликнуть | scams.md V6 | `handlers/appeal.py` followup-path |
| M6 | **TLS pinning отсутствует**. `aiohttp` проверяет цепочку через системный CA store, без cert-pinning к `*.max.ru` | max-threats.md CVE-5 | infra-level — accept |
| M7 | **Shell injection в `init-letsencrypt.sh`** (manual-run, низкий риск) | exploits.md F2b | `infra/init-letsencrypt.sh:14-18` |
| M8 | **GitHub API response без schema validation**. `repo_sync.py` использует `.get()` без проверки структуры | inventory.md | `services/repo_sync.py:166,358` |
| M9 | **`/export` без size-limit**. JSON может вырасти на 500+ обращений, может OOM-нуть админ-чат | inventory.md | `handlers/admin_commands.py` |
| M10 | **`/setting` json.loads глубокой структуры**. Валидируется только по required-keys (issubset), extras пропускаются | inventory.md | `handlers/admin_commands.py:336` |

### ✅ Защищено (проверено)

- **Reply spoofing** — `handlers/_auth.py:21-32` + `callback_router.py:108-110` chat-binding + allowlist по `max_user_id`.
- **Callback replay** — SEC #7 fail-CLOSED `services/idempotency.py`.
- **Webhook secret** — `main.py:147-154` `hmac.compare_digest`, header-only.
- **BOT_TOKEN leak** — Authorization header, нет sentry, нет traceback в admin chat.
- **Bot impersonation на уровне платформы** — верификация юрлица РФ, модерация MAX.
- **DM-flooding / DoS** — 3 уровня: `_WEBHOOK_CONCURRENCY=32`, per-user lock, `FOLLOWUP_MAX_PER_HOUR_PER_APPEAL=5`.
- **Header spoofing на webhook** — secret-based check.
- **SQL injection** — все `text(...)` с bind-params или literals. F-string interpolation в SQL отсутствует.
- **Timing attacks** — `hmac.compare_digest` в webhook secret. GITHUB_PAT не сравнивается с user-input.
- **SSRF/path-traversal в attachments** — `uploads.py` не fetch URL извне, только local files via `bot.upload_media(InputMedia(path=...))`.
- **Whitelist bypass** (`@`, suffix, hyphen, cyrillic homograph, `javascript:`) — все 5 классических обходов блокированы `urlparse().hostname + endswith("." + suffix)`.
- **Broadcast logging** — `broadcast.py` не пишет текст рассылки в audit (явный скип `services/operators.write_audit`).

---

## План фиксов

### Batch B — Code-fixes (один PR, после согласования)

Низкорисковые, узкие правки. Один PR пройдёт CI как atomic-юнит.

| ID | Действие | Объём |
|---|---|---:|
| H1-fix | Sanitize `operator_name` в `_build_pr_body`: `replace("\n", " ").replace("\r", " ")[:120]` | 5 строк + тест |
| H2-fix | Quote vars + validate `ADMIN_GROUP_ID` числом в `healthwatch.sh` | 15 строк + smoke |
| M1-fix | `appeal.py:490,492` → debug-only, вырезать payload (оставить prefix) | 10 строк + тест |
| M3-fix | Outgoing operator reply URL whitelist — re-use `_is_whitelisted_url` | 20 строк + тест |
| M4-fix | `emergency_contacts.phone` format validation (regex `^[\d\s\+\(\)\-]+$`) | 10 строк + тест |
| M2-fix | Cron-job stale operators cleanup (compare `get_chat_members` с `operators`, deactivate missing) | 60 строк + тест |
| M7-fix | Quote vars в `init-letsencrypt.sh` | 5 строк |

### Batch A — Copy / Product (отдельные PR'ы или один docs-PR)

| ID | Действие | Объём |
|---|---|---:|
| C4-fix | `seed/welcome.md` блок «❌ Что бот НИКОГДА не делает» (паспорт / СНИЛС / банк / деньги) | 15 строк копи |
| C5-fix | `seed/welcome.md` — указать реальный username бота для self-verification жителя | 3 строки копи |
| C6-fix | `docs/Политика_v2.md` — warning про attachment URL leak в MAX, рекомендация не делиться чувствительными вложениями | 10 строк копи |

### Batch C — Решение требуется (decision before fix)

| ID | Что | Варианты |
|---|---|---|
| C1 | **Welcome/consent dormant** | (a) подключить `texts.WELCOME` к `settings_store.get("welcome_text")` с fallback + markdown-sanitizer; (b) удалить UI ветки + миграция данных. **Спросить у владельца** |
| C2 | **Broadcast spoofing** | (a) two-man approval (один оператор draft → другой confirm); (b) cooldown 5 мин между подтверждением и реальным отправлением (отмена возможна); (c) URL whitelist на `broadcast.text`. Каждый — отдельный PR. **Спросить приоритет** |
| C3 | **Operator 2FA / PIN** | Большой track. Возможные варианты: (a) PIN-код, проверяемый через личку другого бота-секретаря (отдельная инфра); (b) одноразовый код через SMS (требует доп-инфры); (c) PIN, хранящийся в `operators.pin_hash`, запрашиваемый перед `/broadcast`/`/op_add`/`/erase`. **Дискуссия с владельцем** |

### Batch D — Accept / Document (no action)

| ID | Что | Reason |
|---|---|---|
| C6 | MAX attachment URL leak | Платформенный, не fix-able в боте |
| M6 | TLS pinning отсутствует | aiohttp + системный CA — стандартная практика, pinning добавляет операционный risk при ротации сертификата |
| M5 | Followup link injection (incoming) | Покрывается M3-fix (whitelist на outgoing), плюс политика «оператор не кликает на ссылки из обращений жителей» в RUNBOOK |
| M8 | GitHub API без schema validation | `.get()` с дефолтами безопасен; добавить full schema = overengineering |
| M9 | `/export` без size-limit | LIMIT 500 уже есть в коде; OOM unlikely. Можно стратегически дополнить streaming-export позже |
| M10 | `/setting json.loads`-extras | Известное проектное решение (forward-compat extras разрешены) |
| M1b/M1c | Docker json-file logs | Решается infra-стороной (log rotation + sanitize layer). Отдельный track, не в этом PR |

---

## Дальше

1. **Открыть Batch B** (один PR, code-fixes) — на ревью.
2. **Открыть Batch A** (docs-PR, copy) — на ревью.
3. **Спросить владельца** по Batch C — это решения, не реализация.
4. **Зарегистрировать** Batch D как «known limitations» в `docs/SECURITY.md §10b`.

> Это первый сводный проход. Следующий — после внедрения Batch B/A + решений Batch C.
