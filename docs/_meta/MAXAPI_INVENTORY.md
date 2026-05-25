# Inventory использования maxapi 1.1.0 в aemr-bot

> Снимок на 2026-05-25 после апгрейда 0.9.18 → 1.1.0 (PR #50).
> Источник версии — `bot/pyproject.toml` (`maxapi~=1.1`), фиксация —
> `bot/uv.lock`. Guard на drift — `tests/test_deps_environment.py`.

## 1. Все импорты `maxapi.*` в коде бота

Грепнуто `from maxapi`. Тестовые мокающие фикстуры (`bot/tests/`) исключены.

### `bot/aemr_bot/main.py`

| line | импорт | использование |
|------|--------|---------------|
| 8 | `from maxapi import Bot, Dispatcher` | конструкция `bot` и `dp` на module-level |
| 9 | `from maxapi.client.default import DefaultConnectionProperties` | таймауты HTTP-клиента и retry-policy |
| 10 | `from maxapi.exceptions.max import InvalidToken` | preflight-проверка токена в `_preflight_check_token` |
| 138 | `from maxapi.methods.types.getted_updates import process_update_webhook` | парсинг JSON в FastAPI webhook-режиме (dead-but-not-removed путь) |

### `bot/aemr_bot/handlers/__init__.py`

| line | импорт | использование |
|------|--------|---------------|
| 1 | `from maxapi import Dispatcher` | тип-хинт в `register_handlers(dp)` и `_attach_outer_middleware` |
| 2 | `from maxapi.filters.middleware import BaseMiddleware` | базовый класс для `IdempotencyMiddleware` и `AdminChatActivityMiddleware` |

### `bot/aemr_bot/handlers/start.py`

| line | импорт | использование |
|------|--------|---------------|
| 3 | `from maxapi import Dispatcher` | тип-хинт |
| 4 | `from maxapi.types import BotStarted, Command, MessageCreated` | фильтр и типы событий стартового сценария |

### `bot/aemr_bot/handlers/appeal.py`

| line | импорт | использование |
|------|--------|---------------|
| 23 | `from maxapi import Dispatcher` | тип-хинт |
| 24 | `from maxapi.types import MessageCallback, MessageCreated` | catch-all обработчик жителя |

### `bot/aemr_bot/handlers/admin_commands.py`

| line | импорт | использование |
|------|--------|---------------|
| 26 | `from maxapi import Dispatcher` | тип-хинт |
| 27 | `from maxapi.types import Command, MessageCreated` | slash-команды операторов |

### `bot/aemr_bot/handlers/broadcast.py`

| line | импорт | использование |
|------|--------|---------------|
| 28 | `from maxapi import Dispatcher` | тип-хинт |
| 29 | `from maxapi.types import Command, MessageCreated` | wizard рассылки |

### `bot/aemr_bot/handlers/operator_reply.py`

| line | импорт | использование |
|------|--------|---------------|
| 12 | `from maxapi.types import MessageCreated` | свайп-reply, /reply, intermediate-reply |

### `bot/aemr_bot/handlers/menu.py`

| line | импорт | использование |
|------|--------|---------------|
| 6 | `from maxapi.exceptions.max import MaxApiError, MaxConnection` | best-effort guard в callback'ах меню жителя |

### `bot/aemr_bot/keyboards.py`

| line | импорт | использование |
|------|--------|---------------|
| 1-5 | `from maxapi.types import CallbackButton, LinkButton, RequestContactButton` | сборка inline-клавиатур |
| 6-8 | `from maxapi.types.attachments.buttons.request_geo_location_button import RequestGeoLocationButton` | кнопка геолокации в воронке адреса |
| 9 | `from maxapi.utils.inline_keyboard import InlineKeyboardBuilder` | builder для всех клавиатур |

### `bot/aemr_bot/services/admin_relay.py`

| line | импорт | использование |
|------|--------|---------------|
| 124-125 | `from maxapi.enums.message_link_type import MessageLinkType` + `from maxapi.types.message import NewMessageLink` (lazy) | reply-link при relay вложений жителя в служебную группу |
| 181-182 | то же, дубль для legacy-пути | то же |

### `bot/aemr_bot/services/uploads.py`

| line | импорт | использование |
|------|--------|---------------|
| 26-27 | `from maxapi.enums.upload_type import UploadType` + `from maxapi.types.input_media import InputMedia` (lazy в `upload_path`) | загрузка PDF/XLSX с диска |
| 49-50 | `UploadType` + `InputMediaBuffer` (lazy в `upload_bytes`) | загрузка байтов рассылки |
| 96-97 | `UploadType` + `AttachmentPayload` + `AttachmentUpload` в `file_attachment(token)` | сборка вложения для `send_message(attachments=...)` |

### `bot/aemr_bot/services/progress.py`

| line | импорт | использование |
|------|--------|---------------|
| 153 | `from maxapi.enums.parse_mode import ParseMode` (lazy) | HTML-формат progress-карточек |

### `bot/aemr_bot/services/cron.py`

| line | импорт | использование |
|------|--------|---------------|
| 80-81 | `CallbackButton` + `InlineKeyboardBuilder` (lazy) | inline-клавиатуры под cron-уведомлениями |

### `bot/aemr_bot/utils/attachments.py`

| line | импорт | использование |
|------|--------|---------------|
| 329 | `from maxapi.types.attachments import Attachments` (lazy в `deserialize_for_relay`) | pydantic-валидация JSON-вложений из БД перед relay |

## 2. Свод по подмодулям

| подмодуль | где импортируется | назначение |
|-----------|-------------------|-------------|
| `maxapi` (Bot, Dispatcher) | main + 5 хендлеров | top-level конструкция и тип-хинты |
| `maxapi.types.*` (Command, MessageCreated, MessageCallback, BotStarted, CallbackButton, LinkButton, RequestContactButton, NewMessageLink) | 5 хендлеров + keyboards + admin_relay | публичный API типов событий и UI |
| `maxapi.types.attachments.*` (Attachments, AttachmentUpload, AttachmentPayload, RequestGeoLocationButton) | uploads, keyboards, attachments-util | вложения и кнопки расширенного типа |
| `maxapi.utils.inline_keyboard.InlineKeyboardBuilder` | keyboards, cron | сборка inline-клавиатур |
| `maxapi.enums.*` (UploadType, ParseMode, MessageLinkType) | uploads, progress, admin_relay | константы вместо «магических строк» |
| `maxapi.filters.middleware.BaseMiddleware` | handlers/__init__ | базовый класс middleware |
| `maxapi.client.default.DefaultConnectionProperties` | main | конфигурация HTTP-клиента |
| `maxapi.exceptions.max.*` (InvalidToken, MaxApiError, MaxConnection) | main, menu | диагностика токена и сетевых ошибок |
| `maxapi.methods.types.getted_updates.process_update_webhook` | main (webhook-режим, dead) | парсинг JSON входящего webhook |

Прямых импортов из `maxapi.dispatcher`, `maxapi.bot`, `maxapi.context`,
`maxapi.webhook.*`, `maxapi.methods.*` (за одним исключением выше) —
нет. Публичный фасад `maxapi.{Bot, Dispatcher, types, enums, filters,
utils}` покрывает 100% использования.

## 3. Bot-методы, которые мы реально вызываем

Source: grep `bot\.<method>` и `event\.bot\.<method>` (см. agent-grep
2026-05-25).

| метод | где используется | примечания |
|-------|-----------------|-----------|
| `bot.send_message` | main, admin_card, admin_bus, admin_relay, progress, cron, broadcast, admin_panel, admin_stats, admin_audience, admin_settings, admin_appeal_ops, admin_operators | основной канал отправки |
| `bot.edit_message` | admin_card, progress, broadcast (admin progress card) | edit-vs-send_new по freshness-rule |
| `bot.get_chat_members` | admin_operators (add-from-group flow) | список участников группы |
| `bot.get_chat_member` | admin_operators (отдельный участник) | реализован как обёртка над `get_chat_members(user_ids=[...])` |
| `bot.pin_message` | admin_panel | закрепить служебное сообщение в admin group |
| `bot.upload_media` | services/uploads | через `InputMedia`/`InputMediaBuffer` |
| `bot.get_me` | main (preflight), внутри Dispatcher (check_me) | проверка токена и логирование @username |
| `bot.get_updates` | main (`_install_polling_timeout` override) | long-poll цикл; обёртка форсит наш timeout |

Методы maxapi 1.1, которые мы **не вызываем явно** (см. раздел 4):
`delete_message`, `delete_chat`, `send_action`, `send_callback`,
`get_message`, `get_messages`, `get_pinned_message`, `change_info`,
`get_chats`, `get_chat_by_link`, `get_chat_by_id`, `edit_chat`,
`get_video`, `delete_pin_message`, `get_me_from_chat`,
`delete_me_from_chat`, `get_list_admin_chat`, `add_list_admin_chat`,
`remove_admin`, `add_chat_members`, `kick_chat_member`,
`download_file`, `set_my_commands`, `get_subscriptions`,
`subscribe_webhook`, `unsubscribe_webhook`, `delete_webhook`.

## 4. Deprecated-паттерны: проверены и отсутствуют

Грепнул на наличие deprecated-форм из maxapi 1.1:

| deprecated | maxapi-замена | в aemr-bot |
|-----------|---------------|-----------|
| `dp.middlewares = [...]` (setter) | `dp.outer_middlewares = [...]` | не используется |
| `dp.middleware(mw)` | `dp.register_outer_middleware(mw)` | не используется |
| `dp.outer_middleware(mw)` (старый insert(0, ...)) | `dp.register_outer_middleware(mw)` (append) | не используется |
| `dp.middlewares` (property getter) | `dp.outer_middlewares` | не используется |
| `bot.send_message(parse_mode=...)` | `format=...` | не используется (передаём `format=fmt`) |
| `bot._resolve_parse_mode(...)` | `bot.resolve_format(...)` | не используется (это приватный helper) |
| `bot.change_info(...)` | (отсутствует в swagger; recommend handcrafted PATCH /me) | заменено на ручной aiohttp PATCH в `_register_bot_commands` |
| `bot.set_my_commands(...)` | то же | заменено на ручной aiohttp PATCH |
| `dp.init_serve(...)` | `dp.handle_webhook(...)` | **исправлено в этой ревизии** (см. ниже) |

Изменение в этой ревизии: `main.py:365` теперь зовёт
`dp.handle_webhook(...)` вместо `dp.init_serve(...)`. Webhook-режим
по проекту dead-but-not-removed (BOT_MODE=polling — единственный
рабочий), но при случайном включении не будет валить
`DeprecationWarning` в логах.

## 5. Известные «слабые места» компатимости

Эти места требуют внимания при следующем bump'е maxapi:

1. **`utils/attachments.py` — `deserialize_for_relay`**.
   `pydantic.TypeAdapter(Attachments)` валидирует JSON-словарь из БД.
   Изменение схемы Pydantic-моделей вложений в maxapi 2.x теоретически
   сломает relay уже сохранённых вложений. Mitigation: миграция
   `0009_attachments_jsonb` — данные пересохранятся свежим pydantic-
   `.model_dump()` при первом relay; ловим `ValidationError` и
   пропускаем битое вложение, а не валим всю отправку.

2. **`services/uploads.py` — `InputMediaBuffer(buffer=..., type=...)`**.
   Сигнатура нестабильна между минорами. Helper уже падает на `TypeError`
   → fallback на временный файл через `InputMedia(path=...)`. При
   следующем bump'е проверить, что fallback не пропадает.

3. **`main.py:138` — `process_update_webhook`**. Прямой импорт
   из приватного пути `maxapi.methods.types.getted_updates`.
   В 1.1.0 функция там есть, в 2.x путь может смениться. Сейчас
   попытка обёрнута в `try/except ImportError` → переменная становится
   `None`, обработчик при включении webhook-режима молча игнорирует
   входящий update (плохо, но не падение). Если webhook-режим вернётся
   к жизни — переписать на `await dp.handle(event)` после публичного
   парсинга через `dp.handle_webhook` напрямую.

4. **`main.py:_install_polling_timeout`** — патчит `bot.get_updates`
   через monkey-patch. Если maxapi изменит сигнатуру `get_updates`
   (например, добавит обязательный `*, mode=...`), наш override
   подменит её несовместимым closure. Mitigation: тест
   `test_default_connection_signature_matches_prod_api` ловит drift
   только в `DefaultConnectionProperties.__init__`. Добавить
   аналогичный guard на `Bot.get_updates` стоит, если будут проблемы.

## 6. Тесты, имитирующие maxapi

Лежат в `bot/tests/`. Не тащат сетевые вызовы, а мокают на уровне
методов `bot`. Где приходится «знать» внутренний тип maxapi:

| тест | импорт maxapi |
|------|--------------|
| `test_extract_location.py:34` | `maxapi.types.attachments.location.Location` |
| `test_deps_environment.py:65` | `maxapi.client.default.DefaultConnectionProperties` |
| `test_handlers_menu.py:604` | `maxapi.exceptions.max.MaxApiError` |
| `test_main_helpers.py:190` | `maxapi.exceptions.max.InvalidToken` |

Все четыре — это внешние якоря, которые валятся раньше прода при
breaking change. Менять при апгрейде только если падают.
