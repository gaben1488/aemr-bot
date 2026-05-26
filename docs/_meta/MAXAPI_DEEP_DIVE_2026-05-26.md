# maxapi 1.1.0 — глубокая разведка для AEMR-bot

> 2026-05-26. Источник: `bot/.venv/Lib/site-packages/maxapi/`.
> Дополняет `MAXAPI_INSIGHTS.md` + `MAXAPI_UNUSED_FEATURES.md` — без
> повторов. Ответы на 18 пунктов чеклиста владельца.

## 1. Attachment lifecycle

**`bot.upload_media(media) -> AttachmentUpload`** (`bot.py:1075-1095`)
— hi-level wrapper над `get_upload_url + upload_file_buffer`. Возвращает
`AttachmentUpload(payload.token)`. **Token переиспользуем** —
`send_message` отдельно сериализует уже залитый payload без повторного
upload'а (`send_message.py:150-153`). TTL token'а в swagger не указан,
надо тестировать. **Применимо.** PDF-шаблон политики, который не
меняется — залить один раз, кэшировать token в БД. Экономия ~5с на
запрос × тысячи запросов. **P1, ~4 ч.**

**Retry на `attachment.not.ready` уже встроен** (`send_message.py:184-209`,
`edit_message.py:162-184`): 5 попыток, задержка 2с, опциональный
`after_upload_give_up_timeout`. Параметры конструктора `Bot(after_upload_attempts,
after_upload_retry_delay, after_upload_give_up_timeout)`. **Наш wrapper-loop
в `services/uploads.py:upload_path` дублирует библиотечный — удалить. P2, 1 ч.**

**Presigned-URL / batch upload — нет.** Ограничение swagger.

## 2. Bot management

`bot.change_info(...)` и `bot.set_my_commands(*commands)` помечены
`DeprecationWarning` в библиотеке («отсутствует в swagger MAX»), но
**фактически работают** — мы это знаем (`main.py:_register_bot_commands`
делает ручной aiohttp PATCH `/me`). При появлении в swagger вернуться
на нативные методы и убрать костыль. **P3 (мониторить changelog).**

Аватар бота — через `change_info(photo=PhotoAttachmentRequestPayload(...))`.
При multi-МО инсталляции пригодится для брендинга. **P3.**

## 3. Chat moderation

`bot.kick_chat_member(chat_id, user_id, block=False)` — `block=True`
бывает как ban (`bot.py:1012-1038`). Полный CRUD админов через
`ChatAdminsManager` (`chats.py:158-204`): add, remove, list.

`ChatPermission` (`enums/chat_permission.py`) гранулярнее нашего:
`READ_ALL_MESSAGES, ADD_REMOVE_MEMBERS, ADD_ADMINS, CHANGE_CHAT_INFO,
PIN_MESSAGE, WRITE, CAN_CALL, EDIT_LINK, POST_EDIT_DELETE_MESSAGE,
EDIT_MESSAGE, DELETE_MESSAGE, EDIT, DELETE, VIEW_STATS`.

**Mute / restrict_member — нет** в swagger. Только kick (+block).

**`ChatMembersManager.iter_all(count=100)` / `list_all()`** —
async-итераторы поверх marker-пагинации, **с защитой от цикла marker**
(`_walk_member_pages` raises RuntimeError если marker повторился).
**Заменить ручной цикл `marker=...` в `services/broadcast.py`. P2, 2 ч.**

## 4. Polling

`get_updates(limit ≤ 1000, timeout ≤ 90, marker, types)` — **фильтр
по типам уже есть в библиотеке** (`bot.py:1040-1058`). Но
`start_polling` (`dispatcher.py:1380-1402`) **не прокидывает `types`**
— зашит `bot.get_updates(marker=...)` без них. Чтобы получить
выигрыш — форк start_polling или PR в upstream. **P2.**

`start_polling(skip_updates=True)` — пропустить все события старше
момента старта (`dispatcher.py:1394, 1354-1360`). **Мы не используем
— все updates за downtime обрабатываются.** При долгом простое
лавина устаревших обращений. **Добавить env `SKIP_STALE_UPDATES=true`
для прода. P1, 1 ч.**

`auto_check_subscriptions=True` (дефолт) уже выводит warning при
загрязнении токена webhook-подписками (`dispatcher.py:486-499`).

## 5. Webhook setup

`subscribe_webhook(url, update_types, secret)` (`subscribe_webhook.py`):
- HTTPS обязателен (HTTP даёт warning),
- `secret: 5..256 [A-Za-z0-9-]` → MAX шлёт в заголовке
  `X-Max-Bot-Api-Secret`. `FastAPIMaxWebhook` проверяет автоматически
  через `secrets.compare_digest` (`webhook/fastapi.py:17-39`).
