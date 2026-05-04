# Архитектурные диаграммы

Визуализация ключевых процессов и структуры кода aemr-bot. Все диаграммы написаны на Mermaid. GitHub отрисовывает их прямо в браузере. Полная схема базы данных лежит в [db-schema.md](db-schema.md). Здесь — ссылка для целостности.

## 1. Жизненный цикл обращения (нотация BPMN)

Один путь от первого `/start` жителя до закрытия обращения координатором. BPMN — это нотация описания деловых процессов. В Mermaid её роль выполняет `flowchart` с разделением на дорожки через `subgraph`.

```mermaid
flowchart TD
    classDef citizen fill:#e1f5ff,stroke:#0288d1,color:#01579b
    classDef bot fill:#f3e5f5,stroke:#7b1fa2,color:#4a148c
    classDef operator fill:#fff3e0,stroke:#f57c00,color:#e65100
    classDef db fill:#e8f5e9,stroke:#388e3c,color:#1b5e20
    classDef terminal fill:#ffebee,stroke:#c62828,color:#b71c1c

    Start([Житель открывает бота]):::citizen
    Welcome[Бот: приветствие<br/>5 кнопок главного меню]:::bot
    BtnAppeal{«📝 Написать<br/>обращение»?}:::citizen

    Start --> Welcome
    Welcome --> BtnAppeal

    BtnAppeal -->|нет| OtherFlow[Контакты / приём граждан /<br/>полезная информация]:::bot
    BtnAppeal -->|да| ConsentCheck{consent_pdn_at<br/>уже стоит?}:::bot

    OtherFlow --> EndOther([Возврат в меню]):::terminal

    ConsentCheck -->|нет| Consent[Бот: текст согласия<br/>+ PDF политики ПДн]:::bot
    Consent --> ConsentChoice{«✅ Согласен» /<br/>«❌ Отказаться»?}:::citizen
    ConsentChoice -->|отказ| EndDecline([Возврат в меню]):::terminal
    ConsentChoice -->|согласие| SaveConsent[(users.consent_pdn_at = now)]:::db

    ConsentCheck -->|да| ContactCheck
    SaveConsent --> ContactCheck{phone уже<br/>сохранён?}:::bot

    ContactCheck -->|нет| AskContact[Бот: запрос контакта<br/>через RequestContactButton]:::bot
    AskContact --> ShareContact[Житель: «📲 Поделиться<br/>контактом»]:::citizen
    ShareContact --> SaveContact[(users.phone,<br/>users.first_name)]:::db

    ContactCheck -->|да| AskName
    SaveContact --> AskName[Бот: запрос имени]:::bot

    AskName --> InputName[Житель вводит имя]:::citizen
    InputName --> AskAddress[Бот: запрос адреса]:::bot
    AskAddress --> InputAddress[Житель вводит адрес]:::citizen
    InputAddress --> AskTopic[Бот: 11 тематик<br/>клавиатурой 2×N]:::bot
    AskTopic --> ChooseTopic[Житель выбирает]:::citizen
    ChooseTopic --> AskSummary[Бот: «Опишите суть.<br/>Можно фото, гео»]:::bot

    AskSummary --> InputSummary[Житель: текст / фото / гео<br/>несколько сообщений]:::citizen
    InputSummary --> Trigger{Кнопка «Отправить»<br/>или 60 сек тишины?}:::bot
    Trigger -->|ещё пишет| InputSummary

    Trigger -->|финализация| CreateAppeal[(appeals.status = new<br/>address, topic, summary,<br/>attachments JSONB)]:::db
    CreateAppeal --> AdminCard[Бот → админ-группа:<br/>📨 Новое обращение #N<br/>+ reply'ом фото]:::bot
    AdminCard --> AckCitizen[Бот → житель:<br/>«Обращение #N принято»]:::bot

    AdminCard --> WaitOp{Оператор отвечает?}:::operator

    WaitOp -->|свайп-reply| ParseReplyLink[link.message.mid →<br/>get_by_admin_message_id]:::bot
    WaitOp -->|/reply N text| ParseCommand[parse appeal_id<br/>из аргумента]:::bot
    WaitOp -->|/close N| CloseSilently[(appeals.status = closed,<br/>closed_at = now)]:::db
    WaitOp -->|таймаут SLA| AlertSLA[/⚠️ Предупреждение в группу/]:::bot

    ParseReplyLink --> ValidateLen{len ≤ 300?}:::bot
    ParseCommand --> ValidateLen
    ValidateLen -->|нет| TooLong[Бот: «Сократите<br/>и пришлите снова»]:::bot
    TooLong --> WaitOp

    ValidateLen -->|да| CheckBlocked{user.is_blocked?}:::bot
    CheckBlocked -->|да| BlockedReply[Бот: «Житель отозвал<br/>согласие — звоните»]:::bot
    BlockedReply --> WaitOp

    CheckBlocked -->|нет| DeliverReply[Бот → жителю:<br/>📬 формальное письмо<br/>от АЕМР]:::bot
    DeliverReply --> SaveAnswered[(appeals.status = answered,<br/>answered_at = now,<br/>messages from_operator)]:::db
    SaveAnswered --> ConfirmGroup[Бот → группа:<br/>✉️ Ответ ушёл]:::bot

    ConfirmGroup --> WaitFollowup{Житель пишет<br/>повторно?}:::citizen
    WaitFollowup -->|нет, 4 ч| EndAnswered([Закрытие по таймауту]):::terminal
    WaitFollowup -->|да| ReopenAppeal[(appeals.status =<br/>in_progress)]:::db
    ReopenAppeal --> FollowupCard[Бот → группа:<br/>💬 Дополнение к #N]:::bot
    FollowupCard --> WaitOp

    CloseSilently --> EndClosed([Закрыто без ответа]):::terminal
    AlertSLA --> WaitOp
```

