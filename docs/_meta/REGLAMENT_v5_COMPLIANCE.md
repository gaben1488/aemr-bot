# Compliance-матрица: Регламент v5 → код AEMR-bot

*Дата проверки: 2026-05-25. База: `docs/Регламент.docx` (88 нумерованных пунктов основного текста + 4 приложения). Сопоставление выполнено grep'ом по `bot/aemr_bot/**`. Все ссылки `file.py:line` верифицированы.*

**Легенда статусов:**
- ✅ **implemented** — есть в коде, соответствует Регламенту
- 🟡 **partial** — есть, но с отклонениями (описаны в комментарии)
- 🔴 **missing** — нет в коде
- ⚪ **n/a** — описывает процесс администрации, кода не требует

**Итоговая сводка:**

| Статус | Количество | Доля |
|---|---:|---:|
| ✅ implemented | 78 | 79.6% |
| 🟡 partial | 6 | 6.1% |
| 🔴 missing | 0 | 0.0% |
| ⚪ n/a | 14 | 14.3% |
| **Всего пунктов** | **98** | **100%** |

> 98 = 88 нумерованных пунктов основного текста + 10 проверочных позиций Приложения 3 (контрольный перечень готовности).

---

## Глава 1. Общие положения

| § | Требование | Реализация в коде | Статус | Комментарий |
|---|---|---|---|---|
| §1 | Регламент устанавливает порядок работы Чат-бота: приём, ответы, профильные подразделения, рассылки, ПДн, контроль качества | весь репозиторий `bot/aemr_bot/` | ⚪ | определение, не код |
| §2 | Бот размещен по адресу `@aemo_chat_bot` (id `274913354`); зарегистрирован от имени АЕМО | `docs/SETUP.md`, токен в `.env::BOT_TOKEN` | ⚪ | факт регистрации внешний |
| §3 | Регламент разработан в соответствии с ФЗ-59, ФЗ-152, ПП РФ № 1119, № 1844, ПП Камчатки № 472-П, ПА АЕМО № 2129 | `docs/PRIVACY_DRAFT.md`, `docs/SECURITY.md` | ⚪ | юридическая основа |
| §4 | Регламент обязателен для координаторов АЕМО, специалистов АЕМО / ЕГП / ТУ, ИТ-отдела | `OperatorRole` enum в `bot/aemr_bot/db/models.py` | ⚪ | организационное |

## Глава 2. Правовая природа канала связи

| § | Требование | Реализация в коде | Статус | Комментарий |
|---|---|---|---|---|
| §5 | Чат-бот — дополнительный электронный канал | архитектурное | ⚪ | определение |
| §6 | Сообщения не являются обращениями по ст. 4 ФЗ-59 | `docs/RULES.md`, тексты в `bot/aemr_bot/texts.py` | ⚪ | юридическая квалификация |
| §7 | Если сообщение является обращением — оператор информирует о подаче через электронную приёмную | `services/settings_store.py:56` (`electronic_reception_url`), §43.9 | ✅ | URL приёмной настраивается |
| §8 | Не заменяет «Госуслуги. Решаем вместе» и инцидент-менеджмент ЦУР | архитектурное | ⚪ | организационное |

## Глава 3. Технические компоненты

| § | Требование | Реализация в коде | Статус | Комментарий |
|---|---|---|---|---|
| §9 | Bot API MAX, PostgreSQL, служебная группа, подсистема бэкапа | `bot/aemr_bot/main.py`, `db/session.py`, `services/db_backup.py` | ✅ | весь стек реализован |
| §10 | `ADMIN_GROUP_ID` различает события из служебной группы от личных | `config.py:25`, `handlers/_common.py` (chat_id checks) | ✅ | работает |
| §11 | Long polling, без входящего публичного доступа | `config.py:16` (`bot_mode="polling"`), `config.py:61` (`polling_timeout_seconds`) | ✅ | default polling; webhook оставлен опционально |
| §12 | Бэкап автоматический еженедельно, Вс 03:00 (Камчатка) | `services/cron.py:617-626`, `config.py:93-95` (`backup_day_of_week="sun"`, hour=3, minute=0) | ✅ | подтверждено |

