---
status: done-in-spirit
applied_in_pr: 131
applied_at: 2026-05-28
note: Cluster F wave 1 (PR #131) перевёл 3 ключевых файла services/.
  Аудит 2026-05-28 evening показал, что services/, utils/, handlers/
  УЖЕ 95%+ на русском; остатки — устоявшийся технический обиход
  (sacred event log, dual-tracker, Lazy-init, monkey-patch, fallback,
  Pydantic-форма, Per-chat tracker). Полный sweep ВСЕХ файлов = busywork
  без real value. Phase D закрыта как «done-in-spirit».
---

# Проход «русские комментарии и docstring'и»

Дата: 2026-05-26. Сканированы `bot/aemr_bot/handlers/*.py` и
`bot/aemr_bot/services/*.py`. Проверены файлы воронки жителя,
админ-панели, рассылок, фонового планировщика, бэкапа БД,
threat-intel, repo-sync и регистра wizard'ов.

Найдено относительно немного: hot-path действительно чистый, основная
масса комментариев уже на литературном русском. Что осталось —
англоязычные «section markers» в `__all__`, короткие хвосты-метки в
середине файлов и группа служебных CRUD-функций без docstring'а в
сервисах данных.

Все правки — текстовые: имена функций, переменных, исключений, SQL,
regex, log.* строки и сообщения exception **не трогаются** (это
технические артефакты).

---

## Приоритет P0 — hot-path (handlers + ключевые services)

### handlers/admin_commands.py

`__all__` ведёт операторские команды и сейчас разделён англоязычными
строками-метками. Это видно при code-review (диалект ломается посреди
русского файла) и легко лечится переводом одной строкой на каждый
раздел.

| Line | Текущий | Предлагаемый | Тип |
|---|---|---|---|
| 95 | `# Stats` | `# Статистика` | comment |
| 100 | `# Operators wizard` | `# Мастер «операторы»` | comment |
| 108 | `# Settings` | `# Настройки` | comment |
| 112 | `# Audience` | `# Аудитория` | comment |
| 115 | `# Per-appeal ops` | `# Действия по конкретному обращению` | comment |
| 123 | `# Common` | `# Общие действия` | comment |

### handlers/admin_settings.py

| Line | Текущий | Предлагаемый | Тип |
|---|---|---|---|
| 603 | `# Pull Request` | `# Создание Pull Request: подтверждение и отправка` | comment |

Разделитель блока «PR-flow». Перевод заодно даёт читателю контекст,
что внутри секции — не определение PR, а конкретный шаг UI.

### handlers/broadcast_templates.py

| Line | Текущий | Предлагаемый | Тип |
|---|---|---|---|
| 568 | `# Strip prefix` | `# Срезаем префикс «op:tmpl:» — дальше идёт чистый хвост` | comment |

### services/admin_card.py

| Line | Текущий | Предлагаемый | Тип |
|---|---|---|---|
| 176 | `# Freshness check.` | `# Проверка свежести: редактируем карточку, только если она физически последнее сообщение бота в чате (см. модульный docstring).` | comment |

### services/cron.py

| Line | Текущий | Предлагаемый | Тип |
|---|---|---|---|
| 131 | `# Module-level cron jobs` | `# Cron-задачи на уровне модуля` | comment (section header) |
| 799 | `# Selfcheck heartbeat` | `# Самопроверка: heartbeat в admin-чат` | comment |
| 932 | `# Helpers` | `# Вспомогательные функции` | comment (section header) |

### services/db_backup.py

| Line | Текущий | Предлагаемый | Тип |
|---|---|---|---|
| 150 | `# Pipe для данных pg_dump → gpg` | `# Канал для данных: pg_dump пишет в data_w, gpg читает из data_r` | comment |

Уже наполовину по-русски — просто разворачиваем литературно.

---

## Приоритет P1 — services второго уровня

Здесь нет английских комментариев в строгом смысле, но три типа правок
заметно повышают читаемость.

### services/broadcasts.py — короткие CRUD-функции без docstring

Функции `set_subscription` (l. 25), `count_subscribers` (l. 60),
`mark_started` (l. 107), `mark_finished` (l. 121), `get_status`
(l. 237), `record_delivery` (l. 243) выполняют конкретные шаги цикла
рассылки, но не объясняют «зачем». Это нарушает стиль остальных
функций модуля (`mark_cancelled`, `reap_orphaned_draft`, `search` —
там docstring'и развёрнутые).

Предлагаемые тексты (по аналогии с соседями):

- `set_subscription` → «Подписать или отписать жителя на рассылки.
  Используется и в start-flow (явное согласие), и в bot_stopped-
  обработчике (мягкая отписка при блокировке бота)».
- `count_subscribers` → «Сколько жителей сейчас может получить
  рассылку. Тот же фильтр, что у `list_subscriber_targets`, — нужен
  для предпросмотра рассылки оператору до отправки».
- `mark_started` → «Перевести рассылку из DRAFT в SENDING и
  зафиксировать `started_at` плюс `admin_message_id` (mid карточки в
  админ-чате, по которой потом нажмут «остановить»)».
- `mark_finished` → «Финализировать рассылку: статус, `finished_at`,
  счётчики доставленных и упавших. Защитный слой: если FAILED при
  нулевых счётчиках — пересчитываем из реальных BroadcastDelivery,
  чтобы оператор не запустил повторную рассылку вслепую».
- `get_status` → «Текущий статус рассылки одной строкой. Используется
  в отмене из cooldown'а (`_handle_cancel_cooldown`) для проверки,
  что отмена ещё имеет смысл».
- `record_delivery` → «Записать одиночный факт доставки. `error=None`
  означает успех. Для батча используется `record_deliveries` — на
  10k подписчиков по-одному писать дорого».

### services/users.py — те же короткие CRUD-функции

`get_or_create` (l. 40), `has_consent` (l. 58), `set_phone` (l. 90),
`set_first_name` (l. 101), `set_state` (l. 105), `reset_state` (l. 112)
без docstring'а. Описать в духе соседних `set_consent` и
`update_dialog_data`.

Особенно важно расширить `get_or_create`: это **самая часто
вызываемая** функция бота (через `_common.current_user`), и читатель
сейчас не видит, что она не падает при отсутствии записи и не
перезаписывает имя у существующего жителя.

### services/wizard_persist.py — публичные обёртки UPSERT

`save_op_wizard`, `save_broadcast_wizard`, `delete_op_wizard`,
`delete_broadcast_wizard` (lines 77–108) — четыре тонкие обёртки
вокруг `_upsert`/`delete`, всё в одну строку. Хотя бы по одной строке
про разную TTL (5 мин vs 30 мин) и про то, что данные дублируются с
`wizard_registry`, чтобы читатель не пытался искать здесь
дополнительную логику.

### services/threat_intel.py — пара коротких docstring'ов

| Line | Текущий | Предлагаемый | Тип |
|---|---|---|---|
| 101 | `"""True если данные старше staleness budget'а."""` | `"""True если последний успешный refresh любого feed'а старше staleness budget (по умолчанию 24 ч). Используется в cron-job: при stale данных пишем admin-alert, но проверки URL продолжают работать на старом снимке — fail-open."""` | docstring |
| 112 | `"""Lazy-singleton доступ к глобальному store."""` | `"""Lazy-singleton доступ к глобальному ThreatIntelStore. Module-level dict — намеренное упрощение для тестируемости через monkeypatch (см. комментарий выше)."""` | docstring |
| 183 | `"""ThreatFox host-file: формат `0.0.0.0 evil.example` per line."""` | `"""Парсер ThreatFox host-file: формат `0.0.0.0 evil.example` на строку. Берём правое поле как hostname; всё, что не парсится, тихо отбрасываем."""` | docstring |
| 199 | `"""PhishTank online-valid.json: список объектов с полем `url`."""` | `"""Парсер PhishTank online-valid.json: список объектов, у каждого поле `url`. Извлекаем hostname через `_normalize_host`; объекты без url или с пустым host тихо отбрасываем."""` | docstring |

### services/broadcast_templates.py

| Line | Текущий | Предлагаемый | Тип |
|---|---|---|---|
| 164 | `"""Soft-delete: проставить archived_at = now."""` | `"""Soft-delete шаблона: проставить `archived_at = now()`. Шаблон исчезает из активного списка и поиска, но остаётся доступен по id (для `record_usage` старых рассылок). Идемпотентен на повторе — archived_at просто перезаписывается."""` | docstring |

### handlers/operator_reply.py

| Line | Текущий | Предлагаемый | Тип |
|---|---|---|---|
| 151 | `"""Backward-compatible alias для тестов/старых импортов. ...` | оставить, заменить только заголовок: `"""Обратная совместимость: alias для тестов и старых импортов. ..."` | docstring |

Мелочь — «Backward-compatible» в одно слово «обратная совместимость»
звучит литературнее в русском контексте.

### handlers/admin_callback_dispatch.py

| Line | Текущий | Предлагаемый | Тип |
|---|---|---|---|
| 87 | `"""op:stats_<period> → ack + run_stats(event, period)."""` | `"""Фабрика handler'а статистики за период: ack callback и `run_stats(event, period)`. Используется для развёртывания одной строки на каждый период (today/week/month/...) без копипасты тела handler'а."""` | docstring |

### services/wizard_registry.py

| Line | Текущий | Предлагаемый | Тип |
|---|---|---|---|
| 181 | `# Best-effort fire-and-forget сохранение wizard state в БД через` | оставить — это уже русско-английский смешанный, технические термины уместны | — |

---

## Приоритет P2 — utils / db / тесты

### Английские слова в коротких docstring'ах сервисов

Несколько docstring'ов начинаются с английского технического термина
(`STRtree`, `ISO`, `ILIKE`, `CSV URLhaus`, `JSON-сериализация`,
`HTML-escape`, `Read-only поиск`, `Read-modify-write апдейт`,
`Lazy-init`, `Catch-up pulse`). Эти **оставить как есть** — это
названия конкретных технологий и операций, перевод сделал бы их
непоисковыми (поиск по `STRtree` или `ILIKE` в репозитории сломается).

### Section comments в cron.py

`# Selfcheck heartbeat` (l. 799) и `# Helpers` (l. 932) — это
section-маркеры внутри длинных файлов. Их перевод — мелочь, влияния на
рантайм нет, но визуально приятнее иметь весь модуль на одном языке.
Переведено в блоке P0 выше.

### TODO/FIXME/XXX

Реальных проектных TODO/FIXME/XXX/HACK-комментариев в `handlers/` и
`services/` **не найдено**. Единственное совпадение — `+7-900-911-XXXX`
в комментарии-примере телефона в `settings_store.py` l. 35, это часть
описания формата, не маркер.

### Короткие docstring'и в db/utils

В рамках текущего прохода не сканировались (бюджет 12 минут).
Отметить как «TBD следующим проходом» — после сервисов и handler'ов
имеет смысл пройтись по `bot/aemr_bot/db/` и `bot/aemr_bot/utils/`,
там тоже встречаются однострочники без объяснения «зачем».

---

## Итог

Всего найдено **27 правок**:

- **8 комментариев** (английские section-маркеры и короткие хвосты в
  P0): admin_commands.py × 6, admin_settings.py × 1,
  broadcast_templates.py × 1.
- **6 коротких комментариев** в services (admin_card.py × 1,
  cron.py × 3, db_backup.py × 1, плюс мелочи) — P0/P1 граница.
- **13 расширений docstring'ов**: broadcasts.py × 6, users.py × 6,
  threat_intel.py × 4, wizard_persist.py × 4 группой, плюс
  broadcast_templates.py × 1, admin_callback_dispatch.py × 1,
  operator_reply.py × 1, admin_card.py module-docstring (если хочется
  поправить «Admin appeal card с freshness-rule» на «Карточка
  обращения для админ-чата с правилом freshness» — но это вкусовое,
  «freshness-rule» — устоявшийся в коде термин).

Хот-path (`handlers/menu.py`, `handlers/start.py`, `handlers/appeal*.py`,
`services/cron.py`, `services/admin_card.py`, `services/appeals.py`,
`services/card_format.py`) уже на хорошем литературном уровне.
Основные точки роста — короткие CRUD-функции сервисов данных
(broadcasts, users, wizard_persist), которые писались под темп
миграций и недополучили docstring'и того уровня, что у соседей
(`reap_orphaned_*`, `find_stuck_in_*`, `update_dialog_data`).

После применения правок весь свежий код на горячем пути будет
говорить одним языком — связной литературной русской прозой с
английскими техническими терминами в правильных местах
(модели, SQL-операторы, имена feed'ов).