**Что не показано на схеме:**

- Восстановление застрявших анкет. Функция `recover_stuck_funnels` при старте бота находит жителей, которые застряли в шаге сбора текста, и завершает их обращения. Этот процесс идёт параллельно основному потоку.
- Команды `/erase` и `/forget`. Обезличивание ставит `is_blocked=true`. После этого ветка «Бот → жителю» блокируется на проверке `CheckBlocked`.
- Промежуточный слой защиты от повторов. Между событием от MAX и любым обработчиком стоит `IdempotencyMiddleware`. Любой прямоугольник «Житель» или «Оператор» неявно отбрасывает дубль обновления через ограничение уникальности `events.idempotency_key`.

## 2. Поток события: от MAX до записи в БД

Что происходит внутри бота, когда приходит обновление (Update). Видно, как обработка разветвляется по типу события и по происхождению — пришло из личного диалога или из служебной группы.

```mermaid
flowchart TD
    classDef external fill:#fce4ec,stroke:#c2185b,color:#880e4f
    classDef middleware fill:#e0f2f1,stroke:#00695c,color:#004d40
    classDef handler fill:#fff8e1,stroke:#ff8f00,color:#e65100
    classDef service fill:#ede7f6,stroke:#5e35b1,color:#311b92
    classDef db fill:#e8f5e9,stroke:#388e3c,color:#1b5e20

    MAX([MAX platform-api]):::external
    Polling[Long-polling / Webhook<br/>main.py:_install_polling_timeout]:::middleware
    Idem[IdempotencyMiddleware<br/>handlers/__init__.py]:::middleware
    EventTbl[(events:<br/>idempotency_key)]:::db
    Dispatcher{Dispatcher<br/>match by type}:::middleware

    MAX -->|Update| Polling
    Polling --> Idem
    Idem --> EventTbl
    EventTbl -->|дубль| Drop([Drop silently]):::external
    EventTbl -->|первый раз| Dispatcher

    Dispatcher -->|message_created| MsgRouter{is_admin_chat?}:::handler
    Dispatcher -->|message_callback| CbRouter{is_admin_chat?}:::handler
    Dispatcher -->|bot_started| Start1[start.py::cmd_start]:::handler

    MsgRouter -->|нет| CitizenChat{Команда?}:::handler
    MsgRouter -->|да| AdminChat{Команда?}:::handler

    CitizenChat -->|/start, /menu, /help| StartHandler[start.py]:::handler
    CitizenChat -->|/policy| PolicyCmd[start.py::cmd_policy]:::handler
    CitizenChat -->|/subscribe, /unsubscribe| SubCmd[start.py]:::handler
    CitizenChat -->|/forget| ForgetCmd[start.py::cmd_forget]:::handler
    CitizenChat -->|свободный текст<br/>+ вложения| AppealOnMessage[appeal.py::on_message]:::handler

    AppealOnMessage --> StateDispatch{dialog_state?}:::handler
    StateDispatch -->|AWAITING_CONSENT| FollowupCheck[followup или ignore]:::handler
    StateDispatch -->|AWAITING_NAME| OnName[_on_awaiting_name]:::handler
    StateDispatch -->|AWAITING_ADDRESS| OnAddr[_on_awaiting_address]:::handler
    StateDispatch -->|AWAITING_TOPIC| OnTopic[_on_awaiting_topic]:::handler
    StateDispatch -->|AWAITING_SUMMARY| OnSum[_on_awaiting_summary]:::handler
    StateDispatch -->|IDLE| FollowupHandler[operator_reply.py::<br/>handle_user_followup]:::handler

    OnSum --> AppendChunk[(users.dialog_data<br/>summary_chunks++,<br/>attachments++)]:::db
    AppendChunk --> ResetTimer[Reset 60s timer]:::handler
    ResetTimer --> WaitMore[Ждём ещё]

    AdminChat -->|/reply N text| ReplyCmd[admin_commands.py::cmd_reply]:::handler
    AdminChat -->|/stats, /reopen, /close,<br/>/erase, /setting,<br/>/add_operators, /backup,<br/>/diag, /broadcast, /op_help| AdminOther[admin_commands.py /<br/>broadcast.py]:::handler
    AdminChat -->|свободный текст<br/>+ reply-link| OpReply[operator_reply.py::<br/>handle_operator_reply]:::handler

    OpReply --> ExtractMid[_extract_reply_target_mid<br/>link.message.mid]:::service
    ExtractMid --> FindAppeal[appeals.py::<br/>get_by_admin_message_id]:::service
    FindAppeal --> Deliver[_deliver_operator_reply]:::handler
    ReplyCmd --> Deliver

    Deliver --> CheckBlock{user.is_blocked?}:::handler
    CheckBlock -->|да| AdminWarn[Reply отбит<br/>в админ-группу]:::handler
    CheckBlock -->|нет| SendMsg[bot.send_message<br/>user_id, formal letter]:::service
    SendMsg --> SaveAnswer[(messages,<br/>appeals.answered_at,<br/>audit_log)]:::db

    CbRouter -->|нет| CitizenCb{payload?}:::handler
    CbRouter -->|да| AdminCb{payload?}:::handler

    CitizenCb -->|menu:* / consent:* /<br/>info:* / topic:N /<br/>appeal:show:N / cancel| OnCallback[appeal.py::on_callback]:::handler
    AdminCb -->|broadcast:confirm/<br/>broadcast:abort/<br/>broadcast:stop:N/<br/>broadcast:unsubscribe| BcastCb[broadcast.py]:::handler
    AdminCb -->|op:stats_today/<br/>op:broadcast/<br/>op:help_full| OpHelpCb[admin_commands.py::<br/>op_help callbacks]:::handler
```