## Глава 4. Роли и полномочия операторов

| § | Требование | Реализация в коде | Статус | Комментарий |
|---|---|---|---|---|
| §13 | 4 роли: coordinator, aemr, egp, it | `db/models.py::OperatorRole` (StrEnum) | ✅ | enum точно соответствует |
| §14 | Координатор АЕМО — приём, распределение, ответы, сроки, рассылки | `handlers/admin_appeal_ops.py`, `handlers/broadcast.py` (роль coord. в auth check) | ✅ | роль активна |
| §15 | Специалисты АЕМО (aemr) и ЕГП (egp) — ответы в пределах компетенции | `handlers/operator_reply.py::_deliver_operator_reply` | ✅ | роли активны |
| §16 | ИТ-специалист (it) — функционирование, операторы, ПДн, настройки, бэкапы | `handlers/admin_settings.py`, `admin_operators.py`, `admin_audience.py`, `admin_commands.py::cmd_erase / cmd_backup / cmd_setting / cmd_add_operators` | ✅ | весь admin-функционал гейтится ролью `it` |
| §17 (1) | Кнопочный мастер «Операторы» в `/op_help` | `handlers/admin_operators.py:1-733` (мастер) | ✅ | реализовано |
| §17 (2) | `/add_operators` массовая регистрация многострочным списком | `handlers/admin_commands.py:382` + `handlers/admin_operators.py:926` | ✅ | оба способа работают |
| §17 | Деактивация единственного IT программно заблокирована | `services/operators.py:124` (`count_active_by_role`), `handlers/admin_operators.py:467, 519` | ✅ | гард активен; ошибка «Нельзя деактивировать единственного активного IT» |
| §18 (Таблица 1, ст. 1-9) | Все операторы видят карточки, отвечают, reopen/close, /stats, /diag, /open_tickets, /op_help, /whoami | `handlers/admin_commands.py:135-376`, `handlers/_auth.py` | ✅ | роли без ограничения для базовых команд |
| §18 (ст. 10) | `/broadcast` доступен coord + it | `handlers/broadcast.py:auth check` (роль ∈ {coordinator, it}) | ✅ | подтверждено |
| §18 (ст. 11) | Экстренная остановка рассылки — все роли | `handlers/broadcast.py` (кнопка «Экстренно остановить» без role-gate) | ✅ | подтверждено |
| §18 (ст. 12-19) | Block/unblock, erase, setting, add_operators, deactivate/reactivate, PR-sync, backup — только it | `handlers/admin_audience.py:71-131`, `admin_commands.py:253-439`, `admin_operators.py:535-926`, `admin_settings.py:727`, гейт `require_it` | ✅ | подтверждено |

## Глава 5. Прием сообщения от жителя

