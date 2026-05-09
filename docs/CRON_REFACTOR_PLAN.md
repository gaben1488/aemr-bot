# CRON_REFACTOR_PLAN — план refactor'а `services/cron.py`

**Дата:** 2026-05-10. **Применил:** `simplify` + `engineering:tech-debt`.

## Контекст

`bot/aemr_bot/services/cron.py:build_scheduler` имеет 522 строки кода и **8 nested closures** — каждая job APScheduler определена как async function внутри одного гигантского builder'а.

```python
def build_scheduler(bot, send_admin_document, send_admin_text):
    scheduler = AsyncIOScheduler(...)

    async def backup_with_alert():     # +25 строк
        ...
    scheduler.add_job(backup_with_alert, ...)

    async def events_retention():      # +30 строк
        ...
    scheduler.add_job(events_retention, ...)

    # ... × 8 раз ...

    return scheduler
```

**Проблемы:**

1. **Тестируемость**: чтобы протестировать одну job — нужно создать весь scheduler. Сейчас тестов на cron.py нет вообще (`bot/tests/`).
2. **Читаемость**: одна функция = 522 строки. Импорты разбросаны (часть локальных внутри closure).
3. **Скрытая зависимость через capture**: closure захватывает `bot`, `send_admin_text`, `send_admin_document` — разработчик не видит явно, какие объекты нужны конкретной job.
4. **Циклы**: при попытке вынести в module-level некоторые closures хотят импортировать `from aemr_bot.main import bot` — это создаёт цикл `services → main`. Решение — передать `bot` параметром.

## Этапы

### ✅ Этап 1 — quick-wins (2026-05-10, commit `H. cron.py refactor`)

Вынесены на module-level:

- `_format_appeal_lines(appeals, *, max_rows=10)` — чистая функция, без closure-зависимостей
- `_send_with_open_tickets_button(bot, text)` — был closure-захват `bot`, теперь принимает явно

**Эффект:** -50 строк сложности из `build_scheduler`. Подготовлена почва для этапа 2.

**Регрессия-риск:** низкий. Изменены 2 call-sites внутри `working_hours_*_reminder`. Тесты 33 passed.

### Этап 2 — module-level closures (планируется)

Каждую из 8 closures вынести наружу как:

```python
async def _job_backup_with_alert(bot, send_admin_text):
    """Был closure в build_scheduler. Принимает зависимости явно."""
    try:
        out = await _backup_db()
        if out is None:
            await send_admin_text("⚠️ ...")
    except Exception:
        log.exception("backup_with_alert wrapper failed")
        await send_admin_text("⚠️ ...")


def build_scheduler(bot, send_admin_document, send_admin_text):
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(
        functools.partial(_job_backup_with_alert, bot, send_admin_text),
        CronTrigger(...),
        name="db-backup",
    )
    # ... × 8 раз ...
    return scheduler
```

**Список closures для выноса:**

1. `backup_with_alert` → `_job_backup_with_alert(send_admin_text)`
2. `events_retention` → `_job_events_retention()` (нет closure-зависимостей)
3. `selfcheck` → `_job_selfcheck(bot)`
4. `monthly_report` → `_job_monthly_report(bot, send_admin_document)`
5. `pulse` → `_job_pulse(bot)`
6. `appeals_5y_retention` → `_job_appeals_5y_retention()` (нет closure)
7. `pdn_retention_check` → `_job_pdn_retention_check(bot)`
8. `funnel_watchdog` → `_job_funnel_watchdog(bot)`
9. `working_hours_open_reminder` → `_job_working_hours_open_reminder(bot)`
10. `working_hours_overdue_reminder` → `_job_working_hours_overdue_reminder(bot)`

**Build_scheduler после этапа 2:** ~80 строк (только конфигурация и `add_job` вызовы).

**Регрессия-риск:** **средний**. Скрытые closure-зависимости могут проявиться (например, переменные, определённые между closures, неявно используемые). Митигации:

- Перед каждым выносом — `grep` всех имён внутри closure
- Smoke-тест в dev-среде перед production deploy
- Roll-back через `git revert` (один коммит = один этап)

### Этап 3 — unit-тесты на jobs (после этапа 2)

Когда jobs стали module-level coroutines, можно мокать `bot` и `send_admin_text` через `unittest.mock.AsyncMock` и тестировать каждую job отдельно:

```python
@pytest.mark.asyncio
async def test_backup_with_alert_sends_warning_on_none():
    send = AsyncMock()
    with patch("aemr_bot.services.cron._backup_db", return_value=None):
        await _job_backup_with_alert(send)
    send.assert_called_once_with(StringContaining("не выполнен"))
```

**Целевое покрытие**: 8 happy-path + 8 error-path = 16 unit-тестов на cron.py.

### Этап 4 — выделить `_backup_db` и его helpers в отдельный модуль

`_backup_db`, `_run_pg_dump`, `_run_pg_dump_encrypted`, `_upload_to_s3`, `_rotate_backups`, `_build_pg_env` — это **216 строк** инфраструктурного кода для бэкапов. Выделить в `services/backup.py` или `services/db_backup.py`.

**Эффект:** `cron.py` сократится до ~250 строк (только scheduler + 10 jobs); backup-логика — отдельный тестируемый модуль.

## Что НЕ делать в одном большом коммите

Нельзя делать все 4 этапа за раз: при ошибке невозможно отдельно откатить. Поэтапный план:

| Этап | Коммитов | Риск | Когда |
|---|---|---|---|
| 1 — quick wins (helpers) | 1 (✅ сделан) | низкий | сделано 2026-05-10 |
| 2 — closures на module-level | 5 (по 2 closures за раз) | средний | по запросу владельца, не сейчас |
| 3 — unit-тесты на jobs | 5 (по 3-4 теста за раз) | низкий | после этапа 2 |
| 4 — выделить backup в свой модуль | 1 крупный | средний | после этапа 3 |

**Итог по времени:** ~6-8 часов суммарно через 12 коммитов. Можно растянуть на месяц.

## Когда делать этапы 2-4

**НЕ делать** в одной из ситуаций:

- Идёт активная разработка фич (можно маскировать регрессии)
- Низкий объём обращений (5-6/день — текущий) — нет ценности от тестируемости
- Команда из одного разработчика (никто кроме автора не читает cron.py)

**ДЕЛАТЬ** когда:

- Команда выросла >2 человек, нужны independent unit-тесты для onboarding
- Возникает нужда добавить новые jobs (например, weekly stats report) — рефакторинг **до** добавления, не после
- Регрессионный баг в одной из jobs — повод заодно вынести её на module-level

## Решение на сейчас

**Этап 1 сделан**, коммит `H. cron.py refactor`. Этапы 2-4 заморожены до явной потребности. Файл `cron.py` сейчас **750 строк** (с docstrings и комментариями), но логическая сложность снижена.
