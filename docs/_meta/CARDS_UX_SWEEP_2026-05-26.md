---
status: applied
applied_in_pr: 104, 105, 106, 107, 108
applied_at: 2026-05-27
superseded_by: docs/_meta/UI_BRAND_CONCEPT_2026-05-26.md (канон-спецификация),
  bot/aemr_bot/ui/citizen_keyboards.py, bot/aemr_bot/ui/operator_keyboards.py,
  bot/aemr_bot/services/card_format.py
note: A1 timeline unification (#104), A2 5/5 stale (#105), A4.1 citizen funnel
  (#106), A4.2 citizen «Мои обращения» (#108), A4.3 admin menu (#107). A4.4
  admin wizards audit 2026-05-28 — done-in-spirit (UI_BRAND_CONCEPT уже
  соблюдён, drift не выявлен).
---

# Cards UX Sweep — 2026-05-26

Полный backlog для распространения единой визуальной грамматики
(шапка `━━━ HEADER ━━━`, статус-tag сразу под шапкой, эмодзи только
в начале строк, разделители `· · ·` тонкий / `━━━━━━━━━━━━━━━━`
жирный, CTA в конце) на все карточки кроме 4 уже сделанных в PR #89.

Грамматика — см. `texts.py:102-114` (комментарий-каноник). Принцип
accessibility: `─` иногда озвучивается NVDA/JAWS-RU как «черта»,
а `· · ·` — как пауза, потому везде, где смысловая граница, а не
тяжёлый раздел, заменяем `──────` → `· · ·`. Жирный `━━━━` оставляем
только перед CTA и в шапке.

---

## P0 — видят оба, каждый день

### 1. `OP_BROADCAST_PREVIEW` (texts.py:520-527)

Сейчас:
```
Предпросмотр рассылки
────────────────
{text}
────────────────
📷 Картинок: {image_count}{image_warning}
Готово к отправке. Получателей: {count}.
```

Улучшить:
```
━━━ ПРЕДПРОСМОТР РАССЫЛКИ ━━━
👥 Получателей: {count}
🖼 Картинок: {image_count}{image_warning}

· · ·

{text}

━━━━━━━━━━━━━━━━
📤 Готово к отправке. Проверьте текст и картинки.
```

Шапка явная, мета (получателей/картинок) сразу под шапкой как status-tag,
тело отделено `· · ·`, жирный разделитель перед CTA. Это та же грамматика
что у admin_card / citizen_reply.

Тесты: `test_broadcast_handlers.py` (substring assertions на «Готово к
отправке» и «Получателей» — если есть; см. `_handle_wizard_text`).
Проверить grep `"Предпросмотр рассылки"` в tests/.

### 2. `OP_BROADCAST_CARD` (texts.py:563-571)

Сейчас:
```
📜 Рассылка #{number}
Статус: {status}
Создана: {created_at}
Доставлено: {delivered}/{total}{failed_line}
Картинок: {image_count}
──────────
{text}
```

Улучшить:
```
━━━ РАССЫЛКА #{number} ━━━
📊 Статус: {status}
🗓 Создана: {created_at}
✅ Доставлено: {delivered}/{total}{failed_line}
🖼 Картинок: {image_count}

· · ·

{text}
```

Эмодзи в начале каждой метаданной (NVDA-friendly), шапка единая,
тонкий разделитель перед телом. `failed_line` уже идёт с `\n` — оставить.

Тесты: `test_broadcast_history_card.py` — проверить assertions на
«📜 Рассылка #» и «──────────».

### 3. `STATUS_LABELS` (texts.py:405-410) — уже хорошие, оставить

```
"new": ("🆕", "Новое"),
"in_progress": ("🔄", "В работе"),
"answered": ("✅", "Завершено"),
"closed": ("⛔", "Закрыто без ответа"),
```

Маркер всегда первым в кортеже — это уже соответствует «эмодзи в начале».
Оставить как есть.

### 4. `APPEAL_LIST_EMPTY` (texts.py:83-86) — оставить

Не карточка, а одностроковый prompt. Эмодзи в скобках естественные,
«Написать обращение» — это название кнопки. Не трогать.

---

## P1 — оператор видит часто; либо житель часто

### 5. `admin_followups_block` в card_format.py:63-88

Сейчас:
```python
lines = ["────────────────", title]   # "Дополнение к обращению:"
```

Улучшить — заменить `────────────────` на `· · ·` (это вспомогательный
блок внутри admin_card, не главный CTA-раздел):

```python
lines = ["", "· · ·", "", title]
```

Эмодзи `📩` префиксом к title (`📩 Дополнение к обращению:`) для
скринридер-симметрии с timeline-маркерами.

Тесты: `test_appeal_card_timeline.py` — там грепаются эти заголовки
substring'ами.

### 6. `_render_timeline` в card_format.py:126

Сейчас:
```python
lines = ["────────────────", "История переписки:"]
```

Улучшить:
```python
lines = ["", "· · ·", "", "📜 История переписки:"]
```

Тот же приём — `· · ·` вместо `────`, эмодзи `📜` префиксом к
section-title (NVDA озвучит как «свиток история переписки», ясный
семантический сигнал).

Тесты: те же `test_appeal_card_timeline.py`, `test_admin_card_render.py`.

### 7. `OP_BROADCAST_LIST_HEADER` (texts.py:559) и `OP_BROADCAST_LIST_ITEM` (texts.py:560)

Сейчас:
```
📜 Недавние рассылки:
#{number} · {created_at} · {status} · {delivered}/{total}
```

Item уже единообразный (всё в одну строку через `·`). Оставить как есть,
лишь поправить header — добавить grammar-line:

```
━━━ ИСТОРИЯ РАССЫЛОК ━━━
📜 Последние 10 (тапните на строку — откроется карточка):
```

Тесты: `test_broadcast_handlers.py` или `test_broadcast_history_card.py`.

### 8. `OP_TMPL_LIST_EMPTY` / `OP_TMPL_LIST_HEADER` / `OP_TMPL_CARD` (texts.py:601-632)

Уже используют `━━━━━━━━━━━━━━━━━━` обильно и эмодзи в начале строк.
Структура хорошая. **Оставить** все три — это уже более выразительная
грамматика, чем сейчас у admin_card. Менять не нужно, только проверить
один момент:

В `OP_TMPL_CARD` (texts.py:621-630) сейчас после второго ━ идёт текст
шаблона **без** `· · ·` отделителя:
```
📊 Применений: {use_count}{last_used_line}
━━━━━━━━━━━━━━━━━━
{text}
```

Это OK — `━━━━` уже жирный разделитель «мета / контент». Но для
симметрии с admin_card (где тело отделено пустой строкой) — добавить
пустую строку до и после:

```
📊 Применений: {use_count}{last_used_line}

━━━━━━━━━━━━━━━━━━

{text}
```

Тесты: `test_broadcast_templates_handlers.py`.

### 9. `OP_TMPL_NEW_NAME_PROMPT` и `OP_TMPL_NEW_TEXT_PROMPT` (texts.py:635-683)

Уже структурированы (шапка, шаги, советы). Хорошие. Оставить.

### 10. Карточка оператора в `admin_operators.py:309-316`

Сейчас собирается inline:
```python
lines = [
    f"👤 {op.full_name}",
    "──────────",
    f"ID:       {op.max_user_id}",
    f"Роль:     {op.role}",
    f"Статус:   {status_line}",
    f"Добавлен: {op.created_at.strftime('%d.%m.%Y')}" if op.created_at else "",
]
```

Улучшить:
```python
lines = [
    f"━━━ ОПЕРАТОР #{op.max_user_id} ━━━",
    f"👤 {op.full_name}",
    f"🏷 Роль: {op.role}",
    f"📊 Статус: {status_line}",
]
if op.created_at:
    lines.append(f"🗓 Добавлен: {op.created_at.strftime('%d.%m.%Y')}")
if extra:
    lines.append("")
    lines.append("· · ·")
    lines.append("")
    lines.extend(extra)
```

`status_line` уже содержит эмодзи (✅/💤) — оставить.
Wide-spacing (`ID:       ` с табом) — убрать, грамматика теперь
«эмодзи · значение», не псевдо-таблица.

Тесты: `test_admin_operators.py` — substring assertions на «ID:» /
«Роль:». При смене формата починить.

### 11. `OP_BROADCAST_STARTED` / `OP_BROADCAST_PROGRESS` / `OP_BROADCAST_DONE` / `OP_BROADCAST_CANCELLED` (texts.py:543-557)

Сейчас:
```
Рассылка #{number} запущена.
Доставлено: 0/{total}
```

Прогресс-карточка видна оператору каждые 5 секунд — должна быть
быстро читаемой, не пышной. **Оставить минимальным**, но добавить
эмодзи-маркер статуса:

```
📤 Рассылка #{number} запущена
✅ Доставлено: 0/{total}
```

```
📤 Рассылка #{number}
✅ Доставлено: {delivered}/{total}{failed_suffix}
```

```
━━━ РАССЫЛКА #{number} ЗАВЕРШЕНА ━━━
✅ Доставлено: {delivered} из {total}.{failed_line}
```

Для finalize-сообщения (`DONE`) — полная шапка-граница (это event-log
карточка, не throw-away progress). Для прогресс-tick'ов — компактно.

Тесты: `test_broadcast_handlers.py` — substring assertions на
«Рассылка #» / «Доставлено:».

### 12. `CONSENT_REVOKE_CONFIRM` / `ERASE_CONFIRM` (texts.py:238-270)

Сейчас bullet-список «Что произойдёт». Хорошая структура, дополнить
шапкой и убрать «голый текст»-заголовок:

`CONSENT_REVOKE_CONFIRM`:
```
━━━ ЗАВЕРШИТЬ ОБЩЕНИЕ С БОТОМ? ━━━
⚠️ Что произойдёт:

• Согласие на обработку ваших данных будет отозвано.
• По открытым обращениям оператор даст финальный ответ через бот.
• Подписка на рассылку отключится.
• Данные будут стёрты или обезличены через 30 дней без активности.

· · ·

Это не лишает вас права обратиться в Администрацию через официальные
каналы. Передумаете — откройте /start и снова дайте согласие до
автоудаления.
```

`ERASE_CONFIRM` — аналогично, шапка `━━━ УДАЛИТЬ ВАС ИЗ БОТА? ━━━`.

Тесты: `test_handlers_menu.py` / `test_handlers_menu_extra.py` —
substring assertions на «Завершить общение» / «Удалить вас из бота»
по началу `CANCELLED`/прощальной воронки.

---

## P2 — IT-only, редко смотрит

### 13. `admin_settings.py` — все settings-карточки

Используют `──────────` (10 chars) повсеместно. IT-only, low-traffic.
**Перевести оптом на грамматику**, но low-priority:

`run_settings_menu` (admin_settings.py:136-143):
```
━━━ НАСТРОЙКИ БОТА ━━━

Выберите категорию для редактирования.
Каждое изменение применяется к боту сразу.
Чтобы зафиксировать изменения в репозитории,
создайте PR в нижней части меню.{extra}
```

`_show_text_card` (admin_settings.py:323-329):
```
━━━ {title} ━━━
🏷 Тип: {type_label}

· · ·

📝 Текущее значение:
{rendered_value}{constraints}
```

`_show_list_card`, `_show_obj_card`, `_show_author_card`, PR-confirm/done
— по тому же шаблону: шапка `━━━ TITLE ━━━`, мета через эмодзи,
`· · ·` отделяет body. Жирный `━━━━━━━━━━━━━━━━` появляется только
перед CTA / при finalize (`✅ PR создан`).

Тесты: `test_admin_settings_audit.py` — substring assertions на
«⚙️ Настройки бота» / «Все ключи». Менять синхронно.

### 14. Operator wizard cards в `admin_operators.py:139-149` и далее

run_operators_menu, _show_from_group, _start_add_with_picked,
_apply_role_choice, _show_add_confirm, _confirm_save — все используют
заголовок-эмодзи + `──────────`.

Те же три замены: `──────────` → шапка `━━━ ... ━━━` + `· · ·` для
вспомогательных разделителей. CTA-разделитель `━━━━━━━━━━━━━━━━` не
нужен (там не CTA, а wizard-prompt'ы — конечной кнопочной CTA нет,
есть keyboard).

Тесты: `test_admin_operators.py`.

### 15. Карточки в menu.py

`open_main_menu` (menu.py:108-117) — для заблокированного жителя:
сейчас inline текст «Ваш аккаунт заблокирован — подача обращений…».
**Вынести в texts.py** константой `BLOCKED_MENU_TEXT` и применить
грамматику:
```
━━━ АККАУНТ ЗАБЛОКИРОВАН ━━━
🚫 Подача обращений и подписка недоступны.

· · ·

Доступные разделы — ниже. Если блокировка ошибочна, обратитесь
к координатору Администрации.
```

`open_my_appeals` (menu.py:155-159) — header «Ваши обращения (стр. N/M,
всего T):» можно оставить однострочным, либо обернуть:
```
━━━ ВАШИ ОБРАЩЕНИЯ ━━━
📂 Всего: {total}{paging_line}
```

`start_appeal_followup` (menu.py:213-216) — inline текст «Опишите
дополнение к обращению #N…». Вынести в `texts.py:APPEAL_FOLLOWUP_PROMPT`:
```
━━━ ДОПОЛНЕНИЕ К ОБРАЩЕНИЮ #{appeal_id} ━━━
📝 Опишите дополнение одним сообщением.

· · ·

📎 Можно приложить фото, видео или файл.
```

Тесты: `test_handlers_menu.py` — substring assertions, ожидаемые
изменения.

### 16. `start_appeal_repeat` (menu.py:274-281)

Сейчас:
```
Подаём новое обращение {context}:
📍 {locality}, {address}
🏷 {topic or '—'}

Опишите суть одним сообщением. Можно приложить фото, видео или файл.
```

Уже близко к грамматике. Лёгкая правка — шапка:
```
━━━ НОВОЕ ОБРАЩЕНИЕ {context} ━━━
📍 {locality}, {address}
🏷 {topic or '—'}

· · ·

📝 Опишите суть одним сообщением.
📎 Можно приложить фото, видео или файл.
```

### 17. `open_emergency` (menu.py:807-814) и `open_dispatchers` (menu.py:828-831)

Уже содержат `☎️` / `📞` эмодзи в начале логических секций. Структура
«секция — список с bullet'ами» работает. **Не менять** — это
data-driven листы, а не статус-карточки.

---

## P3 — лучше не трогать (внутреннее или CTA-only)

### 18. `CONTACT_REQUEST` / `LOCALITY_REQUEST` / `CONTACT_RECEIVED` / `NAME_EMPTY` / `ADDRESS_EMPTY` / `APPEAL_EMPTY_REJECTED` / `CONTACT_RETRY` (texts.py:37-100)

Микро-prompt'ы в воронке, по 1-3 строки. Уже хорошие. Оставить.

### 19. `GEO_DETECTED_FULL` / `GEO_DETECTED_LOCALITY_ONLY` / `GEO_OUTSIDE_EMO` (texts.py:55-74)

Confirmation-карточки геолокации. Эмодзи `📍` и `✓` уже в начале строк
и работают. **Оставить**.

### 20. `WELCOME` (texts.py:1-17)

Старт-карточка. Структура шапка-блоки-CTA уже есть («———» как разделитель).
Менять только если хочется заменить «———» на `· · ·` — но текстура «———»
работает как уверенный визуальный разрыв между «приветствие / антифишинг
/ "выберите действие"», и пенсионеры различают его хорошо. **Оставить**.

### 21. `HELP_USER` / `RULES_TEXT` / `RULES_SHORT` (texts.py:290-356)

Длинные информационные тексты. Bullets и абзацы уже работают.
**Оставить**.

### 22. `OP_HELP` (texts.py:414-450) — оператор-памятка

Длинный help-текст с разделами через двойную пустую строку. Уже
структурирован, эмодзи `🛡️` префиксы там где нужно. **Оставить**.

### 23. `SUBSCRIBE_*` / `UNSUBSCRIBE_*` (texts.py:358-374)

Confirm-сообщения после действия. Однострочные. Оставить.

### 24. `BROADCAST_HEADER` (texts.py:387-390)

Используется внутри тела рассылки как «шапка письма». Менять `────────────────`
на `━━━ ОБЪЯВЛЕНИЕ ━━━` для симметрии с CITIZEN_REPLY_TEMPLATE — да,
**сделать**, но это уровень P1:

```
BROADCAST_HEADER = (
    "━━━ ОБЪЯВЛЕНИЕ АДМИНИСТРАЦИИ ━━━\n"
    "Елизовский муниципальный округ"
)
```

Подписчик видит как письмо «от муниципалитета», та же грамматика что
у `CITIZEN_REPLY_TEMPLATE` (texts.py:171-181).

Тесты: тесты рассылок (`test_broadcasts_service_pg.py` /
`test_broadcast_handlers.py`) — substring assertions на «Объявление
Администрации».

### 25. `ADMIN_REPLY_DELIVERED_FINAL` / `INTERMEDIATE` (texts.py:160-169)

Подтверждение оператору после отправки ответа. Однострочные, эмодзи
в начале. Оставить.

---

## Priority sort + дельта изменений

| # | Приоритет | Объект | Файл:строки |
|---|-----------|--------|-------------|
| 1 | P0 | OP_BROADCAST_PREVIEW | texts.py:520-527 |
| 2 | P0 | OP_BROADCAST_CARD | texts.py:563-571 |
| 5 | P1 | admin_followups_block | card_format.py:74 |
| 6 | P1 | _render_timeline (history header) | card_format.py:126 |
| 7 | P1 | OP_BROADCAST_LIST_HEADER | texts.py:559 |
| 10 | P1 | _show_operator_card | admin_operators.py:309-316 |
| 11 | P1 | OP_BROADCAST_STARTED/PROGRESS/DONE | texts.py:543-557 |
| 12 | P1 | CONSENT_REVOKE_CONFIRM / ERASE_CONFIRM | texts.py:238-270 |
| 24 | P1 | BROADCAST_HEADER | texts.py:387-390 |
| 13 | P2 | admin_settings.py (все экраны) | весь файл |
| 14 | P2 | admin_operators.py wizard cards | весь файл |
| 15 | P2 | menu.py inline-карточки → texts.py + грамматика | menu.py:108-117, 213-216, 274-281 |

`OP_TMPL_*` и confirmation-карточки воронки (geo, contact) — **не трогать**,
они уже соответствуют или сознательно отличаются.

Глобальный шаблон правки: где `────` или `──────────` — заменить на
`· · ·` если разделитель «мета / контент», и на пустую строку + `━━━━━━━━━━━━━━━━` если это CTA-разделитель. Шапки везде
`━━━ ОПИСАНИЕ В CAPS ━━━`. Эмодзи строго в начале строки или вообще
без них.

## Тесты под удар

`test_card_format.py` · `test_appeal_card_timeline.py` ·
`test_admin_card_render.py` · `test_broadcast_handlers.py` ·
`test_broadcast_history_card.py` · `test_broadcast_templates_handlers.py`
(если P1/2 OP_TMPL'и трогаем) · `test_admin_operators.py` ·
`test_admin_settings_audit.py` · `test_handlers_menu.py` ·
`test_handlers_menu_extra.py`.

В подавляющем большинстве — substring assertions («Доставлено:»,
«Рассылка #», «📜», «Статус:»), которые ловят формат частично. Менять
синхронно с правкой шаблона. Перед коммитом — `pytest -k "card or
broadcast or menu or operators or settings"`.
