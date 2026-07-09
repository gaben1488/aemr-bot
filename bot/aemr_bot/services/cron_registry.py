"""Декларативный реестр cron-задач для anti-drift тестирования docs.

`JOB_REGISTRY` — единственный source-of-truth (читаемый машиной) для
имён, расписаний и назначения всех cron-задач, регистрируемых
`build_scheduler` в `cron.py`. Тест `tests/test_cron_docs_sync.py`
проверяет, что каждый `id` из реестра упомянут в каноничных docs
(`HOW_IT_WORKS.md`, `RUNBOOK.md`, `SYSADMIN.md`, `COMPLIANCE_WITH_REGLAMENT_v7.md`)
— любое добавление новой задачи без записи в docs валит CI с явным
сообщением «cron `X` отсутствует в `Y.md`».

**Workflow добавления нового cron:**

1. Добавить новую запись в `JOB_REGISTRY` (id, schedule_human, purpose).
2. `build_scheduler` в `cron.py` — `scheduler.add_job(...)`.
3. CI-тест `test_cron_docs_sync` упадёт → добавить строку в таблицу
   cron-задач в docs (HOW_IT_WORKS.md/RUNBOOK.md/SYSADMIN.md/COMPLIANCE_v7).
4. CI зелёный → можно мерджить.

См. план MLP, Codex PR 8 «Cron registry + docs generation hook».
"""
from __future__ import annotations


# Запись реестра. `id` — точное имя job_id из `scheduler.add_job(id=...)`.
# `schedule_human` — человекочитаемое описание (не cron-expression).
# `purpose` — короткое назначение для docs.
JOB_REGISTRY: list[dict[str, str]] = [
    # ── Pulse (heartbeat в админ-чат, проверка «бот жив») ────────────
    {
        "id": "pulse-hourly",
        "schedule_human": "каждый час 24/7 в :05",
        "purpose": "базовый heartbeat «бот работает»",
    },
    {
        "id": "pulse-workhours-extra",
        "schedule_human": "пн–пт 09:00–17:59 в :35",
        "purpose": "дополнительный пинг в рабочее время",
    },
    {
        "id": "startup-pulse",
        "schedule_human": "+5 секунд после старта (DateTrigger, одноразово)",
        "purpose": "подтверждение, что процесс поднялся",
    },
    # ── Health / monitoring ──────────────────────────────────────────
    {
        "id": "health-selfcheck",
        "schedule_human": "каждые HEALTHCHECK_INTERVAL_MIN мин (5 по умолчанию)",
        "purpose": "мониторит heartbeat, шлёт алёрт при healthy↔unhealthy",
    },
    {
        "id": "healthcheck-ping",
        "schedule_human": "каждые 5 мин",
        "purpose": "внешний ping, только если задан HEALTHCHECK_URL",
    },
    # ── Operational reminders ────────────────────────────────────────
    {
        "id": "funnel-watchdog",
        "schedule_human": "ежечасно в :15",
        "purpose": "сброс зависших анкет воронки приёма обращения",
    },
    {
        "id": "open-reminder-workhours",
        "schedule_human": "пн–пт 09:00–11:59 и 13:00–17:59 в :10",
        "purpose": "напоминание оператору об открытых обращениях",
    },
    {
        "id": "overdue-reminder-workhours",
        "schedule_human": "пн–пт 09:00–11:59 и 13:00–17:59 в :40",
        "purpose": "напоминание о просроченных по SLA",
    },
    {
        "id": "monthly-stats",
        "schedule_human": "1 числа в 09:00",
        "purpose": "автоматический отчёт за прошлый месяц",
    },
    # ── Retention / 152-ФЗ ──────────────────────────────────────────
    {
        "id": "events-retention",
        "schedule_human": "ежедневно 04:00",
        "purpose": "удаление событий старше 30 дней (idempotency cleanup)",
    },
    {
        "id": "pdn-retention",
        "schedule_human": "ежедневно 04:30",
        "purpose": "обезличивание жителей через 30 дней после revoke (152-ФЗ)",
    },
    {
        "id": "appeals-5y-retention",
        "schedule_human": "ежедневно 04:45",
        "purpose": "обезличивание содержимого обращений старше 5 лет",
    },
    {
        "id": "audit-log-retention",
        "schedule_human": "ежедневно 04:15",
        "purpose": "удаление audit_log старше AUDIT_LOG_RETENTION_DAYS (default 365), 152-ФЗ",
    },
    # ── Retention catch-up (разово при старте, +30 сек) ──────────────
    # Misfire grace (120 сек) не спасает от простоя дольше 2 минут в
    # момент тика 04:xx — при более долгом простое regular-job молча
    # теряется до следующих суток. Catch-up дублирует вызов той же
    # идемпотентной функции при каждом старте процесса; повторный
    # прогон безопасен (см. cron.py::build_scheduler).
    {
        "id": "events-retention-catchup",
        "schedule_human": "+30 секунд после старта (DateTrigger, одноразово)",
        "purpose": "догоняющий прогон events-retention, если суточный тик был пропущен простоем",
    },
    {
        "id": "pdn-retention-catchup",
        "schedule_human": "+30 секунд после старта (DateTrigger, одноразово)",
        "purpose": "догоняющий прогон pdn-retention, если суточный тик был пропущен простоем",
    },
    {
        "id": "appeals-5y-retention-catchup",
        "schedule_human": "+30 секунд после старта (DateTrigger, одноразово)",
        "purpose": "догоняющий прогон appeals-5y-retention, если суточный тик был пропущен простоем",
    },
    {
        "id": "audit-log-retention-catchup",
        "schedule_human": "+30 секунд после старта (DateTrigger, одноразово)",
        "purpose": "догоняющий прогон audit-log-retention, если суточный тик был пропущен простоем",
    },
    # ── Operational housekeeping ─────────────────────────────────────
    {
        "id": "stale-operators-cleanup",
        "schedule_human": "ежедневно 04:20",
        "purpose": "пометить покинувших служебную группу операторов как неактивных",
    },
    {
        "id": "threat-intel-refresh",
        "schedule_human": "ежечасно в :17",
        "purpose": "обновление локального кэша URL-хостов (URLhaus + ThreatFox + PhishTank)",
    },
    {
        "id": "broadcast-draft-reaper",
        "schedule_human": "ежечасно в :37",
        "purpose": "DRAFT-рассылки старше 30 минут → FAILED",
    },
    # ── Backup ──────────────────────────────────────────────────────
    {
        "id": "db-backup",
        "schedule_human": "каждое вс в BACKUP_HOUR:BACKUP_MINUTE (03:00 по умолчанию)",
        "purpose": "pg_dump → GPG → named volume, ротация",
    },
]


def all_ids() -> set[str]:
    """Множество всех зарегистрированных job_id."""
    return {entry["id"] for entry in JOB_REGISTRY}


def lookup(job_id: str) -> dict[str, str] | None:
    """Найти запись по id, либо None."""
    for entry in JOB_REGISTRY:
        if entry["id"] == job_id:
            return entry
    return None
