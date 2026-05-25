# MAX-specific bot threats (2025-2026)

Аудит поверхности атаки aemr-bot против class'ов угроз, специфичных для платформы MAX (max.ru, бывший TamTam). Источник угроз: dev.max.ru/docs-api, публичные CVE / репорты, исследование maxapi 1.1.0 SDK и кода aemr-bot. HEAD = `a2a2b87`.

Базовый факт о платформе: **MAX отказался от webhook-подписи** в виде HMAC — единственный механизм аутентификации входящего вебхука — статическое сравнение заголовка `X-Max-Bot-Api-Secret` с секретом, переданным при `POST /subscriptions`. Подписи payload как в Telegram/LINE/Stripe нет. С 25 мая 2025 MAX запретил HTTP-вебхуки и self-signed TLS.

---

### CVE-class 1: Reply / sender spoofing внутри update
**Угроза:** MAX-сервер доставляет update с `sender.user_id` и `recipient.chat_id`. Если бот доверяет этим полям без cross-check (например, оператор пишет из админ-группы и handler берёт `user_id` без проверки, что чат = `admin_group_id`), злоумышленник из личного чата может выдать payload, имитирующий оператора.
**Сценарий атаки:** Житель присылает в личку сообщение с тем же telegram-like callback payload `op:close:123`, что нажимают операторы. Без admin-chat фильтра handler выполнит «закрыть обращение».
**Защита MAX SDK:** maxapi 1.1.0 — нулевая. SDK десериализует payload и доверяет полям из update'а как есть. Никакой проверки подписи отдельных полей нет.
**Защита aemr-bot:** двухслойная — (a) `handlers/__init__.py:108-110` регистрирует middleware и далее `handlers/_auth.py:21-32` `get_operator()` проверяет `is_admin_chat(event)` ДО lookup в БД; (b) `handlers/callback_router.py:108-110` `is_admin_callback()` плюс explicit allowlist `admin_allowed=True` на каждом маршруте `op:*`/`broadcast:*`. Любой `op:close:42` из личного чата отбрасывается на уровне auth, до бизнес-логики.
**Статус:** ✅ защищены (chat_id-binding везде на admin actions).

