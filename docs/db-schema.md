# Схема базы данных

Диаграмма «сущность-связь» (ER-диаграмма) ниже собрана вручную по файлу `bot/aemr_bot/db/models.py`. Обновляйте её при изменении моделей или миграций.

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

| Таблица | Назначение | Срок хранения |
|---|---|---|
| `users` | Житель. Профиль, состояние пошаговой анкеты, флаги `is_blocked` и `subscribed_broadcast`. | Бессрочно. Обезличивание через `/forget` или `/erase`. |
| `operators` | Оператор. `max_user_id`, ФИО, роль, активность. | Бессрочно. |
| `appeals` | Обращение. Одно обращение — одна строка с номером `#N`. | Бессрочно. |
| `messages` | История сообщений внутри обращения. Жителем и оператором. | Бессрочно. |
| `events` | Журнал сырых обновлений (Update) от MAX. Нужен для защиты от повторов и для отладки. | Автоматическая очистка раз в сутки удаляет записи старше 30 дней. |
| `audit_log` | Журнал действий операторов: ответ, закрытие, удаление персональных данных (ПДн), изменение настроек. | Бессрочно. |
| `settings` | Параметры, которые редактируются прямо в БД. Ссылки, тексты, контакты, тематики. | Бессрочно. |
| `broadcasts` | Метаданные рассылок: текст, кто отправил, счётчики, статус. | Бессрочно. |
| `broadcast_deliveries` | Одна строка на каждую попытку доставки (житель × рассылка). | Бессрочно. |

## Ключевые инварианты

- `users.max_user_id` уникален в пределах платформы MAX. На него опирается поиск жителя при следующем `/start`. Дубликатов записей в таблице не возникает.
- `events.idempotency_key` уникален. Это основа защиты от повторных обновлений (когда MAX повторно шлёт одно и то же событие). Дубликат тихо отбрасывается на уровне ограничения уникальности.
- `appeals.admin_message_id` — идентификатор сообщения карточки обращения в служебной группе. По нему функция `handle_operator_reply` находит обращение, на которое отвечает оператор свайпом или командой `/reply`. До момента публикации карточки поле равно NULL.
- `users.dialog_state` хранится как `String(32)`. Значения берутся из перечисления `DialogState` в коде. Перевод в тип `Enum` базы PostgreSQL — одно из возможных направлений развития (см. [ADR-001 §11](ADR-001-architecture.md)). Для MVP оставили строкой ради скорости миграций при добавлении новых состояний.
- `appeals.attachments` и `messages.attachments` — это массивы JSONB с сериализованными вложениями MAX. При пересылке в служебную группу они восстанавливаются в Pydantic-объекты `Attachments` через `TypeAdapter`.
- Подписчики рассылки определяются условием `users.subscribed_broadcast=true AND users.is_blocked=false`. После `/erase` оба флага переключаются (`subscribed_broadcast=false`, `is_blocked=true`). Это исключает повторные сообщения жителю без нового согласия.
- `broadcasts.subscriber_count_at_start` — снимок количества получателей на момент старта рассылки. Нужен для прогресс-бара и итогового расчёта «доставлено + не доставлено». В ходе рассылки не пересчитывается.

## Связь с миграциями Alembic

Миграции лежат в `bot/aemr_bot/db/alembic/versions/`. Файлы: `0001_initial.py`, `0002_broadcast.py`, `0003_phone_normalized.py`, `0004_indexes_and_autovacuum.py`. Каждое изменение моделей фиксируется новой миграцией. Текущую версию в БД проверяет команда `alembic current` внутри контейнера.

```bash
docker compose exec bot alembic current
docker compose exec bot alembic upgrade head
```
