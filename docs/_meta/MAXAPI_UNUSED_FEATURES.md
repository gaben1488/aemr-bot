# Неиспользуемые фичи maxapi 1.1.0

> Снимок на 2026-05-25. Перечисляет API, которые библиотека предоставляет,
> но мы не зовём. Не «обязательно перейти», а «знать, что есть, и
> сознательно решить — нужно или нет».

## 1. Топ-5 заметных пропусков

### 1.1. `maxapi.utils.formatting` — структурное форматирование HTML/Markdown

В `utils/formatting.py` (см. /maxapi/utils/formatting.py) есть полный
DSL для сборки форматированного текста: `Bold`, `Italic`, `Underline`,
`Strikethrough`, `Code`, `Heading`, `Highlighted`, `Blockquote`, `Link`,
`UserMention`, `Text`, плюс `as_html(...)` и `as_markdown(...)`.

Мы сейчас собираем HTML руками строками-шаблонами в `texts.py`,
`card_format.py`, `progress.py`. Это работает, но даёт шанс
забыть HTML-escape для пользовательского ввода (`<`, `>`, `&`).
Эпизоды XSS в боте нет (MAX рендерит наши сообщения, не браузер),
но при копировании текста с тэгами наружу — потенциально проблема.

**Что выиграем.** Перевод hot-path сборок (карточка обращения,
admin-card timeline, progress-карточка рассылки) на `Bold(...)`,
`Link(...)`, `UserMention(...)` — гарантированный escape без ручного
`.replace("<", "&lt;")` и более читаемый код. Особенно полезно для
`UserMention` в admin-уведомлениях о подписке — там сейчас раскрытие
имени жителя пишется в plain-тексте.

**Стоимость.** Большая миграция (десятки шаблонов). Не приоритет, но
для новых сообщений — использовать.

### 1.2. `bot.send_action(chat_id, SenderAction.TYPING_ON)` — индикатор «бот печатает»

В личке жителя долгие операции (загрузка PDF политики, retrieval
прошлых обращений, формирование статистики) занимают 1-3 секунды.
Без typing-индикатора житель не понимает, что бот живой.

**Что выиграем.** UX-полировка: в начале долгой операции — один
`await bot.send_action(chat_id, SenderAction.TYPING_ON)`. MAX покажет
«печатает» до следующего сообщения. Особенно ценно в /broadcast-wizard
(оператор подтверждает рассылку, бот несколько секунд готовит снимок
аудитории) и в `handlers/menu.py` при открытии «📂 Мои обращения» с
большим списком.

**Стоимость.** Тривиальная (один вызов в каждой долгой ветке). Не
блокирует, fire-and-forget.

### 1.3. `bot.delete_message(message_id)` — удаление подсказок воронки

Сейчас в воронке обращения мы редактируем prompt («введите населённый
пункт» → «выберите тему»), но мусорные intermediate-сообщения от
жителя (отменённый ввод, дубликаты) остаются висеть в личке.