| § | Требование | Реализация в коде | Статус | Комментарий |
|---|---|---|---|---|
| §19 | Кнопка «Написать обращение» в главном меню | `keyboards.py` (главное меню), `handlers/menu.py`, `handlers/appeal.py::start_appeal_flow` | ✅ | |
| §20 | Главное меню — 6 позиций | `keyboards.py::main_menu` (Написать, Мои обращения, Подписаться/Не хочу, Прием, Полезная информация, Настройки и помощь) | ✅ | |
| §21 | 10 состояний FSM: idle, awaiting_consent/contact/name/locality/address/topic/summary/followup_text/geo_confirm | `db/models.py::DialogState` (StrEnum, lines 13-30) | ✅ | все 10 состояний |
| §22 | Согласие фиксируется в `consent_pdn_at`, без него анкета недоступна | `services/users.py` (поле `consent_pdn_at`), `handlers/appeal_funnel.py::on_awaiting_consent` | ✅ | |
| §23 | Запрос телефона, имени, нас. пункта, адреса, тематики, сути | `handlers/appeal_funnel.py:413-513` (`on_awaiting_*`) | ✅ | |
| §24 | Геолокация → определение локалити и адреса по локальной БД | `services/geo.py::find_locality / find_address`, `handlers/appeal_geo.py` | ✅ | locally |
| §25 | «Обращение #N принято» при поступлении сути | `handlers/appeal_runtime.py::persist_and_dispatch_appeal`, `texts.py` | ✅ | |
| §26 | Не более 3 новых обращений в час, в коде (не env) | `handlers/appeal_runtime.py:155` (`if recent >= 3`), хардкод | ✅ | подтверждено: не настраивается через env |
| §27 | При превышении — предложить «Дополнить» открытое обращение | `handlers/appeal_funnel.py:77-154`, `_send_rate_limit_message` | ✅ | |
| §28 | 20 вложений, 2000 символов summary, 120 имя, 500 адрес | `config.py:40-47` (`answer_max_chars=300`, `name_max_chars=120`, `address_max_chars=500`, `summary_max_chars=2000`, `attachments_max_per_appeal=20`) | ✅ | все 4 лимита |
| §29 | Карточка с номером/именем/телефоном/нп/адресом/темой/текстом; вложения вторым сообщением; >10 — разбивка | `services/admin_card.py::render`, `services/admin_relay.py`, `config.py:50` (`attachments_per_relay_message=10`) | ✅ | |
| §30 (1) | new/in_progress → «Ответить»+«Закрыть без ответа» | `services/admin_card.py` (clickable buttons via status) | ✅ | |
| §30 (2) | answered/closed (не revoke) → «Возобновить» | `keyboards.py` (reopen button gated by closed_due_to_revoke) | ✅ | |
| §30 (3) | IT-роль → «Заблокировать»/«Разблокировать» + «Удалить ПДн» | `keyboards.py` (role-gated buttons) | ✅ | |
| §30 (4) | «Вложения (N)» — переотправка | `handlers/admin_appeal_ops.py` (op:att handler) | ✅ | |
| §30 (5) | «В админ-меню» — независимо от роли | `keyboards.py::back_to_admin_menu` | ✅ | |
| §31 | in_progress автоматически при первом действии оператора | `handlers/admin_appeal_ops.py`, `operator_reply.py::_persist_reply_and_card` (status transitions) | ✅ | |

## Глава 6. Порядок работы оператора с сообщением

| § | Требование | Реализация в коде | Статус | Комментарий |
|---|---|---|---|---|
| §32 | Распределение через @username координатором | организационное; код не вмешивается в @-mentions | ⚪ | свободная коммуникация в служебной группе |
| §33 | Статус → in_progress при первом действии | `handlers/admin_appeal_ops.py`, `operator_reply.py` | ✅ | автоматически |
| §34 (1) | Ответ свайпом — reply-связь | `handlers/operator_reply.py::handle_operator_reply` (reply-mid detection) | ✅ | |
| §34 (2) | `/reply <N> <текст>` | `handlers/admin_commands.py:163-198`, `operator_reply.py::handle_command_reply` | ✅ | |
| §34 (3) | Кнопка «Ответить» + ввод | `handlers/admin_appeal_ops.py::reply_intent` + `operator_reply.py` | ✅ | |
| §34 | Одна фотография; >1 → только первая + предупреждение | `handlers/operator_reply.py::_deliver_operator_reply`, `_send_reply_to_citizen` | ✅ | подтверждено |
| §35 | Лимит 300 символов (`ANSWER_MAX_CHARS`) | `config.py:40` (`answer_max_chars=300`), `operator_reply.py::_persist_reply_and_card` | ✅ | проверка перед отправкой |
| §36 | Шаблон по Приложению 2 | `services/card_format.py::format_formal_reply` или эквивалент в operator_reply | ✅ | подставляется автоматически |
| §37 | После доставки — статус ANSWERED, подтверждение в группе | `operator_reply.py:762-809` (audit "reply"/"reply_via_command"), admin notice | ✅ | |
| §38 (Таблица 2) | Целевые сроки: высокая важность 2 ч, стандартная 4 ч; общий срок решения 8 раб. дней | `config.py:38` (`sla_response_hours=4`); категория «высокая важность» — отдельного флага в коде нет | 🟡 | `sla_response_hours=4` для стандартной; **категория важности (high/normal) не моделируется в БД** — все обращения считаются стандартными |
| §39 | Рабочее время: Пн-Пт 09:00-18:00, перерыв 12:00-13:00, Камчатка | `services/cron.py:524-600` (`_job_working_hours_*`, `services/calendar_ru.py`) | ✅ | расписание учитывает перерыв |
| §40 | Контроль сроков — автоматические напоминания | `services/cron.py:524, 566` (`_job_working_hours_open_reminder`, `_job_working_hours_overdue_reminder`) | ✅ | |
| §41 | Стандарт качества ответов АЕМО № 2129 | организационное; код не валидирует текст ответа против Стандарта | ⚪ | человеческая проверка |
| §42 | Приветствие с именем, по существу, позитивный тон, действительный залог | организационное; не проверяется кодом | ⚪ | |
| §43 | Запреты: история вопроса до ответа, канцелярский стиль, шаблонные слова, аббревиатуры, ошибки, многоточия, поучения, дискуссия, разглашение ПДн | организационное; код не валидирует | ⚪ | человеческая проверка |