- `update_types: list[UpdateType]` — фильтр (TG `allowed_updates`).

**НЕТ:** `max_connections`, `drop_pending_updates`, `ip_address`.

## 6. Inline mode

**Нет** в MAX swagger. Не реализовать.

## 7. Media groups / Forward / Copy

**Media-group** — `send_message(attachments=[...])` принимает список.
Отдельного метода нет. У нас use case'а нет.

**`message.forward(chat_id|user_id)`** (`types/message.py:478-533`) —
helper уже есть, строит `NewMessageLink(type=FORWARD, mid=...)`.
**Применимо: forward сообщения жителя в operator-чат с сохранением
вложений и markup**, вместо нашего relay-копирования через
`_relay_to_operator` в `appeal_funnel.py`. **P0-P1, ~2 ч.**

**`copy_message` (TG-стиль бесшумная копия) — нет.** Только forward
с пометкой «переслано от».

## 8. Pin / Unpin

`message.pin()` / `message.unpin()` (`types/message.py:613-650`),
`chat.pin(msg)` / `chat.unpin()` (`chats.py:385-402`),
`bot.delete_pin_message(chat_id)`.

`bot.edit_chat(chat_id, pin=mid)` (`bot.py:691-722`) — атомарная
ротация pinned-сообщения через PATCH чата (удобнее `unpin + pin`).
**P3.**