### CVE-class 2: Callback_id replay
**Угроза:** MAX callback не несёт встроенного nonce / TTL. `callback_id` уникален внутри одного клиента, но MAX повторяет недоставленные событие при сетевом разрыве. Без идемпотентности handler может выполнить мутацию дважды (закрыть → переоткрыть → закрыть).
**Сценарий атаки:** Сетевой джиттер или manual retry в long-polling → `op:erase:42` доходит дважды → audit лог пишет два erase-события для одного обращения, real ПДн удаляются один раз (UNIQUE на appeal_id), но идёт несогласованность с counters.
**Защита MAX SDK:** нет. SDK даже не дедуплицирует update по marker.
**Защита aemr-bot:** `services/idempotency.py:31-79` `build_idempotency_key()` собирает ключ из (`update_type`, `cb.callback_id`, `mid`, `seq`, `ts`, `chat_id`, `user_id`); `claim()` через `pg_insert(...).on_conflict_do_nothing` гарантирует одноразовое выполнение. Critical: `idempotency.py:131-142` — fail-CLOSED при сбое БД (SEC #7), attacker не может induce DB stall и заставить replay. Middleware `handlers/__init__.py:14-20` ставит idempotency outer-middleware ПЕРВЫМ.
**Статус:** ✅ защищены (storage-backed, fail-closed).

### CVE-class 3: Attachment_id cross-chat / cross-tenant access
**Угроза:** [Подтверждённый репорт abit.ee, 2025](https://abit.ee/en/soft/messengers/max-messenger-vulnerability-security-private-messages-photo-leak-web-version-en): direct-URL вложений в MAX публично доступны **без авторизации**, паттерн URL предсказуем, ссылка остаётся живой ~неделю после удаления. Это означает: любая картинка, попавшая в admin chat (фото жителя, скрин паспорта в обращении), может быть выкачана третьим лицом, знающим/угадавшим URL.
**Сценарий атаки:** Утечка одного URL вложения из логов nginx / mes monitoring → enumeration рядом стоящих ID → выкачка всех фото за период.
**Защита MAX SDK:** нет — это вне SDK, проблема платформы. MAX не выпустил фикс на момент аудита.
**Защита aemr-bot:** **отсутствует** — `services/uploads.py:23-75` использует `bot.upload_media` и возвращает токен, далее `bot.send_message(attachments=[file_attachment(token)])` — мы доверяем платформе хранение. Бот не делает re-encrypt / не хранит attachment-копий своих.
**Статус:** 🔴 **остаточная уязвимость платформы** — не fixable со стороны бота. Митigation: документировать риск для жителей в политике (PDN), не запрашивать сканы паспортов, минимизировать фото вложений в обращениях. Файлы политики у нас уже не в чате (PDF через `policy_service.ensure_uploaded`), но attachments жителя при отправке обращения хранятся у MAX.
**PoC sketch:** прислать боту фото → скопировать URL из network-tab клиента MAX → открыть в incognito без cookies → файл доступен.

### CVE-class 4: Webhook secret leak / weak comparison
**Угроза:** MAX шлёт секрет в каждом запросе plain-text (нет HMAC). Если webhook-handler сравнивает через `==`, возможен timing-oracle. Если секрет логируется в access-log или попадает в exception trace — утечка.
**Защита MAX SDK:** maxapi 1.1.0 в `dp.handle_webhook` принимает `secret=` и сравнивает (см. `dispatcher.py`). Используется ли там constant-time — не подтверждено документацией.
**Защита aemr-bot:** `main.py:144-155` — handler `_max_webhook` использует `hmac.compare_digest(got, settings.webhook_secret)`, секрет читается ТОЛЬКО из заголовка `X-Max-Secret` (не из query — комментарий в коде об этом явно). 403 при mismatch.
**Статус:** ✅ защищены (constant-time + header-only). **Caveat:** дефолтный режим = polling (`bot_mode == "polling"`, `main.py:88`), webhook отмечен как dead-but-not-removed. Если включат webhook — handler готов.

### CVE-class 5: Long-polling MITM / DNS spoofing
**Угроза:** Long-polling = бот сам ходит на `botapi.max.ru` / `platform-api.max.ru`. При DNS-spoofing/MITM attacker может вернуть подделанный update'ы, в т.ч. от имени любого user_id. BOT_TOKEN при этом утечёт тоже (Authorization header).
**Защита MAX SDK:** maxapi пользуется aiohttp, который проверяет TLS-цепочку дефолтно. Cert-pinning отсутствует.
**Защита aemr-bot:** мы не отключаем TLS verify (grep `verify=False` — пусто в коде бота, единственный hit в `services/cron.py` — это APScheduler-конфиг, не TLS). Доверяем системному CA-bundle на VPS. **Cert-pinning к `*.max.ru` мы НЕ делаем.**
**Статус:** 🟡 частично — TLS включён, но без pinning. Принимаем риск compromise системного CA store на VPS как low (selfhost, контроль конфигурации). Mitigation: monitor VPS audit + periodically check `bot.get_me()` для аномалий через `_preflight_check_token` (`main.py:213-245`).

### CVE-class 6: BOT_TOKEN leak через логи / traceback
**Угроза:** Любой `exception` в admin chat / traceback с request URL мог бы вылить token в `?access_token=...`. Plus, `_register_bot_commands` (`main.py:175-210`) кладёт token в `Authorization` header при aiohttp вызове.
**Защита MAX SDK:** maxapi после миграции на header-only auth не пишет token в URL. Старый query-style теперь возвращает 401.
**Защита aemr-bot:** (a) `main.py:194-200` использует `Authorization: <token>` (header, не query) — utечки в access-log нет; (b) traceback'и из `log.exception` пишутся в stdout docker логи, не пересылаются в admin чат (`_build_admin_senders` отдельный sink, не подключён к stderr/exception); (c) sentry/error tracking отсутствует — нет third-party exfiltration. **Caveat:** preflight error `"❌ BOT_TOKEN неверный..."` не содержит сам токен (`main.py:230-234`). Конфиг через env (`infra/.env`), не commit'нут.
**Статус:** ✅ защищены (header-only auth, нет sentry, traceback не уходит наружу).

### CVE-class 7: Bot impersonation / squatting
**Угроза:** Злоумышленник регистрирует `@aemr_elizovo_bot` (имя похожее на наш) и убеждает жителей слать туда обращения / ПДн.
**Защита MAX SDK:** N/A.
**Защита платформы MAX:** [Habr-репорт, август 2025](https://habr.com/ru/articles/951326/) — публикация ботов в MAX **ТОЛЬКО через верифицированные юрлица РФ**, модерация ≤24ч, ИП/самозанятые/физлица заблокированы. Это нативная защита: атакер должен зарегистрировать левое юрлицо и пройти модерацию — высокий barrier.
**Защита aemr-bot:** N/A — это внеcкоп бота. Mitigation: разместить «официальная ссылка t.me/max — вот эта» на сайте администрации, периодически проверять MAX search на имена-двойники.
**Статус:** ✅ защищены (платформенная модерация — сильнее Telegram).

### CVE-class 8: DM-flooding / DoS на одну личку или admin chat
**Угроза:** Житель шлёт 1000 mes/sec в свою личку с ботом → handler queue растёт, БД throttle, остальные обращения не обрабатываются. Или: жалует «followup» 10к раз → admin chat завален.
**Защита MAX SDK:** MAX API имеет глобальный rate-limit ~30 req/sec на bot-токен (исходящие). Входящие НЕ лимитированы платформой.
**Защита aemr-bot:** (a) `main.py:54` `_WEBHOOK_CONCURRENCY = 32` ограничивает параллельные handler-таски при webhook (защита от OOM в 512m mem_limit); (b) per-user lock `appeal_runtime.py:37-53` `_user_locks` — один сabсemaphore на max_user_id, параллельные сообщения сериализуются; (c) **SEC #5** `config.py:138-143` — `FOLLOWUP_MAX_PER_HOUR_PER_APPEAL=5` + `FOLLOWUP_MIN_INTERVAL_SECONDS=30`, реализовано в `services/appeals.py:189` `followup_rate_limit_stats`; (d) polling-режим natural rate-limited GetUpdates таймаутом 30s.
**Статус:** ✅ защищены на трёх уровнях (concurrency-cap, per-user-lock, app-level rate-limit на followup).
**Caveat:** глобального per-user-throttle на «новые обращения» нет (только per-appeal followup-throttle). Один житель может слать 5 новых обращений в минуту — это ляжет в БД, попадёт в admin chat. Не блокирующий, но noisy.

### CVE-class 9: Operator permission stale после выхода из admin-группы
**Угроза:** ИТ-оператор покидает MAX-группу `admin_group_id` (сам уволился / удалён группы), но запись `operators.is_active=true` остаётся в БД. Если он ещё в каком-то shared чате и попадёт callback... защищён `is_admin_chat` (см. CVE-1). Но если admin-group_id поменяли по ошибке — все операторы валидны на новый chat_id.
**Защита MAX SDK:** maxapi exposes `bot.get_chat_members()` (`admin_operators.py:88-98` `_safe_get_chat_members`), но активного cleanup-watcher на bot_removed event ботом не делается.
**Защита aemr-bot:** `admin_operators.py` использует list_active при отображении operator list, но deactivate выполняется ТОЛЬКО через ручной wizard в админ-меню. Автоматического «при leave из чата → deactivate» — **НЕТ**. Audit-trail хранится 365 дней (`config.py:129-131` `audit_log_retention_days`).
**Статус:** 🟡 частично — auth-проверка работает потому что `is_admin_chat` блокирует non-admin-chat, **но** мёртвые operators остаются в БД до ручной чистки IT. Mitigation: периодически IT прогоняет `op:operators` и видит stale. Recommendation (out-of-scope): cron job, сверяющий `operators.is_active` с `get_chat_members(admin_group_id)`.

### CVE-class 10: User-Agent / handle_webhook header spoofing
**Угроза:** Если webhook включён и доступен из публичной сети, attacker шлёт прямые POST с подделанными `X-MAX-*` headers, минуя MAX-сервер. С известным секретом — выдаёт fake events.
**Защита MAX SDK:** SDK не валидирует source IP, не проверяет User-Agent.
**Защита aemr-bot:** `hmac.compare_digest` на `X-Max-Secret` (`main.py:144-155`). Source IP / `X-Forwarded-For` / User-Agent **не проверяются** — это OK при наличии секрета (MAX не публикует whitelist IP). Если секрет утёк — никакого второго фактора.
**Статус:** ✅ защищены при условии целостности секрета (это инвариант всех webhook-платформ без HMAC).

---

## Сводная таблица

| # | Класс угрозы | Статус | Кто защищает |
|---|---|---|---|
| 1 | Reply / sender spoofing | ✅ | aemr-bot (chat-binding) |
| 2 | Callback replay | ✅ | aemr-bot (`idempotency.py`) |
| 3 | Attachment cross-chat URL | 🔴 | НИКТО (платформа MAX) |
| 4 | Webhook secret comparison | ✅ | aemr-bot (`hmac.compare_digest`) |
| 5 | Long-polling MITM | 🟡 | aiohttp TLS, без pinning |
| 6 | BOT_TOKEN leak | ✅ | aemr-bot (header-only auth) |
| 7 | Bot impersonation | ✅ | MAX (верификация юрлица) |
| 8 | DM-flooding / DoS | ✅ | aemr-bot (3 уровня лимитов) |
| 9 | Operator stale после leave | 🟡 | ручная чистка через UI |
| 10 | Header spoofing на webhook | ✅ | secret-based |

## Главные TODO

1. **CVE-3 (attachment URL)** — единственная hard уязвимость, fixable только платформой MAX. Действие: в `texts.py` / политике явно предупредить жителей «не присылайте сканы документов»; рассмотреть удаление attachments из БД сразу после закрытия обращения (defense-in-depth даже если URL у MAX живёт).
2. **CVE-9 (stale operators)** — реализовать cron в `services/cron.py`: ежесуточно сверять `operators.is_active=true` с `get_chat_members(admin_group_id)`, разница → deactivate + audit-log.
3. **CVE-5 (no cert pinning)** — accept как known limitation; периодически проверять system CA store на VPS.

---

## Sources

- [Требования к Webhook-endpoint — MAX для разработчиков](https://dev.max.ru/docs-api/methods/POST/subscriptions)
- [MAX изменил правила: публиковать ботов только через верифицированные юрлица РФ — Хабр](https://habr.com/ru/articles/951326/)
- [Security Flaw in Max Messenger: Private Message Photos Accessible Without Login — abit.ee](https://abit.ee/en/soft/messengers/max-messenger-vulnerability-security-private-messages-photo-leak-web-version-en)
- [maxapi · PyPI](https://pypi.org/project/maxapi/) / [github max-messenger/max-botapi-python](https://github.com/max-messenger/max-botapi-python)
- [maxapi (love-apples/maxapi) on GitHub](https://github.com/love-apples/maxapi) — наш SDK
- [Сравнение Max Bot API и Telegram Bot API — vc.ru](https://vc.ru/telegram/2799410-sravnenie-max-bot-api-i-telegram-bot-api)
- [MAX Messenger plugin for Claude Code — github MAVII-RU](https://github.com/MAVII-RU/max-messenger-plugin) (источник rate-limit 30 req/sec)
- [Webhook Security Best Practices 2025-2026 — DEV Community](https://dev.to/digital_trubador/webhook-security-best-practices-for-production-2025-2026-384n)
- [TamTam Bot API security overview](https://github.com/tamtam-chat/tamtam-bot-api/security) (legacy под MAX)
