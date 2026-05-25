# maxapi 1.1.0 — инсайты для AEMR-bot

> Источник: установленная библиотека `bot/.venv/Lib/site-packages/maxapi/`
> + README на PyPI (METADATA) + публичный сайт документации
> `https://love-apples.github.io/maxapi/examples/`. Дополняет ранее
> собранные `MAXAPI_INVENTORY.md` и `MAXAPI_UNUSED_FEATURES.md`.
>
> **Исходники проверены под рукой.** В установленной библиотеке файла
> `examples/` нет — пакет ставит только сам код (`__init__.py`,
> `bot.py`, `dispatcher.py`, `context/`, `types/`, `utils/`, `webhook/`,
> `filters/`, `methods/`, `enums/`, `client/`, `connection/`,
> `exceptions/`). Примеры живут на отдельном сайте документации, ссылка
> которого указана в METADATA. Все примеры ниже выписаны оттуда и
> сверены с реальными классами в установленной библиотеке.

## 1. Что подсмотрено в примерах

Сайт `love-apples.github.io/maxapi/examples/` содержит 15 примеров —
от echo-бота до webhook на FastAPI/Litestar. Все примеры используют
event-helpers (`event.message.answer`, `event.message.reply`,
`event.message.delete`, `event.answer(...)`, `event.ack()`), которые
мы сейчас обходим вручную через `event.bot.send_message(chat_id=...,
user_id=...)`.

### 1.1 Echo / quickstart-бот

```python
@dp.message_created(Command('start'))
async def hello(event: MessageCreated):
    await event.message.answer("Пример чат-бота для MAX 💙")
```

Источник: README (METADATA) + сайт. Наш паттерн в 16 файлах:
`event.bot.send_message(chat_id=cid, user_id=uid, text=...)` — 6
строк вместо 1. `event.message.answer(text)` это alias для send в
тот же peer (см. `maxapi/types/message.py:375-421`).

**Что выиграем.** Минус ~120 строк boilerplate, минус четыре места
(`utils/event.py:send`, `utils/event.py:send_to`, `services/admin_bus.py`,
прямые `bot.send_message`) дублирования логики «chat_id или user_id».

### 1.2 CallbackPayload — типизированный payload вместо ручного парсинга

```python
class MyPayload(CallbackPayload, prefix='mypayload'):
    foo: str
    action: str

@dp.message_callback(MyPayload.filter(F.foo == '123'))
async def on_first_callback(event: MessageCallback, payload: MyPayload):
    await event.answer(new_text=f'Первая кнопка: foo={payload.foo}')
```

Источник: сайт документации + `maxapi/filters/callback_payload.py`.
Pydantic-модель, `pack()` → строка длиной до 1024 байт,
`unpack()` обратно, и фильтр `Payload.filter(F.<field> == ...)`.

Наш паттерн: одиночный catch-all `appeal.on_callback`, который
руками парсит строки вида `appeal:123:reply` через `.split(":")` и
ветвится `if payload.startswith(...)`. Это:
- 6+ префиксов жёстко зашиты в строках (`appeal:`, `cb_menu:`,
  `op:menu:`, `broadcast_template_apply:`, `subscribe:` и т.д.);