## 3. Диаграмма последовательности: доставка ответа оператора

Два пути от текста, написанного оператором, до личного диалога жителя. Различие — как находится номер обращения (`appeal_id`).

```mermaid
sequenceDiagram
    autonumber
    participant Op as Оператор<br/>(в админ-группе)
    participant Bot as aemr-bot
    participant DB as PostgreSQL
    participant MAX as MAX platform-api
    participant Cz as Житель<br/>(в личке)

    Note over Op,Cz: Путь A — свайп-reply

    Op->>Bot: message_created<br/>text + link.type=REPLY<br/>+ link.message.mid
    Bot->>Bot: is_admin_chat? ✓
    Bot->>Bot: _extract_reply_target_mid<br/>(link.message.mid)
    Bot->>DB: get_by_admin_message_id(mid)
    DB-->>Bot: Appeal #N or None

    alt не найден
        Bot->>Op: «Не понял, к какому обращению»
    else найден
        Bot->>DB: SELECT user.is_blocked
        alt is_blocked = true
            Bot->>Op: «Житель отозвал согласие, звоните»
        else can deliver
            Bot->>Bot: len(text) ≤ 300?
            alt слишком длинно
                Bot->>Op: «Сократите и пришлите снова»
            else OK
                Bot->>Bot: card_format.citizen_reply<br/>(formal letter wrap)
                Bot->>MAX: bot.send_message(user_id, text)
                MAX-->>Cz: 📬 Ответ Администрации ЕМР
                MAX-->>Bot: SendedMessage
                Bot->>Bot: extract_message_id<br/>(.message.body.mid)
                Bot->>DB: BEGIN<br/>messages from_operator<br/>appeals.status = answered<br/>audit_log action=reply<br/>COMMIT
                Bot->>Op: ✉️ Ответ ушёл жителю
            end
        end
    end

    Note over Op,Cz: Путь B — команда /reply N

    Op->>Bot: /reply 42 Здравствуйте!...
    Bot->>Bot: is_admin_chat? ✓
    Bot->>Bot: parse appeal_id из argv[0]
    Bot->>DB: appeals.get_by_id(42)
    DB-->>Bot: Appeal or None

    alt не найден
        Bot->>Op: «Обращение #42 не найдено»
    else найден
        Note right of Bot: дальше тот же путь<br/>что в варианте A:<br/>is_blocked / 300 / send / save / ack
    end
```