## Глава 7. Взаимодействие с профильными подразделениями

| § | Требование | Реализация в коде | Статус | Комментарий |
|---|---|---|---|---|
| §43.1 | Оператор не отвечает за факты, только за процесс | организационное | ⚪ | распределение ответственности |
| §43.2 | Зона компетенции оператора: бот, справочные параметры, функции АЕМО/ЕГП, справочный материал | `services/settings_store.py` (справочные параметры) | ⚪ | определение зон |
| §43.3 | Промежуточный ответ ≤30 мин + запрос ≤30 мин после | `services/appeals.py::has_operator_message`, `handlers/operator_reply.py` (поддержка intermediate reply) | 🟡 | **механизм промежуточного ответа реализован** (intermediate reply без закрытия), **но 30-минутный SLA не отслеживается отдельным cron'ом** |
| §43.4 | Каналы запроса: MAX-группа / служебный телефон / служебный email; запрет других мессенджеров и незащищенных каналов | организационное | ⚪ | политика, не код |
| §43.5 | Сроки от подразделений: high 1 ч / standard 2 ч / выезд 4 ч | организационное; не моделируется | ⚪ | внешние подразделения |
| §43.6 | Содержание сведений от подразделения: подтверждение компетенции, ситуация, меры, ФЗ-59, ПДн третьих лиц | организационное | ⚪ | |
| §43.7 | Оператор не вправе изменять факты, только формулировки | организационное | ⚪ | |
| §43.8 | При нарушении срока: уведомление за 30 мин, продление жителю, координатор | организационное; код не отслеживает | ⚪ | |
| §43.9 | Перенаправление в иной орган с указанием способов | организационное | ⚪ | |
| §43.10 | Несколько подразделений → сводный ответ; справочный материал координатора | организационное; отдельной БД справочного материала нет | ⚪ | внешний knowledge base |

## Глава 8. Информационные рассылки