- невозможно typecheck;
- легко спутать формат payload между местом сборки кнопки и местом
  парсинга (мы это поймали в SEC #3 — `🆔 №N` маркер spoof).

**Что выиграем.** Каждый сценарий — отдельная Pydantic-модель,
typecheck guard на формат, payload-фильтр атомарно подписан под
конкретный handler. Регрессии типа SEC #3 невозможны конструктивно.

### 1.3 Handler-level middleware

```python
class CheckChatTitleMiddleware(BaseMiddleware):
    async def __call__(self, handler, event_object, data):
        if event_object.chat.title == 'MAXApi':
            return await handler(event_object, data)

@dp.message_created(Command('start'), CheckChatTitleMiddleware())
async def start(event: MessageCreated):
    await event.message.answer('Chat title is MAXApi!')
```

Источник: сайт. Middleware можно передать вторым позиционным
аргументом в декоратор регистрации. Применяется ТОЛЬКО к этому
handler — гораздо точнее outer-middleware на уровне Dispatcher.

Наш паттерн: каждый сценарий, требующий гварда (например, «оператор
ли это», «обращение ещё открыто»), проверяет это руками в первых
строчках handler'а через `services/operators.py:is_operator(...)`
и явный `return`. Это дублируется 20+ раз.

**Что выиграем.** Декоратор `@dp.message_created(Command('reply'),
OperatorOnlyMiddleware())` снимает 3-5 строк boilerplate с каждого
operator handler'а. Декларативность важнее экономии.

### 1.4 Кастомный фильтр (BaseFilter)

```python
class FilterChat(BaseFilter):
    async def __call__(self, event: UpdateUnion):
        chat = await event.fetch_chat()
        if chat is None:
            return False
        return chat.title == 'Test'

@dp.message_created(CommandStart(), FilterChat())
async def custom_data(event: MessageCreated):
    await event.message.answer('Привет!')
```

Источник: сайт + `maxapi/filters/filter.py`. Filter может вернуть
`dict` — тогда ключи попадут в kwargs handler'а (мы видим это в
`PayloadFilter` и `Contact` filter'е — `return {"payload": payload}`
/ `return {"contact": att}`).

Наш паттерн: `utils/event.py:is_admin_chat(event)` —
помогающая функция, но не фильтр. Поэтому проверка пишется в каждом
handler руками: `if not is_admin_chat(event): return`. Можно сделать
`AdminGroupFilter(BaseFilter)` и подписывать его как обычный фильтр.

### 1.5 Router'ы и `dp.include_routers(...)`

```python
# main.py
router = Router(router_id='broadcasts')
dp.include_routers(router)

# router.py
@router.message_created(Command('router'))
async def hello(event: MessageCreated):
    await event.message.answer('Пришёл из router-ского handler')
```

Источник: сайт. `Router` наследуется от `Dispatcher`
(см. `maxapi/dispatcher.py:1502-1515`), несёт собственные
outer/inner middleware и filters, имеет свой `router_id` для логов.

Наш паттерн: единый `Dispatcher`, регистрация через
`register_handlers(dp)` в `handlers/__init__.py`. Этого хватает,
но `admin_settings.py:1000+ строк` и `appeal_funnel.py:726 строк`
— первые кандидаты на вынос в отдельные роутеры.

### 1.6 FSM через `StatesGroup` / `MemoryContext` (КЛЮЧЕВОЕ!)

```python
class Form(StatesGroup):
    name = State()
    age = State()

@dp.message_created(F.callback.payload == 'btn_1')
async def hello(event: MessageCallback, context: MemoryContext):
    await context.set_state(Form.name)
    await event.message.answer('Отправьте своё имя:')

@dp.message_created(F.message.body.text, Form.name)
async def hello(event: MessageCreated, context: MemoryContext):
    await context.update_data(name=event.message.body.text)
    data = await context.get_data()
    await event.message.answer(f"Привет, {data['name'].title()}!")
```

Источник: сайт. Бесплатное состояние per `(chat_id, user_id)` пары:
- `context.set_state(Form.name)` ставит состояние,
- `context.get_data()` / `update_data(...)` хранят словарь,
- `context.clear()` сбрасывает,
- декоратор `Form.name` вторым аргументом — фильтр по этому состоянию.

**Заглядываем под капот.** Dispatcher хранит `OrderedDict` контекстов
с LRU-вытеснением при 10000 ключей, есть TTL (per-context, см.
`maxapi/context/ttl.py`). Storage можно подменить на `RedisContext`:
`Dispatcher(storage=RedisContext, redis_client=...)`. Доступны
`StatesGroup` с вложенными группами и фильтр `StateFilter("*")`
(любое состояние) / `StateFilter(None)` (отсутствие состояния).

