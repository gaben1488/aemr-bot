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
        bool subscribed_broadcast
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

    broadcasts ||--o{ broadcast_deliveries : "broadcast_id"
    operators ||--o{ broadcasts : "created_by_operator_id"
    users ||--o{ broadcast_deliveries : "user_id"

    broadcasts {
        int id PK
        int created_by_operator_id FK
        text text
        int subscriber_count_at_start
        timestamptz started_at
        timestamptz finished_at
        string status
        int delivered_count
        int failed_count
        string admin_message_id
        timestamptz created_at
    }

    broadcast_deliveries {
        int id PK
        int broadcast_id FK
        int user_id FK
        timestamptz delivered_at
        text error
    }
```

## Таблицы по назначению

| Таблица | Назначение | Ретенция |
|---|---|---|
| `users` | Житель: профиль, FSM-состояние воронки, флаги `is_blocked` и `subscribed_broadcast` | бессрочно (анонимизация по `/forget` или `/erase`) |
| `operators` | Оператор: `max_user_id`, ФИО, роль, активность | бессрочно |
| `appeals` | Обращение: одно обращение — одна строка `#N` | бессрочно |
| `messages` | История сообщений внутри обращения (житель ↔ оператор) | бессрочно |
| `events` | Лог сырых Update от MAX для идемпотентности и отладки | бессрочно (см. инварианты ниже — авто-чистка не реализована) |
| `audit_log` | Действия операторов (ответ, закрытие, удаление ПДн, изменение настроек) | бессрочно |
| `settings` | Редактируемые из БД параметры (URL, тексты, контакты, тематики) | бессрочно |
| `broadcasts` | Метаданные рассылок: текст, кто отправил, счётчики, статус | бессрочно |
| `broadcast_deliveries` | По одной строке на каждую попытку доставки (житель × рассылка) | бессрочно |

## Ключевые инварианты

- `users.max_user_id` уникален в пределах MAX-платформы. На него опирается весь дедуплицированный поиск жителя при следующем `/start`.
- `events.idempotency_key` уникален — основа защиты от дубликатов Update-ов. Авто-ретеншн пока не реализован; таблица растёт линейно к трафику. На малом масштабе АЕМР это годится; авто-чистку старше N дней оставляем как направление развития (см. [ADR-001 §11](ADR-001-architecture.md)).
- `appeals.admin_message_id` — `mid` текстовой карточки в админ-группе. По нему `handle_operator_reply` находит обращение, на которое отвечает оператор свайпом или `/reply`. NULL до момента отправки карточки в группу.
- `users.dialog_state` хранится как `String(32)`, значения — из `DialogState` enum в коде. Перевод в Postgres `Enum` — одно из возможных направлений развития ([ADR-001 §11](ADR-001-architecture.md)). На MVP сознательно оставлен строкой ради скорости миграций при добавлении состояний.
- `appeals.attachments` и `messages.attachments` — JSONB-массивы с сериализованными MAX-вложениями. Воссоздаются в pydantic-объекты `Attachments` через `TypeAdapter` при пересылке в админ-группу.
- Подписчики рассылки = `users.subscribed_broadcast=true AND users.is_blocked=false`. После `/erase` оба флага переключаются (`subscribed_broadcast=false`, `is_blocked=true`), чтобы жителя нельзя было повторно тронуть рассылкой без нового согласия.
- `broadcasts.subscriber_count_at_start` — снимок количества получателей на момент старта; нужен для прогресс-бара и итогового расчёта `delivered + failed`. Не пересчитывается при ходе рассылки.

## Связь со схемами Alembic

Миграции — в `bot/aemr_bot/db/alembic/versions/` (`0001_initial.py`, `0002_broadcast.py`). Каждое изменение моделей фиксируется новой миграцией; версия в БД проверяется командой `alembic current` внутри контейнера.

```bash
docker compose exec bot alembic current
docker compose exec bot alembic upgrade head
```