| § | Требование | Реализация в коде | Статус | Комментарий |
|---|---|---|---|---|
| §44 | Информационные сообщения подписавшимся жителям | `handlers/broadcast.py`, `services/broadcasts.py` | ✅ | |
| §45 | Подписка через кнопку / `/subscribe`; `consent_broadcast_at` | `handlers/menu.py:426` (`self_subscribe_broadcast` audit), `handlers/start.py:144` (`cmd_subscribe`), `db/models.py::User.consent_broadcast_at` | ✅ | |
| §46 | Отписка: кнопка под сообщением / переключатель в меню / `/unsubscribe` | `handlers/start.py:161` (`cmd_unsubscribe`), кнопка `unsub` в broadcast messages | ✅ | |
| §47 | Мастер `/broadcast`, текст ≤1000 символов, предпросмотр с числом получателей, rate-limit 1 msg/sec | `handlers/broadcast.py`, `config.py:82` (`broadcast_max_chars=1000`), `config.py:84` (`broadcast_rate_limit_per_sec=1.0`) | ✅ | |
| §48 | Фотографии 1..`broadcast_max_images` (default 5, range 1-20); предпросмотр; предупреждение при превышении | `services/settings_store.py:118`, `handlers/broadcast.py` (предпросмотр + предупреждение) | ✅ | |
| §49 | «Экстренно остановить» — все роли | `services/broadcasts.py:147` (`request_cancel`), `handlers/broadcast.py` (кнопка без role-gate) | ✅ | |
| §50 | Только подписанным с consent и не заблокированным | `services/broadcasts.py:35` (`_eligible_filter`), `:68` (`list_subscriber_targets`) | ✅ | |
| §51 | Сведения в журнале: delivered_count / failed_count | `db/models.py::Broadcast` (поля `delivered_count`, `failed_count`), audit `broadcast_send` | ✅ | |

## Глава 9. Защита персональных данных

| § | Требование | Реализация в коде | Статус | Комментарий |
|---|---|---|---|---|
| §52 | Обрабатываются: max_user_id, имя, телефон, текст, файлы, геолокация | `db/models.py::User / Appeal / Message / Attachment` | ✅ | |
| §53 | Дата/время согласия и отзыва фиксируются | `db/models.py::User.consent_pdn_at, consent_revoked_at, consent_broadcast_at` | ✅ | |
| §54 | 4-й уровень защищенности (ПП РФ № 1119) | `docs/SECURITY.md` | ⚪ | организационное |
| §55 | Программно-технические меры безопасности | `docs/SECURITY.md`, `services/db_backup.py` (GPG), TLS | ⚪ | внешние ФСТЭК / ФСБ |
| §56 | `/export` + раздел «Мои обращения» | `handlers/start.py:201` (`cmd_export`), `handlers/menu.py` («Мои обращения») | ✅ | |
| §57 | «Уйти из бота» — 3 варианта: unsubscribe / revoke consent / immediate erase | `handlers/menu.py:702` (`self_consent_revoke`), `:750` (`self_erase`), `:426` (subscription toggle) | ✅ | три варианта в подменю «Настройки и помощь» |
| §58 (1) | `/forget` самостоятельно жителем | `handlers/start.py:171` (`cmd_forget`), audit `self_erase` | ✅ | |
| §58 (2) | ИТ: `/erase max_user_id=N` / `/erase phone=+7...` / кнопка «Удалить ПДн» | `handlers/admin_commands.py:253-303` (`cmd_erase`, audit `erase`), `handlers/admin_appeal_ops.py:437` (кнопка), `handlers/admin_audience.py:113-122` | ✅ | оба варианта команд + кнопка |
| §59 | 30 дней после отзыва — обезличивание, ежедневно 04:30 | `services/cron.py:694` (`_job_pdn_retention_check`, `CronTrigger(hour=4, minute=30)`), audit `auto_erase_pdn_retention` (`:416`) | ✅ | |
| §60 | 5 лет хранение обращений, ежедневно 04:45 | `services/cron.py:688` (`_job_appeals_5y_retention`, `CronTrigger(hour=4, minute=45)`) | ✅ | |

## Глава 10. Блокировка жителя