Наш паттерн: целая инфраструктура `services/wizard_registry.py`
(312 строк) + `services/wizard_persist.py` (153 строки) +
БД-миграция `0011_wizard_state_persistence.py` + 4 module-level
dict'а (`_op_wizards`, `_broadcast_wizards`, `_reply_intent`,
`_recent_replies`) — это всё ПЕРЕИЗОБРЕЛИ MemoryContext руками.

### 1.7 ChatActionLoop — typing-индикатор как context-manager

В `maxapi/types/shortcuts.py:19-66` есть `ChatActionLoop`:

```python
async with event.message.typing(interval=4.0):
    pdf_bytes = await build_policy_pdf(...)
    await upload_and_send(...)
# при выходе из контекста — auto-stop, без таймера
```

Источник: исходники + сайт. Циклически шлёт `TYPING_ON` каждые 4s,
пока вы не вышли из контекста. Это РОВНО то, что просит kaizen #1
из `MAXAPI_UNUSED_FEATURES.md` — но мы видели только разовый
`send_action`, а тут — auto-периодический.

### 1.8 `InlineKeyboardBuilder.adjust(*sizes)` — динамическая paging-сетка

В `maxapi/utils/inline_keyboard.py:50-86`:

```python
kb = InlineKeyboardBuilder()
for appeal in appeals:
    kb.add(CallbackButton(text=str(appeal.id), payload=...))
kb.adjust(3, 3, 2)  # три ряда: 3-3-2
```

Источник: исходники. Полезно для нашей пагинации списка обращений
жителя (`keyboards.my_appeals_list_keyboard`) — сейчас мы вручную
вызываем `builder.row(...)` в цикле и считаем `len() % 3`.

## 2. Готовые фичи которые могли бы использовать

### 2.1 `Contact` filter из `maxapi.filters`

`maxapi/filters/contact.py:15-38` уже даёт фильтр на сообщения с
вложением `contact:` + injection самого `ContactAttachment` в
kwargs handler'а:

```python
@dp.message_created(Contact())
async def on_contact(event: MessageCreated, contact: ContactAttachment):
    phone = contact.vcf_info.phone  # уже распаршен!
    ...
```

`Contact.vcf_info` (см. `maxapi/utils/vcf.py:6-30`) — dataclass с
`full_name`, `phones`, `fields`. У нас своя реализация в
`services/users.py` (vCard парсинг руками). Можно выкинуть полностью.

### 2.2 `MessageBody.html_text` / `MessageBody.md_text` — готовый рендер

`maxapi/types/message.py:124-149` — обе property'и берут raw text +
markup и возвращают полностью отформатированную HTML / Markdown
строку с правильным escape'ом и UTF-16 offset'ами для emoji.

У нас вся работа с входящим текстом — через `body.text` (plain) +
ручная конкатенация. Это значит **forward сообщений жителя в
operator чат с сохранением жирного / курсива не работает**, мы
теряем форматирование. Это полезно для admin-карточки, где мы
показываем «суть обращения» — если житель писал жирным, оператор
сейчас этого не видит.

### 2.3 `build_message_link(mid)` — внешний URL сообщения

`maxapi/utils/message_link.py` + property `Message.url`. Полезно
для audit-лога «оператор X ответил на №N, см. ссылку».

### 2.4 `event.fetch_chat()` / `event.fetch_from_user()` — lazy-fetch

В `maxapi/types/fetchable.py` есть mixin для lazy-fetch объектов чата
и юзера. Используется в примере custom filter (раздел 1.4). У нас
есть аналог через прямые `bot.get_chat_by_id(...)`, но fetch_chat
кэширует на event'е, поэтому повторные вызовы из middleware → filter
→ handler не плодят запросы.

### 2.5 Webhook через FastAPI/Litestar с lifespan

```python
webhook = FastAPIMaxWebhook(dp=dp, bot=bot, secret=webhook_secret)
app = FastAPI(lifespan=webhook.lifespan)
webhook.setup(app, path='/webhook')
```

