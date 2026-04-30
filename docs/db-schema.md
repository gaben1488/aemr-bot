# Схема базы данных

ER-диаграмма ниже сгенерирована вручную из `bot/aemr_bot/db/models.py`. Обновляйте при изменении моделей или миграций.

```mermaid
erDiagram
    users ||--o{ appeals : "user_id"
    appeals ||--o{ messages : "appeal_id"
    operators ||--o{ appeals : "assigned_operator_id"
    operators ||--o{ messages : "operator_id"

    users {
        int id PK
        bigint max_user_id UK
        string first_name
        string phone
        timestamptz consent_pdn_at
        bool is_blocked
        string dialog_state
        jsonb dialog_data
        timestamptz created_at
        timestamptz updated_at
    }

    operators {
        int id PK
        bigint max_user_id UK
        string full_name
        string role
        bool is_active
        timestamptz created_at
    }

    appeals {
        int id PK
        int user_id FK
        string status
        string address
        string topic
        text summary
        jsonb attachments
        string admin_message_id
        int assigned_operator_id FK
        timestamptz created_at
        timestamptz answered_at
        timestamptz closed_at
    }

    messages {
        int id PK
        int appeal_id FK
        string direction
        text text
        jsonb attachments
        string max_message_id
        int operator_id FK
        timestamptz created_at
    }

    events {
        int id PK
        string idempotency_key UK
        string update_type
        jsonb payload
        timestamptz received_at
    }

    audit_log {
        int id PK
        bigint operator_max_user_id
        string action
        string target
        jsonb details
        timestamptz created_at
    }

    settings {
        string key PK
        jsonb value
        timestamptz updated_at
    }
```

## Таблицы по назначению

| Таблица | Назначение | Ретенция |
|---|---|---|
| `users` | Житель: профиль + текущее состояние FSM воронки | бессрочно (до `/forget`) |
| `operators` | Оператор: max_user_id, ФИО, роль, активность | бессрочно |
| `appeals` | Обращение: один обращение = одна строка #N | бессрочно |
| `messages` | История сообщений внутри обращения (citizen ↔ operator) | бессрочно |
| `events` | Лог сырых Update от MAX для idempotency и debugging | 30 дней |
| `audit_log` | Действия операторов (ответ, закрытие, удаление ПДн, изменение настроек) | бессрочно |
| `settings` | Редактируемые из БД параметры (URL электронной приёмной, тексты, контакты) | бессрочно |

## Ключевые инварианты

- `users.max_user_id` уникален в пределах MAX-платформы.
- `events.idempotency_key` уникален — основа защиты от дубликатов update'ов от MAX.
- `appeals.admin_message_id` — message_id текстовой карточки в админ-группе. Используется чтобы привязать reply оператора обратно к обращению. NULL до момента отправки карточки в группу.
- `users.dialog_state` хранится как `String(32)`, значения — из `DialogState` enum в коде. Phase D кандидат на миграцию в Postgres `Enum` тип (см. ADR-001 §9).
- `appeals.attachments` и `messages.attachments` — JSONB-массивы с сериализованными MAX-вложениями. Воссоздаются обратно в pydantic-объекты `Attachments` через `TypeAdapter` при пересылке в админ-группу.

## Связь со схемами Alembic

Миграции — в `bot/alembic/versions/`. Каждое изменение моделей фиксируется новой миграцией; версия в БД проверяется командой `alembic current` внутри контейнера.

```bash
docker compose exec bot alembic current
docker compose exec bot alembic upgrade head
```