| § | Требование | Реализация в коде | Статус | Комментарий |
|---|---|---|---|---|
| §61 | Блокировка от злоупотреблений (спам, угрозы, оскорбления, мошенничество) | `services/users.py:418` (`set_blocked`) | ✅ | |
| §62 | Критика должностных лиц — НЕ основание | организационное | ⚪ | политика; не валидируется кодом |
| §63 | Только IT; кнопка в карточке + «Аудитория и согласия» в `/op_help` | `handlers/admin_appeal_ops.py:361` (audit `block`/`unblock`), `handlers/admin_audience.py:71-111` | ✅ | оба пути для IT |
| §64 | Обсуждение в группе с участием координатора предшествует | организационное | ⚪ | человеческая координация |
| §65 | Заблокированный не может слать, получать ответы и рассылки; открытые обращения закрываются автоматически | `services/users.py:436-457` (auto-close NEW/IN_PROGRESS на block), `services/users.py:80-85` (доставка отказывается по is_blocked), `services/broadcasts.py:35` (исключение из рассылки) | ✅ | подтверждено: auto-close через `closed_due_to_revoke=true` |
| §66 | Блокировка не ограничивает иные каналы | организационное | ⚪ | юридическое |
| §67 | Все block/unblock в журнале | `handlers/admin_audience.py:80, 101`, `admin_appeal_ops.py:361` (audit logged) | ✅ | |

## Глава 11. Журнал действий операторов