## 4. Диаграмма последовательности: рассылка `/broadcast`

```mermaid
sequenceDiagram
    autonumber
    participant Op as Оператор<br/>(coordinator/it)
    participant Bot
    participant DB as PostgreSQL
    participant MAX
    participant Subs as Подписчики

    Op->>Bot: /broadcast
    Bot->>DB: count_subscribers()<br/>(subscribed_broadcast=true<br/>AND is_blocked=false)
    DB-->>Bot: N
    Bot->>Op: «Введите текст (≤1000 символов)»
    Op->>Bot: <текст>
    Bot->>Op: Превью + «Разослать N?»<br/>[✅] [❌]

    alt отмена
        Op->>Bot: ❌
        Bot->>Op: «Отменено»
    else подтверждение
        Op->>Bot: ✅
        Bot->>DB: create_broadcast(status=DRAFT)
        Bot->>DB: audit_log<br/>(action=broadcast_send,<br/>chars=N, не текст!)
        Bot->>MAX: send admin start-message<br/>(progress + emergency-stop)
        Bot->>DB: mark_started<br/>(status=SENDING, admin_mid)
        Bot->>DB: list_subscriber_targets()<br/>(snapshot, close txn!)
        DB-->>Bot: [(id, max_user_id), ...]

        loop по каждому подписчику
            Bot->>DB: get_status (fresh txn)
            alt CANCELLED
                Note over Bot: break — экстренный стоп
            else SENDING
                Bot->>MAX: send_message(user_id, body<br/>+ unsubscribe button)
                MAX-->>Subs: 📢 Объявление АЕМР
                Bot->>DB: record_delivery<br/>(error or null)

                opt каждые 5 сек
                    Bot->>DB: update_progress
                    Bot->>MAX: edit_message<br/>(новый текст бара)
                end

                Bot->>Bot: sleep(1 / rate_limit)
            end
        end

        Bot->>DB: mark_finished<br/>(DONE / CANCELLED)
        Bot->>MAX: edit final message<br/>(итоги)
    end
```

## 5. Схема базы данных

Полная схема — в [db-schema.md](db-schema.md). Девять таблиц. Связи сводятся к трём ключевым:

- `users → appeals → messages`. Один житель — много обращений. В каждом обращении — много сообщений.
- `operators ← appeals.assigned_operator_id` и `operators ← messages.operator_id`. Кто взял обращение и кто ответил.
- `broadcasts → broadcast_deliveries ← users`. Метаданные рассылки и матрица доставок.

Опорные таблицы без внешних ключей: `events` (журнал обновлений для защиты от повторов), `audit_log` (журнал действий операторов), `settings` (хранилище «ключ → значение» для редактируемой конфигурации).