**Что выиграем.** После `/cancel` или таймаута wizard'а — `await
bot.delete_message(prompt_mid)` подчищает прогресс-карточку, чтобы
житель не возвращался к мёртвой воронке через несколько часов.

**Стоимость.** Низкая. Главный риск — пытаться удалить чужое
сообщение (житель удалил аккаунт → mid невалидный). Ловить
`MaxApiError` и игнорировать.

### 1.4. `bot.pin_message(chat_id, message_id)` для admin-панели

Метод вызывается в одном месте (`admin_panel.py:80`) — закрепляет
служебное сообщение «бот живой». Но `bot.get_pin_message(chat_id)`
для проверки уже закреплённого не используется — каждый запуск
повторно пинит, MAX сам дедупит, но при перезапуске бот пишет
«закреплено» в логи, даже если оно и так было.

**Что выиграем.** Перед `pin_message` — `await
bot.get_pin_message(cfg.admin_group_id)`; если mid совпадает —
skip. Меньше шума в audit_log.

**Стоимость.** Низкая, локальная правка в `admin_panel.py`.

### 1.5. `dp.handle_webhook(...)` вместо `dp.init_serve(...)` (выполнено)

`init_serve` deprecated в 1.1, обёртка над `handle_webhook`. Уже
исправлено в этой ревизии (`main.py:365`). Webhook-режим в проекте
dead-but-not-removed (BOT_MODE=polling), но при включении не будет
плодить DeprecationWarning.

## 2. Дополнительные пропуски (P2-P3)

### 2.1. `maxapi.utils.message_link` — `build_message_link(mid)`

Готовая функция «mid → URL https://max.ru/c/{chat_id}/{seq_b64}».
Сейчас мы строим reply-link через `NewMessageLink(type=REPLY, mid=...)`
— это правильно. Но для **внешнего** копирования ссылки (например, в
аудит-логе «оператор X ответил на обращение Y, см.
{message_link}») у нас нет helper'а. Если такой кейс возникнет —
взять отсюда, не писать свой base64.

### 2.2. `bot.set_my_commands(*commands)` — публикация /-меню

В 1.1 помечен как deprecated с пояснением «нет в swagger MAX API».
Мы это знаем (см. `main.py:_register_bot_commands` — ручной aiohttp
PATCH `/me`). При появлении в swagger MAX и удалении DeprecationWarning
из maxapi — вернуться к `bot.set_my_commands(...)` и убрать костыль.

### 2.3. `Router(router_id="...")` — раздельные роутеры по группам хендлеров

Сейчас все хендлеры регистрируются в одном `Dispatcher`. При расширении
проекта (например, добавление чата-канала, отдельный handler-набор
для модерации) — `Router` даёт логическую группировку с
`dp.include_routers(router_main, router_admin, router_broadcasts)`.
Каждый Router имеет свои `outer_middlewares`, фильтры и `router_id`
для логов. Не критично сейчас, но если файлы хендлеров перевалят за
4000 строк (admin_settings уже 1000+) — стоит подумать.

### 2.4. `dp.register_inner_middleware(mw)` — middleware после фильтров

Сейчас зарегистрированы только outer middleware (`IdempotencyMiddleware`,
`AdminChatActivityMiddleware`). Они срабатывают на каждое событие,
включая «проигнорированные» (нет подходящего хендлера). Inner
middleware вызывается только когда конкретный handler матчит фильтры
и реально пойдёт исполняться. Это полезно для:
- логирования факта «handler X вызван» с детальной трассировкой
- pre/post-hook'ов на handler-уровне (например, авто-`commit()`
  на сессии БД после успешного handler'а)
- per-handler метрик задержки

Сейчас всё это сделано вручную внутри отдельных handler'ов; inner
middleware дало бы единое место и единое декоративное оформление.

### 2.5. `Dispatcher(storage=RedisContext, ...)` — внешний стейт FSM

Сейчас `MemoryContext` (in-memory dict). На перезапуске процесса —
вся wizard-state теряется. Mitigation у нас своя:
`services/wizard_persist.py` (миграция 0011) — отдельно вытаскивает
operator-wizards из БД при рестарте. Если бы строили с нуля —
`RedisContext` от maxapi решал бы это бесплатно. Сейчас не имеет
смысла переписывать, но если придётся выносить FSM-state из
SQLite/Postgres — рассмотреть.

### 2.6. `bot.upload_media(InputMedia | InputMediaBuffer) → AttachmentUpload`

Используется. Но `bot.download_file(url, destination)` — нет.
Нам не нужно скачивать вложения жителей (мы их relay'им
по token'у). Если когда-нибудь будем делать архив-снимки
(скачать вложения → ZIP → пользователю в обратную сторону) —
этот метод готов.

### 2.7. `bot.get_subscriptions()` — диагностика подписок webhook

В polling-режиме maxapi сам проверяет (`auto_check_subscriptions=True`)
и warning'ует, если есть оставшиеся webhook-подписки от предыдущего
владельца токена. Мы это видим только в логах maxapi. Полезно
завернуть в `/diag` (handlers/admin_diag) — оператор-IT мог бы тапнуть
и сразу увидеть, что токен «загрязнён» подписками от тестового
webhook-режима.

## 3. Не нужны / не подходят

- `bot.change_info(...)` — отсутствует в swagger MAX. Используем
  ручной PATCH /me (см. `main.py:_register_bot_commands`).
- `bot.set_my_commands(...)` — то же.
- `bot.subscribe_webhook(...)` / `bot.unsubscribe_webhook(...)` /
  `bot.delete_webhook(...)` — webhook-режим dead, polling-only.
- `bot.add_list_admin_chat(...)` / `bot.remove_admin(...)` /
  `bot.kick_chat_member(...)` — мы не управляем составом
  служебного чата программно (это делают руками админы группы).
- `Bot(parse_mode=...)` — deprecated, мы передаём `format` через
  `_resolve_format` неявно (default — None → plain text).

## 4. Что добавить в следующий бэклог

Подходит для отдельных мелких PR (не блокирующие):

- [ ] **kaizen #1**: typing-indicator в /broadcast confirm
  (`bot.send_action(chat_id, SenderAction.TYPING_ON)` перед снимком
  аудитории).
- [ ] **kaizen #2**: typing-indicator в `handlers/menu.py:my_appeals`
  при listing'е > 10 обращений.
- [ ] **kaizen #3**: cleanup мусорных promt'ов воронки через
  `bot.delete_message(...)` на `/cancel` и таймауте.
- [ ] **kaizen #4**: `bot.get_pin_message(...)` перед `pin_message(...)`
  в `admin_panel.py` — skip redundant pin.
- [ ] **kaizen #5**: `/diag` показывает `bot.get_subscriptions()` —
  проверка чистоты токена.

Каждый — одна-две строки кода, тест в существующем
`test_handlers_*.py` для регрессии.