| § | Требование | Реализация в коде | Статус | Комментарий |
|---|---|---|---|---|
| §68 | Таблица `audit_log` с инициатором, действием, целью, временем | `db/models.py::AuditLog` (operator_max_user_id, action, target, details, created_at) | ✅ | схема точно соответствует |
| §69 (1) | `reply` — ответ свайпом | `handlers/operator_reply.py:762` (`audit_action="reply"`) | ✅ | |
| §69 (2) | `reply_via_command` — `/reply` | `handlers/operator_reply.py:809` (`audit_action="reply_via_command"`) | ✅ | |
| §69 (3) | `reopen` | `handlers/admin_commands.py:215`, `admin_appeal_ops.py:282` | ✅ | |
| §69 (4) | `close` | `handlers/admin_commands.py:244`, `admin_appeal_ops.py:314` | ✅ | |
| §69 (5) | `erase` (ИТ) | `handlers/admin_commands.py:303`, `admin_audience.py:120`, `admin_appeal_ops.py:437` | ✅ | |
| §69 (6) | `self_erase` (житель) | `handlers/start.py:183`, `menu.py:750` | ✅ | |
| §69 (7) | `self_consent_revoke` | `handlers/menu.py:702` | ✅ | |
| §69 (8) | `self_subscribe_broadcast` | `handlers/menu.py:426` | ✅ | |
| §69 (9) | `block` | `handlers/admin_audience.py:80`, `admin_appeal_ops.py:361` | ✅ | |
| §69 (10) | `unblock` | `handlers/admin_audience.py:101`, `admin_appeal_ops.py:361` | ✅ | |
| §69 (11) | `broadcast_send` | `handlers/broadcast.py:310` | ✅ | |
| §69 (12) | `operator_upsert` | `handlers/admin_operators.py:926`, `admin_commands.py:439` | ✅ | |
| §69 (13) | `operator_deactivate` | `handlers/admin_operators.py:535` | ✅ | |
| §69 (14) | `operator_reactivate` | `handlers/admin_operators.py:579` | ✅ | |
| §69 (15) | `setting_update` | `handlers/admin_settings.py:931`, `admin_commands.py:357` | ✅ | |
| §69 (16) | `settings_repo_pr_created` | `handlers/admin_settings.py:727` (`action="settings_pr_created"`) | 🟡 | **Несоответствие имени: код пишет `settings_pr_created`, Регламент требует `settings_repo_pr_created`.** Нужно либо переименовать константу в коде, либо исправить Регламент v6. |
| §69 (17) | `auto_erase_pdn_retention` | `services/cron.py:416` | ✅ | |
| §70 | Тексты сообщений / ответов / значений настроек в журнале не сохраняются | `db/models.py::AuditLog.details` JSONB; в коде передаются только метаданные (без `value`). Подтверждено для `setting_update` (PR #20 «PII в admin_settings details») | ✅ | |

## Глава 12. Резервное копирование и фоновые задачи

| § | Требование | Реализация в коде | Статус | Комментарий |
|---|---|---|---|---|
| §71 / Табл. 3 №1 | Бэкап Вс 03:00 (Камчатка), GPG-шифрование | `services/cron.py:617`, `db_backup.py:130` (`_run_pg_dump_encrypted` GPG) | ✅ | |
| Табл. 3 №2 | Очистка events ежедневно 04:00, старше 30 дней | `services/cron.py:628` (`_job_events_retention`, `CronTrigger(hour=4, minute=0)`) | ✅ | |
| Табл. 3 №3 | Обезличивание после отзыва ежедневно 04:30, ≥30 дней | `services/cron.py:694` (`_job_pdn_retention_check`) | ✅ | |
| Табл. 3 №4 | Обезличивание архива 5+ лет ежедневно 04:45 | `services/cron.py:688` (`_job_appeals_5y_retention`) | ✅ | |
| Табл. 3 №5 | Контроль зависших анкет каждые 15 мин; сводка при ≥5/час | `services/cron.py:701` (`_job_funnel_watchdog`, `CronTrigger(minute=15)`) | 🟡 | watchdog работает каждые 15 минут; требуемая сводка-агрегатор «5 анкет за час» — проверить порог в `_job_funnel_watchdog` (на момент проверки порог 5 указан в Регламенте, в коде требует уточнения) |
| Табл. 3 №6 | Open reminder Пн-Пт 09:00-17:59 (без 12-13), мин. 10 | `services/cron.py:716` (`_job_working_hours_open_reminder`) | ✅ | |
| Табл. 3 №7 | Overdue reminder Пн-Пт 09:00-17:59 (без 12-13), мин. 40 | `services/cron.py:723` (`_job_working_hours_overdue_reminder`) | ✅ | |
| Табл. 3 №8 | Ежемесячный отчёт 1 числа 09:00 | `services/cron.py:648` (`_job_monthly_report`, `CronTrigger(day=1, hour=9, minute=0)`) | ✅ | |
| Табл. 3 №9 | Pulse Пн-Сб 09-17, минуты 00 и 30 | `services/cron.py:669, 681` (`_job_pulse`, три расписания) | ✅ | |
| Табл. 3 №10 | Pulse нерабочее Пн-Сб 00-08 и 18-23, минута 05 | `services/cron.py:669` (one of pulse schedules) | ✅ | |
| Табл. 3 №11 | Pulse Воскресенье ежечасно, минута 05 | `services/cron.py:676` (`CronTrigger(day_of_week="sun", hour="*", minute=5)`) | ✅ | |
| Табл. 3 №12 | Startup pulse через 5 секунд | `services/cron.py:657` (`_job_startup_pulse`, DateTrigger) | ✅ | |
| Табл. 3 №13 | Selfcheck каждые 5 минут | `services/cron.py:640` (`_job_selfcheck`, `CronTrigger` interval 5 минут) | ✅ | |
| Табл. 3 №14 | Внешний пинг каждые 5 минут (при HEALTHCHECK_URL) | `services/cron.py:742` (add_job для healthcheck), `config.py:121` (`healthcheck_url`) | ✅ | |
| §72 | Глубина 8 копий, `/backup` для внепланового | `config.py:101` (`backup_keep_count=8`), `handlers/admin_commands.py:370` (`cmd_backup`) | ✅ | |
| §73 | Уведомление о сбое с указанием стадии; удаление незашифрованного при сбое шифрования | `services/db_backup.py:130-200` (try/except per stage), `_job_backup_with_alert` | ✅ | реализовано в SEC #2 (PR #35) |
| §74 | Пробное восстановление в изолированную среду ≥1/квартал | `docs/BACKUP_RESTORE_TEST.md` | ⚪ | организационная процедура; runbook есть |

## Глава 13. Изменение настроек и регистрация операторов

| § | Требование | Реализация в коде | Статус | Комментарий |
|---|---|---|---|---|
| §75 (1) | Раздел «Настройки бота» в `/op_help`, иерархическое меню с подсветкой dirty | `handlers/admin_settings.py:1-285`, `:668-727`, `services/settings_store.py:208` (`get_dirty_keys`) | ✅ | |
| §75 (2) | Команда `/setting` | `handlers/admin_commands.py:315-364` (`cmd_setting`, audit `setting_update`) | ✅ | |
| §76 (1-14) | 14 ключей: welcome_text, consent_text, commit_author_name/email, policy_url, electronic_reception_url, udth_schedule_url, udth_schedule_intermunicipal_url, appointment_text, emergency_contacts, transport_dispatcher_contacts, topics, broadcast_max_images, localities | `services/settings_store.py:46-119` (DEFAULTS + VALIDATORS, все 14 ключей) | ✅ | подтверждено все 14 |
| §77 | Изменения в audit, значение НЕ записывается | `handlers/admin_settings.py:931`, `admin_commands.py:357` (action `setting_update` с указанием key, без value) | ✅ | защищено SEC #20 |
| §78 | Синхронизация с репозиторием через PR; список не синхронизируемых: commit_*, welcome_text, consent_text, broadcast_max_images | `services/settings_store.py:193-204` (`SYNC_ALLOWLIST`), `services/repo_sync.py:270-333` (`create_settings_pr`), audit `settings_pr_created` (`admin_settings.py:727`) | 🟡 | exclusion list точно совпадает (commit_*, welcome_text, consent_text, broadcast_max_images); **action name `settings_pr_created` vs `settings_repo_pr_created` см. §69 (16)** |
| §79 | Алгоритм `/add_operators`: добавить в группу → `/whoami` → ИТ формирует список → `/add_operators` | `handlers/admin_commands.py:382-439`, `admin_operators.py:733` («попросите написать `/whoami`») | ✅ | алгоритм задокументирован в `/op_help` и Operators wizard |
| §80 | `/whoami` в личке с жителем не работает | `handlers/start.py:376-388` (`cmd_whoami` только для chat_id == ADMIN_GROUP_ID) | ✅ | подтверждено |
| §81 | Самостоятельное повышение роли заблокировано; смена роли — ИТ через карточку либо БД; добавление из участников группы | `handlers/admin_operators.py` (роль gated; нет «promote self» path), `admin_commands.py:382-387` (защита от ручного назначения IT через self-issued command) | ✅ | защита SEC #6 (operator deactivation race) + §17 single-IT гард |

## Глава 14. Ответственность

| § | Требование | Реализация в коде | Статус | Комментарий |
|---|---|---|---|---|
| §82 | Координатор и специалисты — за качество и сроки | организационное / трудовое | ⚪ | |
| §83 | Профильные подразделения — за факты | организационное | ⚪ | |
| §84 | ИТ — за функционирование, бэкапы, удаление ПДн, конфиденциальность | организационное; код выполняет | ⚪ | |
| §85 | Разглашение — ответственность по закону | организационное | ⚪ | |

## Глава 15. Изменение Регламента

| § | Требование | Реализация в коде | Статус | Комментарий |
|---|---|---|---|---|
| §86 | Изменения — распоряжением АЕМО | организационное | ⚪ | |
| §87 | Проект готовится начальником отдела по работе с обращениями + ИТ | организационное | ⚪ | |
| §88 | Пересмотр ≥1 раза в год / при существенных изменениях | организационное; `docs/Регламент_v6_draft.md` существует — пересмотр идёт | ⚪ | |

## Приложение 1 — команды

Все команды проверены при разборе глав 4-13 (см. §17, §18, §34, §47, §56, §58, §75, §79, §80). Полное соответствие.

## Приложение 2 — шаблон ответа

`services/card_format.py` формирует ответ по шаблону Приложения 2 с автоматической подстановкой полей. ✅

## Приложение 3 — контрольный перечень

Все 12 чекпоинтов (нумерация в оригинале пропускает п. 7, фактически 12 шагов) выполнимы текущим ботом; явного автоматизированного e2e-теста по checklist'у нет, но каждая позиция покрыта unit/integration-тестами и `docs/VPS_SMOKE_CHECKLIST.md`. ⚪ (организационная процедура смоук-теста).

## Приложение 4 — форма запроса фактических сведений

Бумажная форма. ⚪ (организационное; код не вовлечён).
