# CODE_DECISIONS_LOG

Архив архитектурных решений, исторически живших в module-docstring'ах
кода. Перенесены сюда в рамках B6 (MLP финальная сборка, 2026-05-27),
чтобы docstring'и в коде не превышали 5-7 строк («что и зачем»), а
полная мотивация и контекст оставались доступны для будущих
контрибутеров и аудиторов.

Из кода ссылка короткая: `# См. CODE_DECISIONS_LOG.md §N`. Сами
обсуждения остаются здесь — они нужны при следующем большом
рефакторинге, при разборе странного поведения, при онбординге нового
разработчика. В коде они шумят и затрудняют чтение.

---

## §1. `utils/menu_tracker` — dual-tracker (physical vs editable)

**Дата решения:** 2026-05-27 (PR #100, после семидневного выявления конфликта смыслов).

**Контекст.** Раньше один `menu_tracker[chat_id]` совмещал две
несовместимые роли:

1. «Какое сообщение физически последнее в чате» — нужно, чтобы
   freshness-rule не редактировал карточку, выше которой уже что-то
   появилось.
2. «Какую карточку разрешено редактировать» — нужно, чтобы клик
   кнопки менял экран меню, а не превращал historic event в меню.

Когда эти смыслы совпадали в одном поле, любое исходящее сообщение
бота двигало tracker — в том числе historic-уведомление с кнопкой
«🏠 В админ-меню». Следующий клик на это уведомление видел совпадение
mid → редактировал уведомление в меню. Sacred event log нарушался.

**Целевая модель.** Три независимых поля на чат:

- `last_physical_mid` — mid последнего физического сообщения в чате
  (любого: меню, события, карточки обращения, pulse, ack-уведомления).
  Двигается каждый раз, когда бот шлёт что-либо или приходит сообщение
  оператора/жителя.
- `last_editable_mid` — mid последней карточки, которую разрешено
  редактировать (меню, wizard-экран, listing). Не двигается на
  historic events (CITIZEN_REPLY, APPEAL_ACCEPTED, audit-уведомления,
  pulse, broadcast progress).
- `last_editable_kind` — категория последней редактируемой карточки
  (`menu`, `wizard`, `progress`, `listing`). Защита от «callback на
  menu редактирует wizard»: если kind не совпадает с тем, что caller
  собирается показать, разрешение не выдаётся.

**Контракт edit:** разрешён только когда все три условия выполнены
одновременно:

1. `callback_mid == last_physical_mid` — карточка физически последняя.
2. `callback_mid == last_editable_mid` — это была редактируемая карточка.
3. `kind == last_editable_kind` — caller показывает экран той же
   категории.

Иначе — send_new, и оба tracker'а сдвигаются на новый mid.

**Хранение:** in-memory dict с `ChatState`. Single-process бот, после
рестарта tracker пуст (graceful — первое нажатие даёт send_new).

**Обратная совместимость:** старый API `get_last_menu_mid` /
`set_last_menu_mid` оставлен как тонкая обёртка над новой структурой
(чтобы не ломать сотни usage-сайтов одним PR'ом). По смыслу:
- `get_last_menu_mid` теперь возвращает `last_editable_mid`.
- `set_last_menu_mid` устанавливает оба — физический И editable.

Совместимо с предыдущим поведением, потому что старый код вызывал
`set_last_menu_mid` только из freshness-aware send'ов
(`send_or_edit_screen` / `_send_or_edit_menu` / `admin_bus.send`), где
это эквивалентно «новое меню стало последним и физически, и как
карточка».

---

## §2. `services/admin_bus` — единая шина в admin chat

**Дата решения:** 2026-05-22 (SACRED #1), уточнено 2026-05-27 (PR #98).

**Контекст.** Раньше десятки путей шли в admin chat напрямую через
`bot.send_message(chat_id=cfg.admin_group_id, ...)` — pulse,
admin_events, broadcast progress, operator_reply confirmations,
retention notifications. Каждое такое сообщение физически сдвигает
чат вниз, но никто из этих путей не обновлял `menu_tracker`. Tracker
отставал от реального состояния чата, и freshness-rule
(`callback_mid == tracker → edit`) врал: оператор тапал кнопку на
старой карточке, бот edit'ал её на месте далеко вверху чата.

**Решение.** Любая отправка в admin chat теперь идёт через
`admin_bus.send` — шина выполняет три действия атомарно:

1. `bot.send_message(chat_id=cfg.admin_group_id, ...)`
2. `extract_message_id(sent)` — достаёт mid из ответа MAX API.
3. `menu_tracker.note_event(cfg.admin_group_id, mid)` — двигает
   `last_physical_mid` на свежий mid.

**Что НЕ делает шина:**

- Не интерпретирует attachments / семантику сообщения. Это тонкий
  wrapper, не бизнес-логика.
- Не делает retry / circuit-breaker. Это responsibility вызывающего
  (для broadcast есть `_send_with_retry`, для admin notifications —
  `_send_admin_text_with_retry` в `services/cron.py`).
- Не делает freshness-check на edit. Edit'ить через шину нельзя
  принципиально — карточки с кнопками идут через `admin_card.render`
  (freshness-aware), карточки меню — через `send_or_edit_screen`
  (тоже freshness-aware). Шина — для **новых** сообщений.

**Incoming admin-message hook.** Отдельная функция
`note_incoming_admin_message(mid)` — вызывается из handler'а на каждое
новое сообщение в admin chat (operator-text, voice, sticker). Она
сдвигает tracker на mid входящего сообщения. Это закрывает дыру
«оператор написал в чат, но tracker по-прежнему на карточке выше —
следующий тап freshness-mismatch не увидит».

---

## §3. `install_outgoing_tracker_hook` — monkey-patch `bot.send_message`

**Дата решения:** 2026-05-27 (PR #98).

**Жалоба владельца.** «Меню в админ-чате редактируется при тапе
кнопки на не-последнем сообщении». Корень — 62 прямых
`bot.send_message(chat_id=admin_group_id, ...)` в коде (handlers +
services), большинство из них не вызывают
`menu_tracker.set_last_menu_mid` после send. Tracker отстаёт от
физического состояния чата.

**Решение.** Hook оборачивает оригинальный `bot.send_message`. После
каждого успешного `send_message(chat_id=admin_group_id, ...)` извлекает
mid и двигает tracker. Раньше единственное место с правильным sync —
`admin_bus.send` — мигрировать все 62 sites через шину было бы 200+
строк правок в 14 файлах, с риском регрессий. Hook решает проблему
один раз на старте бота.

**Что делает hook:**

1. Если `chat_id != admin_group_id` — пробрасывает вызов без
   изменений (citizen-chat tracker имеет свой sync через
   `_send_or_edit_menu`).
2. Если `chat_id == admin_group_id` — выполняет оригинальный send,
   извлекает mid, обновляет tracker. Возвращает результат как был.
3. Ошибки `send_message` не глотает — пробрасывает caller'у. Tracker
   обновляет только при успешном send.

**Идемпотентность.** Повторный вызов на тот же bot — no-op (маркер
`_aemr_admin_outgoing_tracker_installed` на bot-объекте). Иначе hook
оборачивал бы себя рекурсивно и каждое сообщение проходило бы N
tracker.set.

**Где НЕ дублируется логика:**

- `admin_card.render` сам делает `note_event` после send_new (sacred
  event log). Hook сначала set'нет tracker на mid карточки — потом
  `render` повторит то же действие на тот же mid (идемпотентно).
- `admin_bus.send` сам делает `note_event` после send. Hook повторит
  то же действие — это идемпотентно (tracker сидит на том же mid). Не
  убираем явный set, оставляем для читаемости `admin_bus.send`.

---

## §4. `utils/typing_indicator` — best-effort UX-улучшение

**Дата решения:** 2026-05-23 (MAXAPI_INSIGHTS P0.2).

**Зачем.** maxapi 1.1.0 поддерживает `bot.send_action(chat_id,
SenderAction.TYPING_ON)` — мигающие точки «бот печатает». Жителю
/оператору сразу понятно: «нажатие услышали, идёт работа», особенно
когда дальше будет несколько сообщений (listing открытых обращений,
broadcast confirm с превью).

Без индикатора оператор тапает «📂 Открытые обращения» и ждёт 1-2
секунды без обратной связи — кажется, что бот завис. С индикатором
точки появляются мгновенно, listing подъезжает следом.

**Контракт `mark_typing(event_or_bot, chat_id)`:**

- На вход: либо event (берём `event.bot` и `chat_id` через `get_ids`),
  либо явный `(bot, chat_id)`.
- Best-effort: любой Exception от MAX API НЕ должен ломать handler.
  `send_action` — UX-улучшение, не критичная часть flow. Логируем на
  DEBUG и продолжаем.
- TYPING_ON автоматически гасится MAX'ом через ~5 секунд или при
  следующем сообщении бота — explicit OFF не нужен.

**Где применять (выявлено CARDS_UX_SWEEP 2026-05-26):**

1. `admin_panel._do_open_tickets` — listing открытых обращений требует
   query + transform → 1-2 сек.
2. `menu.do_my_appeals` — listing обращений жителя.
3. `broadcast._handle_confirm` — перед запуском send-loop рассылки.
4. Любые другие точки, где handler делает >500 мс работы перед
   первым send_message.

**Где НЕ применять:**

- Простые callbacks с одним send_message <100 мс — typing на 5 секунд
  выглядит дольше реакции, UX хуже.
- Cron / pulse — там нет «оператор ждёт реакцию».
- На карточке обращения (force_new) — она сама и есть фидбек.

---

## §5. `services/wizard_registry` — единое хранилище intent'ов оператора

**Дата решения:** 2026-05-24 (рефакторинг SLF001).

**Контекст.** До этого модуля каждый handler хранил собственный
module-level dict:

- `handlers/admin_commands.py:_op_wizards` — wizard «Добавить оператора»
- `handlers/broadcast.py:_wizards` — wizard рассылки
- `handlers/operator_reply.py:_reply_intent` — короткоживущий «оператор
  готовится ответить на обращение N»
- `handlers/operator_reply.py:_recent_replies` — дедуп уже отправленных
  ответов (кросс-процессная защита от двойного нажатия)

**Минусы старой схемы:**

- кросс-handler доступ через приватные имена (ruff SLF001 — 12+ мест:
  `broadcast_handler._wizards.pop(...)` в `appeal.py` etc).
- нет единой точки сброса при `/cancel` оператора — приходилось
  вручную дёргать каждое из 4 хранилищ.
- состояние раскидано — при тестах не понятно, что нужно мокать.

**Решение.** Единая точка с публичным API:

```python
from aemr_bot.services import wizard_registry as wr

wr.set_op_wizard(operator_id, {"step": "awaiting_id"})
state = wr.get_op_wizard(operator_id)
wr.clear_all_for(operator_id)  # сброс всех визардов оператора
```

Для тестов есть `wr.reset_all()` — обнуляет все хранилища.

Не вносит логику — только хранение. Бизнес-логика остаётся в handlers.
Это снизило риск регрессии при выделении: данные не меняются, меняется
только способ доступа.

---

## §6. `services/admin_card` — sacred event log + freshness-rule

**Дата решения:** 2026-05-22 (DDD pivot), уточнено 2026-05-27 (PR #100).

**Контракт.** Admin appeal card следует sacred-event-log правилу:
карточка обращения — это событие в журнале чата, не меню. Едит её
нельзя в принципе после dual-tracker (PR #100). Каждое изменение
(reply от оператора, followup от жителя, статус-смена) → новая
карточка с обновлённым содержимым, старая остаётся как след.

**Что НЕ карточка** (события без кнопок, иммутабельные): ответ
оператора жителю, followup-уведомления, подписки/отписки/erase ack.
Идут через прямой `bot.send_message` без trackers.

**Контракт `Appeal`-полей:**

- `admin_message_id` — mid ПЕРВОЙ публикации карточки (finalize).
  Используется как reply-link при relay вложений жителя. Не меняется
  после finalize.
- `last_admin_card_mid` — mid последней опубликованной карточки этого
  обращения. Обновляется при send_new и при edit-с-new-mid (edit fail
  fallback).