## 6. Развёртывание

```mermaid
flowchart LR
    classDef ext fill:#fce4ec,stroke:#c2185b
    classDef bot fill:#e3f2fd,stroke:#1565c0
    classDef db fill:#e8f5e9,stroke:#388e3c
    classDef opt fill:#fff8e1,stroke:#ff8f00,stroke-dasharray: 5 5

    Citizen([Житель MAX]):::ext
    Operator([Оператор MAX<br/>в админ-группе]):::ext

    BotContainer[bot:<br/>Python 3.12<br/>maxapi + APScheduler<br/>aiohttp /healthz<br/>127.0.0.1 only]:::bot
    DBContainer[(db:<br/>PostgreSQL 16<br/>internal network)]:::db

    BackupVol[(named-volume<br/>/backups/<br/>aemr-*.sql.gpg)]:::db

    MAX([MAX platform-api<br/>platform-api.max.ru]):::ext
    HCInternal[selfcheck cron<br/>→ алерт в админ-группу]:::bot

    Citizen <-->|outbound long-polling| MAX
    Operator <-->|outbound long-polling| MAX
    MAX <--> BotContainer
    BotContainer <--> DBContainer
    BotContainer -->|еженедельно<br/>pg_dump → gpg AES-256| BackupVol
    BotContainer --- HCInternal
```

Размещение на собственном сервере. Опросный режим связи. Бот делает **только исходящие** запросы на `platform-api.max.ru`. Ни один входящий порт наружу не публикуется. Адрес `/healthz` слушает только `127.0.0.1:8080` — это нужно для проверки здоровья контейнера в Docker Compose. База данных доступна только во внутренней сети Docker. Загрузка резервных копий в S3-хранилище опциональна и тоже исходящая. Стек серверного режима связи (nginx + certbot) лежит в дополнительном профиле Docker Compose `webhook` на случай будущего перехода. **В производственной сборке он не используется.**

## 7. Состояния обращения (диаграмма состояний)

```mermaid
stateDiagram-v2
    [*] --> new: бот создал #N
    new --> in_progress: оператор сделал reply<br/>(или /reopen)
    new --> closed: /close (без ответа)

    in_progress --> answered: оператор ответил<br/>(swipe или /reply)
    in_progress --> closed: /close

    answered --> in_progress: житель написал<br/>повторно
    answered --> [*]: 4ч таймаут SLA

    closed --> in_progress: /reopen N
    closed --> [*]
```

Переход из состояния `answered` в финальное сейчас не автоматический. Обращение остаётся в `answered` до явного действия. Либо житель пишет повторно, либо оператор закрывает командой `/close`, либо ничего не происходит. Автоматическое истечение срока ответа в коде не реализовано. В команде `/stats` есть только метрика «попадание в срок».

## 8. Состояния пошаговой анкеты жителя

```mermaid
stateDiagram-v2
    [*] --> idle
    idle --> awaiting_consent: «📝 Написать обращение»<br/>(если consent_pdn_at нет)
    idle --> awaiting_contact: «📝 Написать обращение»<br/>(если consent есть, но phone нет)
    idle --> awaiting_name: «📝 Написать обращение»<br/>(если phone есть)

    awaiting_consent --> awaiting_contact: «✅ Согласен»
    awaiting_consent --> idle: «❌ Отказаться» / «Отмена»

    awaiting_contact --> awaiting_name: contact-share

    awaiting_name --> awaiting_address: ввод имени
    awaiting_address --> awaiting_topic: ввод адреса
    awaiting_topic --> awaiting_summary: выбор тематики
    awaiting_summary --> idle: финализация<br/>(«Отправить» или 60s timeout)<br/>+ создание appeals.NNN

    awaiting_name --> idle: «Отмена»
    awaiting_address --> idle: «Отмена»
    awaiting_topic --> idle: «Отмена»
    awaiting_summary --> idle: «Отмена»
```

При старте бота функция `recover_stuck_funnels` находит все жители в состоянии `awaiting_summary`, у которых поле `updated_at` старше `APPEAL_TIMEOUT`. Завершает их обращения и возвращает в состояние `idle`.