Источник: README + сайт. У нас сейчас в `main.py` сложный самодельный
FastAPI route, который импортирует `process_update_webhook` из
приватного `maxapi.methods.types.getted_updates` (см. предупреждение
в `MAXAPI_INVENTORY.md:191-199`). Если когда-нибудь оживим webhook —
заменить на `FastAPIMaxWebhook` и `lifespan` без приватных импортов.

### 2.6 Параллельная обработка событий через `use_create_task=True`

`Dispatcher(use_create_task=True)` — каждое событие шлётся в
`asyncio.create_task(...)`. У нас сейчас sequential обработка, что
ОК для МО-масштабов (1 муниципалитет = пиковая нагрузка 5-10 r/s).
Но при росте до уровня области или federated-инсталляции — это
сразу полезно. `stop_polling` корректно ждёт background tasks
(см. `dispatcher.py:1404-1424`). **Не включать без нагрузочного теста**
— concurrency могут поломать наши module-level wizard dict'ы
(см. п. 3.2).

## 3. Что не стоит использовать

### 3.1 Прямые импорты из `maxapi.methods.*` и `maxapi.context.*` глубоко

Публичный API — это `from maxapi import Bot, Dispatcher, Router, F`
и `from maxapi.types import ...` / `from maxapi.utils import ...`.
Импорты вида `from maxapi.methods.types.getted_updates import
process_update_webhook` (наш `main.py:138`) — приватные пути, могут
исчезнуть в 2.x.

### 3.2 `use_create_task=True` без рефакторинга wizard'ов (см. 2.6)

Наши `wizard_registry._op_wizards: dict[int, ...]` — module-level,
без блокировки. При concurrent обработке двух событий от одного
оператора (rapid double-tap) — race condition. Прежде чем включать
параллелизм — мигрировать wizard на `MemoryContext` (за это
автоматически отвечает `asyncio.Lock` per-context, см.
`maxapi/context/context.py:26`).

### 3.3 Старые формы middleware

`dp.middleware(mw)`, `dp.outer_middleware(mw)`, `dp.middlewares = [...]`
все три помечены `DeprecationWarning` и будут удалены. Использовать
только `register_outer_middleware` / `register_inner_middleware`.
Мы уже на новой форме (см. `handlers/__init__.py:62-75`).

### 3.4 `Bot(parse_mode=...)` устарел — только `format=...`

В конструкторе бота `parse_mode=ParseMode.HTML` выдаст
`DeprecationWarning`. Мы это не используем (передаём `format=fmt`
точечно), но если рефакторинг добавит — следить.

### 3.5 `dp.init_serve(...)` deprecated — `dp.handle_webhook(...)`

Уже исправлено в `main.py:365` (см. `MAXAPI_INVENTORY.md:173`).
В новых PR не возвращать.

## 4. Рекомендуемые изменения по приоритету

### P0 (high-impact, low-risk; делать в течение месяца)

- **P0.1** — `event.message.answer(...)` / `event.message.reply(...)`
  / `event.ack()` вместо `event.bot.send_message(chat_id=..., user_id=...)`
  в 16 handler-файлах. Effort: ~3 часа на полную миграцию + тесты.
  Сейчас идиома смешана (см. grep — `admin_commands.py` уже на
  `event.message.answer`, остальные — нет).
- **P0.2** — typing-индикатор как `async with event.message.typing():
  ...` обернуть три долгих пути: `/broadcast` snapshot аудитории,
  `handlers/menu.py:my_appeals` listing, `services/uploads.py:upload_path`
  для тяжёлых PDF/XLSX. Effort: 30 минут, одно изменение per место.

### P1 (architectural; делать в течение квартала)

- **P1.1** — мигрировать wizard'ы (`wizard_registry.py` + 4 dict'а +
  `wizard_persist.py`) на `Dispatcher(storage=...)` + `StatesGroup`
  + декларативные фильтры по состоянию (`Form.awaiting_name`). Это
  снимет 500+ строк нашего кода и решит race для `use_create_task`.
  Storage можно оставить наш кастомный (subclass `BaseContext`,
  пишет в SQLite вместо in-memory) — тогда переживёт рестарт без
  отдельной миграции 0011. Effort: 2-3 дня + регрессия всей анкеты
  через E2E.