`get_pin_message` перед `pin_message` для дедупа — уже в бэклоге
(MAXAPI_UNUSED_FEATURES kaizen #4).

## 9. User profile / Chat members

`get_chat_member(chat_id, user_id)` возвращает `ChatMember` с
`is_owner, is_admin, permissions[], join_time, last_access_time, alias`.

**Применимо:** sanity-check «оператор всё ещё в чате?» в
`services/operators.py:is_operator` — устранит расхождение БД ↔ MAX
если оператор вышел вручную. **P2, ~3 ч + кеш 60с.**

`get_me_from_chat(chat_id)` (`bot.py:812-831`) — статус бота в чате
(`is_admin`, `permissions[]`). Полезно для `/diag`: «бот в
operator_chat: is_admin=True, есть права WRITE+PIN_MESSAGE». Покажет,
если права обрезали вручную. **P2 kaizen.**

## 10. Reactions / Stickers / Polls / Live location

| Feature | В MAX | В maxapi |
|---|---|---|
| Reactions API | нет | нет |
| Stickers receive | да | `Sticker` attachment |
| Stickers upload | нет | `UploadType` без STICKER |
| Polls | нет | нет |
| Live location | нет | только static `Location` |

Всё — **P3-документировать «не пытаться».**

## 11. Scheduled messages

**Нет native schedule.** Наш cooldown + APScheduler остаются.

## 12. Edit history

API не отдаёт старые версии. `MESSAGE_EDITED` приходит **только с
новым текстом** — старый текст хранить самим в БД при первом
`MESSAGE_CREATED`. **P1 если требование комплаенса, иначе P3.**

## 13. MessageBody — что ещё есть

- `MessageBody.seq: int` — порядковый номер. Server-side ordering
  взамен `timestamp` (тот одинаковый при пачке forward'ов).
- `MessageStat.views: int` — счётчик просмотров. **Применимо: эффективная
  аудитория broadcast'а.** P2, 2 ч.
- `LinkedMessage.sender: User | None` — может быть None при forward от
  канала (`message.py:294`).
- `Message.url` / `build_message_link(mid)` — уже отмечены в INSIGHTS §2.3.

## 14. Rate-limit headers / Retry-After

**Не парсятся библиотекой.** `connection/base.py:148-188` ретраит
**только HTTP 502/503/504** (`DEFAULT_RETRY_STATUSES`,
кастомизируется `default_connection.retry_on_statuses`). **HTTP 429
и `Retry-After` игнорируются** — будет `MaxApiError`, ловить вручную.

При broadcast 1000+ жителей рискуем поймать 429 без бэкоффа.
**Subclass `BaseConnection` с 429 + Retry-After, или PR в upstream.
P1 для production broadcast, ~1 день.**

## 15. Idempotency-key

**Не поддерживается ни maxapi, ни swagger** — headers фиксированы
(`connection/base.py:142-204`). Наш `IdempotencyMiddleware`
(дедуп `MESSAGE_CALLBACK`) — единственный слой защиты.

## 16. API errors классификация

`exceptions/max.py`:
- `MaxApiError(code, raw)` — generic. `code` — HTTP, `raw` — JSON body.
- `MaxConnection` — сеть (DNS, refused).
- `InvalidToken` — 401, останавливает polling (`dispatcher.py:1317-1320`).
- `MaxUploadFileFailed` — upload.

Внутри `raw.get("code")` есть **string-коды** MAX'а: подтверждено
`"attachment.not.ready"` (`send_message.py:185-188`). Других library
не парсит. **Собрать distinct'ы кодов в Sentry tag → классификация
retryable/permanent/auth на нашей стороне. P2, ~2 ч.**

## 17. Update types — что не обрабатываем

Из `enums/update.py` — НЕ используем активно:

- **`MESSAGE_REMOVED`** — житель удалил сообщение обращения. Сейчас
  не реагируем — текст остаётся в БД. Audit-trail
  «житель отозвал». **P2, 2 ч.**
- **`BOT_STOPPED`** — житель остановил бот (TG /stop-аналог). Сейчас
  продолжаем слать. **Убрать из broadcast-аудитории. P1, ~2 ч.**
- **`DIALOG_MUTED` / `DIALOG_UNMUTED`** — флаг beep-в-ночи. При muted
  → `notify=False` в исходящих. **P2, 1 ч.**
- **`DIALOG_CLEARED` / `DIALOG_REMOVED`** — житель очистил историю /
  удалил диалог. Метрика оттока. **P3 analytics.**
- **`RAW_API_RESPONSE`** — псевдо-событие, шлётся **на каждый ответ
  API** (`connection/base.py:182-197`). Уникальный hook для
  observability: latency, error-rate, парсинг `raw["code"]` в Sentry.
  **P1 для observability, ~4 ч.**

## 18. Buttons

Все типы (`enums/button_type.py`):
`CALLBACK, LINK, REQUEST_CONTACT, REQUEST_GEO_LOCATION, CLIPBOARD,
MESSAGE, OPEN_APP, CHAT (deprecated)`.

Используем: `CALLBACK, LINK`. **Не используем:**

- **`RequestGeoLocationButton(quick=True)`** — запрос геолокации без
  диалога подтверждения. **Идеально для «дыра в дороге» — кнопка
  «📍 Указать место», координаты приходят как `Location` attachment.
  Сейчас житель пишет адрес текстом, оператор гадает. P0-P1,
  ~1 день.** Материальное улучшение качества обращений.
- **`RequestContactButton`** — запрос телефона. Emergency-обращения
  «свяжитесь срочно». **P2.**
- **`ClipboardButton(payload=...)`** — копирует payload в буфер.
  «Скопировать №обращения» для вставки в CRM оператора. **P3.**
- **`OpenAppButton(web_app=URL)`** — открыть mini-app. Для будущего
  ЕМР-портала как mini-app. **P3.**

## 19. Приоритетная таблица (новое поверх существующих INSIGHTS+UNUSED)

| Приоритет | Что | Effort | Раздел |
|---|---|---|---|
| **P0** | RequestGeoLocationButton в обращениях | 1 день | §18 |
| **P0** | `message.forward(...)` вместо relay-копии | 2 ч | §7 |
| **P1** | Attachment-кэш по token (PDF-шаблон) | 4 ч | §1 |
| **P1** | `SKIP_STALE_UPDATES=true` для прод | 1 ч | §4 |
| **P1** | `BOT_STOPPED` handler + флаг users | 2 ч | §17 |
| **P1** | `RAW_API_RESPONSE` hook → observability | 4 ч | §17 |
| **P1** | 429 + Retry-After для broadcast | 1 день | §14 |
| **P2** | Удалить наш upload-retry wrapper | 1 ч | §1 |
| **P2** | `iter_all()` в broadcast-снимке | 2 ч | §3 |
| **P2** | `get_updates(types=...)` форк или PR | 2 ч | §4 |
| **P2** | `get_chat_member` sanity-check | 3 ч | §9 |
| **P2** | `get_me_from_chat` в `/diag` | 1 ч | §9 |
| **P2** | Парсить `raw["code"]` в Sentry | 2 ч | §16 |
| **P2** | `MESSAGE_REMOVED` audit-handler | 2 ч | §17 |
| **P2** | `DIALOG_MUTED` → `notify=False` | 1 ч | §17 |
| **P2** | `stat.views` в broadcast-аналитике | 2 ч | §13 |
| **P3** | `set_my_commands` когда swagger обновят | мониторить | §2 |
| **P3** | `OpenAppButton` под mini-app | future | §18 |
| **P3** | Edit-history persistence в БД | 1 ч | §12 |

## 20. Окончательно отсутствует в MAX

Inline mode, reactions, polls, live location, copy_message, batch
upload, presigned URLs, `max_connections`, `drop_pending_updates`,
`ip_address`, idempotency-key header, edit-history API,
mute_member, restrict_member, scheduled messages — нет ни в swagger,
ни в библиотеке. Делаем сами либо не делаем.