- **P1.2** — `CallbackPayload` для всех 6+ префиксов callback'ов.
  Особенно для `appeal:` — текущая конструкция самый sec-чувствительный
  путь (SEC #3 был именно про подделку payload). Effort: ~1 день,
  16+ мест сборки кнопок + 1 catch-all парсер.

### P2 (nice-to-have)

- **P2.1** — `Contact` filter из maxapi вместо нашего парсинга в
  `services/users.py`. Effort: ~2 часа, удаляет 100+ строк vCard.
- **P2.2** — `InlineKeyboardBuilder.adjust(3, 3, 2)` для my_appeals
  listing вместо ручного `% 3`. Effort: 20 минут.
- **P2.3** — выделить отдельные `Router`'ы для broadcast и
  admin_settings (по 1000+ строк), `dp.include_routers(...)`. Effort:
  полдня, чисто механика, тесты не должны сломаться.
- **P2.4** — `MessageBody.html_text` для admin-карточки «суть» — пусть
  оператор видит, если житель писал жирным или курсивом. Effort: ~1
  час, точечная правка в `card_format.py`.

### P3 (на потом / документировать как «знаем»)

- **P3.1** — `event.fetch_chat()` lazy-кэш — пригодится при добавлении
  per-chat настроек (например, мульти-МО-инсталляция).
- **P3.2** — `RedisContext` — если выкатим в продакшен с горизонтальным
  scaling.
- **P3.3** — `FastAPIMaxWebhook` + `lifespan` — для возвращения webhook
  без приватных импортов.

## 5. Что отсутствует в maxapi и нам нужно держать своё

Спецификация не имеет:

- **Throttling / rate-limit декораторов.** Документация явно ничего не
  упоминает. Наш `services/rate_limit.py` (если есть; иначе in-handler
  guard'ы) остаётся.
- **Pagination helpers.** `InlineKeyboardBuilder.adjust()` помогает
  только с разбивкой на ряды; саму пагинацию (next/prev, cursor)
  делаем сами.
- **Media-group отправка.** `bot.send_message(..., attachments=[...])`
  принимает список вложений — это и есть «media-group», отдельного
  helper'а нет. Но мы вообще не шлём albums (только одиночные файлы),
  так что не критично.
- **Reaction API.** В swagger MAX этого нет, в maxapi тоже.
- **Inline mode / inline query** (как в TG). Платформа MAX не
  поддерживает inline mode принципиально.
- **Conversation timeout watcher.** TTL у `MemoryContext` есть
  (`Dispatcher(storage=MemoryContext, ttl=300)`), но не вызывает
  callback при истечении — только сам сбрасывает. Если нужно
  «через 10 минут после последнего шага напомнить» — наш cron-loop
  остаётся.

## 6. Краткий gap-аналис: «как могло бы быть»

```python
# Сейчас (упрощённо, реальный код длиннее):
@dp.message_created()
async def on_message(event):
    chat_id, user_id = get_ids(event)
    if not is_admin_chat(event):
        # voronka жителя
        wizard = wizard_registry.get_op_wizard(user_id)
        if wizard and wizard.get("step") == "awaiting_name":
            ...
            await event.bot.send_message(chat_id=chat_id, user_id=user_id, text=...)

# Могло бы быть:
class Appeal(StatesGroup):
    awaiting_name = State()

@router.message_created(F.message.body.text, Appeal.awaiting_name)
async def on_name(event: MessageCreated, context: MemoryContext):
    await context.update_data(name=event.message.body.text)
    await context.set_state(Appeal.awaiting_locality)
    await event.message.answer("Укажите населённый пункт")
```

Разница не косметическая: второй вариант **не имеет catch-all диспатчера
вручную**, потому что фильтрация по состоянию делается на уровне
maxapi. Каждое сообщение приходит сразу в правильный handler — нет
ветвящегося if-cascade в `appeal.on_message`. Это и есть paradigm shift,
ради которого стоит идти на P1.1.
