# Инвентаризация реализации — 30.10.2026

Статический снимок исходников текущей ветки. Генератор читает все Python-модули приложения, регистрации, таблицу маршрутов, экспортируемые тексты, схему настроек, seed и пример окружения. Чтение/хеш файла означает охват инвентаризации, а не доказательство семантической корректности каждой строки.

Воспроизведение: `python scripts/inventory_implementation.py`. После изменения исходников обновить этот файл. Сгенерированные `docs/site/index.html` и `aemr-bot-index.md` не являются независимыми источниками истины.

## Проверенные связи возможностей

| Возможность | Реализация |
| --- | --- |
| Черновик обращения/дополнения, 20000/20, явная отправка | `handlers/appeal_funnel.py`, `services/appeals.py`, `ui/citizen_keyboards.py` |
| Ответ 3000, предпросмотр, роль, подтверждение/unknown | `handlers/operator_draft.py`, `handlers/operator_reply.py`, `services/operator_drafts.py`, `services/operator_reply_attempts.py` |
| Общая очередь, отдельные подробности, IT-восстановление | `services/queue_panel.py`, `handlers/admin_panel.py`, `handlers/admin_callback_dispatch.py` |
| Расписание и снимки периодов SLA | `services/work_schedule.py`, `services/sla.py`, `services/appeals.py`, `handlers/admin_settings.py` |
| Полная история, Word, XLSX | `handlers/admin_callback_dispatch.py`, `services/appeal_word.py`, `services/complete_stats.py`, `services/stats.py` |
| Раздельные согласия, доказательства показанного текста | `services/consent_history.py`, `services/users.py`, `handlers/start.py` |
| Удаление и восстановление, неизвестные внешние копии | `services/data_erasure.py`, `services/db_backup.py`, `scripts/replay_erasure_tombstones.py` |
| Тихий режим, критические события, инциденты баз угроз | `services/admin_bus.py`, `services/quiet_hours.py`, `services/alert_state.py`, `services/threat_intel.py` |

## Границы доказательства

AST не раскрывает все вычисляемые payload, getattr, monkey-patch, косвенную регистрацию или реальную последовательность событий. Команды ниже — литеральные вызовы фильтра; фактические доступы зависят от чата, активной роли, состояния и повторных проверок. Тесты контрактов маршрутов и сквозные PostgreSQL-тесты дополняют этот снимок. Получение/доставка сообщений реальным MAX, клиентские ограничения, права группы, ручная обработка внешних копий, journald, неизвестные старые копии, восстановление резервной копии и закрытый рабочий сервер требуют ручной проверки. Нет заявления о 100% охвате или exactly-once внешних действий.

## Команды и регистрация

- `Command('add_operators')` — `bot/aemr_bot/handlers/admin_commands.py:394`
- `Command('backup')` — `bot/aemr_bot/handlers/admin_commands.py:382`
- `Command('broadcast')` — `bot/aemr_bot/handlers/broadcast.py:920`
- `Command('cancel')` — `bot/aemr_bot/handlers/start.py:753`
- `Command('close')` — `bot/aemr_bot/handlers/admin_commands.py:233`
- `Command('diag')` — `bot/aemr_bot/handlers/admin_commands.py:376`
- `Command('erase')` — `bot/aemr_bot/handlers/admin_commands.py:259`
- `Command('export')` — `bot/aemr_bot/handlers/start.py:763`
- `Command('find_resident')` — `bot/aemr_bot/handlers/admin_resident_search.py:238`
- `Command('forget')` — `bot/aemr_bot/handlers/start.py:746`
- `Command('help')` — `bot/aemr_bot/handlers/start.py:723`
- `Command('menu')` — `bot/aemr_bot/handlers/start.py:732`
- `Command('op_help')` — `bot/aemr_bot/handlers/admin_commands.py:388`
- `Command('open_tickets')` — `bot/aemr_bot/handlers/admin_commands.py:140`
- `Command('policy')` — `bot/aemr_bot/handlers/start.py:770`
- `Command('reopen')` — `bot/aemr_bot/handlers/admin_commands.py:203`
- `Command('reply')` — `bot/aemr_bot/handlers/admin_commands.py:168`
- `Command('rules')` — `bot/aemr_bot/handlers/start.py:777`
- `Command('setting')` — `bot/aemr_bot/handlers/admin_commands.py:321`
- `Command('start')` — `bot/aemr_bot/handlers/start.py:714`
- `Command('stats')` — `bot/aemr_bot/handlers/admin_commands.py:153`
- `Command('subscribe')` — `bot/aemr_bot/handlers/start.py:784`
- `Command('unsubscribe')` — `bot/aemr_bot/handlers/start.py:791`
- `Command('whoami')` — `bot/aemr_bot/handlers/start.py:798`

Регистрационные вызовы (включая косвенные):

- `admin_commands.register` — `bot/aemr_bot/handlers/__init__.py:155`
- `admin_resident_search.register` — `bot/aemr_bot/handlers/__init__.py:156`
- `appeal.register` — `bot/aemr_bot/handlers/__init__.py:159`
- `broadcast.register` — `bot/aemr_bot/handlers/__init__.py:157`
- `dp.bot_started` — `bot/aemr_bot/handlers/start.py:667`
- `dp.bot_stopped` — `bot/aemr_bot/handlers/start.py:673`
- `dp.handle` — `bot/aemr_bot/services/inbox.py:181`
- `dp.message_callback` — `bot/aemr_bot/handlers/appeal.py:764`
- `dp.message_created` — `bot/aemr_bot/handlers/admin_commands.py:140`
- `dp.message_created` — `bot/aemr_bot/handlers/admin_commands.py:153`
- `dp.message_created` — `bot/aemr_bot/handlers/admin_commands.py:168`
- `dp.message_created` — `bot/aemr_bot/handlers/admin_commands.py:203`
- `dp.message_created` — `bot/aemr_bot/handlers/admin_commands.py:233`
- `dp.message_created` — `bot/aemr_bot/handlers/admin_commands.py:259`
- `dp.message_created` — `bot/aemr_bot/handlers/admin_commands.py:321`
- `dp.message_created` — `bot/aemr_bot/handlers/admin_commands.py:376`
- `dp.message_created` — `bot/aemr_bot/handlers/admin_commands.py:382`
- `dp.message_created` — `bot/aemr_bot/handlers/admin_commands.py:388`
- `dp.message_created` — `bot/aemr_bot/handlers/admin_commands.py:394`
- `dp.message_created` — `bot/aemr_bot/handlers/admin_resident_search.py:238`
- `dp.message_created` — `bot/aemr_bot/handlers/appeal.py:816`
- `dp.message_created` — `bot/aemr_bot/handlers/broadcast.py:920`
- `dp.message_created` — `bot/aemr_bot/handlers/start.py:714`
- `dp.message_created` — `bot/aemr_bot/handlers/start.py:723`
- `dp.message_created` — `bot/aemr_bot/handlers/start.py:732`
- `dp.message_created` — `bot/aemr_bot/handlers/start.py:746`
- `dp.message_created` — `bot/aemr_bot/handlers/start.py:753`
- `dp.message_created` — `bot/aemr_bot/handlers/start.py:763`
- `dp.message_created` — `bot/aemr_bot/handlers/start.py:770`
- `dp.message_created` — `bot/aemr_bot/handlers/start.py:777`
- `dp.message_created` — `bot/aemr_bot/handlers/start.py:784`
- `dp.message_created` — `bot/aemr_bot/handlers/start.py:791`
- `dp.message_created` — `bot/aemr_bot/handlers/start.py:798`
- `dp.start_polling` — `bot/aemr_bot/main.py:893`
- `dp.startup` — `bot/aemr_bot/main.py:887`
- `start.register` — `bot/aemr_bot/handlers/__init__.py:154`

## Маршруты callback

- `CallbackRoute('menu:main', CallbackGroup.CITIZEN_FLOW, False, 'главное меню жителя')` — `bot/aemr_bot/handlers/callback_router.py:33`
- `CallbackRoute('menu:new_appeal', CallbackGroup.CITIZEN_FLOW, False, 'новое обращение')` — `bot/aemr_bot/handlers/callback_router.py:34`
- `CallbackRoute('menu:my_appeals', CallbackGroup.CITIZEN_FLOW, False, 'мои обращения')` — `bot/aemr_bot/handlers/callback_router.py:35`
- `CallbackRoute('menu:useful_info', CallbackGroup.CITIZEN_FLOW, False, 'полезная информация')` — `bot/aemr_bot/handlers/callback_router.py:36`
- `CallbackRoute('menu:appointment', CallbackGroup.CITIZEN_FLOW, False, 'запись на приём')` — `bot/aemr_bot/handlers/callback_router.py:37`
- `CallbackRoute('menu:settings', CallbackGroup.CITIZEN_FLOW, False, 'настройки жителя')` — `bot/aemr_bot/handlers/callback_router.py:38`
- `CallbackRoute('menu:security', CallbackGroup.CITIZEN_FLOW, False, 'защита от мошенников')` — `bot/aemr_bot/handlers/callback_router.py:39`
- `CallbackRoute('consent:yes', CallbackGroup.CITIZEN_FLOW, False, 'согласие на ПДн')` — `bot/aemr_bot/handlers/callback_router.py:41`
- `CallbackRoute('consent:no', CallbackGroup.CITIZEN_FLOW, False, 'отказ от ПДн')` — `bot/aemr_bot/handlers/callback_router.py:42`
- `CallbackRoute('cancel', CallbackGroup.CITIZEN_FLOW, False, 'отмена воронки')` — `bot/aemr_bot/handlers/callback_router.py:43`
- `CallbackRoute('addr:reuse', CallbackGroup.CITIZEN_FLOW, False, 'использовать прошлый адрес')` — `bot/aemr_bot/handlers/callback_router.py:44`
- `CallbackRoute('addr:new', CallbackGroup.CITIZEN_FLOW, False, 'ввести новый адрес')` — `bot/aemr_bot/handlers/callback_router.py:45`
- `CallbackRoute('geo:confirm', CallbackGroup.GEO_FLOW, False, 'подтвердить гео-адрес')` — `bot/aemr_bot/handlers/callback_router.py:46`
- `CallbackRoute('geo:edit_address', CallbackGroup.GEO_FLOW, False, 'исправить адрес')` — `bot/aemr_bot/handlers/callback_router.py:47`
- `CallbackRoute('geo:other_locality', CallbackGroup.GEO_FLOW, False, 'выбрать другой пункт')` — `bot/aemr_bot/handlers/callback_router.py:48`
- `CallbackRoute('appeal:submit', CallbackGroup.CITIZEN_FLOW, False, 'устаревшая кнопка отправки')` — `bot/aemr_bot/handlers/callback_router.py:49`
- `CallbackRoute('info:emergency', CallbackGroup.CITIZEN_FLOW, False, 'экстренные контакты')` — `bot/aemr_bot/handlers/callback_router.py:51`
- `CallbackRoute('info:dispatchers', CallbackGroup.CITIZEN_FLOW, False, 'диспетчеры транспорта')` — `bot/aemr_bot/handlers/callback_router.py:52`
- `CallbackRoute('info:subscribe_on', CallbackGroup.CITIZEN_FLOW, False, 'подписаться на рассылку')` — `bot/aemr_bot/handlers/callback_router.py:53`
- `CallbackRoute('info:subscribe_off', CallbackGroup.CITIZEN_FLOW, False, 'отписаться от рассылки')` — `bot/aemr_bot/handlers/callback_router.py:54`
- `CallbackRoute('subscribe:confirm', CallbackGroup.CITIZEN_FLOW, False, 'мини-консент подписки')` — `bot/aemr_bot/handlers/callback_router.py:55`
- `CallbackRoute('settings:help', CallbackGroup.CITIZEN_FLOW, False, 'справка по боту')` — `bot/aemr_bot/handlers/callback_router.py:57`
- `CallbackRoute('settings:rules', CallbackGroup.CITIZEN_FLOW, False, 'правила приёма обращений')` — `bot/aemr_bot/handlers/callback_router.py:58`
- `CallbackRoute('settings:policy', CallbackGroup.CITIZEN_FLOW, False, 'политика ПДн')` — `bot/aemr_bot/handlers/callback_router.py:59`
- `CallbackRoute('settings:goodbye', CallbackGroup.CITIZEN_FLOW, False, 'прощание (отзыв/удаление)')` — `bot/aemr_bot/handlers/callback_router.py:60`
- `CallbackRoute('goodbye:unsub', CallbackGroup.CITIZEN_FLOW, False, 'отписаться от рассылки')` — `bot/aemr_bot/handlers/callback_router.py:61`
- `CallbackRoute('goodbye:revoke_ask', CallbackGroup.CITIZEN_FLOW, False, 'запрос подтверждения отзыва согласия')` — `bot/aemr_bot/handlers/callback_router.py:62`
- `CallbackRoute('goodbye:revoke_yes', CallbackGroup.CITIZEN_FLOW, False, 'подтверждение отзыва согласия')` — `bot/aemr_bot/handlers/callback_router.py:63`
- `CallbackRoute('goodbye:erase_ask', CallbackGroup.CITIZEN_FLOW, False, 'запрос подтверждения удаления ПДн')` — `bot/aemr_bot/handlers/callback_router.py:64`
- `CallbackRoute('goodbye:erase_yes', CallbackGroup.CITIZEN_FLOW, False, 'подтверждение удаления ПДн')` — `bot/aemr_bot/handlers/callback_router.py:65`
- `CallbackRoute('broadcast:unsubscribe', CallbackGroup.CITIZEN_FLOW, False, 'отписаться через broadcast-сообщение')` — `bot/aemr_bot/handlers/callback_router.py:67`
- `CallbackRoute('broadcast:confirm', CallbackGroup.BROADCAST_ADMIN, True, 'подтвердить рассылку')` — `bot/aemr_bot/handlers/callback_router.py:69`
- `CallbackRoute('broadcast:abort', CallbackGroup.BROADCAST_ADMIN, True, 'отменить мастер рассылки')` — `bot/aemr_bot/handlers/callback_router.py:70`
- `CallbackRoute('broadcast:edit', CallbackGroup.BROADCAST_ADMIN, True, 'изменить текст рассылки')` — `bot/aemr_bot/handlers/callback_router.py:71`
- `CallbackRoute('op:menu', CallbackGroup.OPERATOR_ADMIN, True, 'операторское меню')` — `bot/aemr_bot/handlers/callback_router.py:72`
- `CallbackRoute('op:stats_menu', CallbackGroup.OPERATOR_ADMIN, True, 'меню статистики')` — `bot/aemr_bot/handlers/callback_router.py:73`
- `CallbackRoute('op:stats_today', CallbackGroup.OPERATOR_ADMIN, True, 'статистика за сегодня')` — `bot/aemr_bot/handlers/callback_router.py:74`
- `CallbackRoute('op:stats_week', CallbackGroup.OPERATOR_ADMIN, True, 'статистика за неделю')` — `bot/aemr_bot/handlers/callback_router.py:75`
- `CallbackRoute('op:stats_month', CallbackGroup.OPERATOR_ADMIN, True, 'статистика за месяц')` — `bot/aemr_bot/handlers/callback_router.py:76`
- `CallbackRoute('op:stats_quarter', CallbackGroup.OPERATOR_ADMIN, True, 'статистика за квартал')` — `bot/aemr_bot/handlers/callback_router.py:77`
- `CallbackRoute('op:stats_half_year', CallbackGroup.OPERATOR_ADMIN, True, 'статистика за полугодие')` — `bot/aemr_bot/handlers/callback_router.py:78`
- `CallbackRoute('op:stats_year', CallbackGroup.OPERATOR_ADMIN, True, 'статистика за год')` — `bot/aemr_bot/handlers/callback_router.py:79`
- `CallbackRoute('op:stats_all', CallbackGroup.OPERATOR_ADMIN, True, 'статистика за всё время')` — `bot/aemr_bot/handlers/callback_router.py:80`
- `CallbackRoute('op:queue_view', CallbackGroup.OPERATOR_ADMIN, True, 'открыть очередь в отдельном экране')` — `bot/aemr_bot/handlers/callback_router.py:81`
- `CallbackRoute('op:open_tickets', CallbackGroup.OPERATOR_ADMIN, True, 'открытые обращения')` — `bot/aemr_bot/handlers/callback_router.py:82`
- `CallbackRoute('op:diag', CallbackGroup.OPERATOR_ADMIN, True, 'диагностика')` — `bot/aemr_bot/handlers/callback_router.py:83`
- `CallbackRoute('op:backup', CallbackGroup.OPERATOR_ADMIN, True, 'бэкап')` — `bot/aemr_bot/handlers/callback_router.py:84`
- `CallbackRoute('op:broadcast', CallbackGroup.OPERATOR_ADMIN, True, 'мастер рассылки')` — `bot/aemr_bot/handlers/callback_router.py:85`
- `CallbackRoute('op:broadcast_list', CallbackGroup.OPERATOR_ADMIN, True, 'история рассылок')` — `bot/aemr_bot/handlers/callback_router.py:86`
- `CallbackRoute('op:operators', CallbackGroup.OPERATOR_ADMIN, True, 'операторы')` — `bot/aemr_bot/handlers/callback_router.py:87`
- `CallbackRoute('op:settings', CallbackGroup.OPERATOR_ADMIN, True, 'настройки')` — `bot/aemr_bot/handlers/callback_router.py:88`
- `CallbackRoute('op:help_full', CallbackGroup.OPERATOR_ADMIN, True, 'полная памятка оператора')` — `bot/aemr_bot/handlers/callback_router.py:89`
- `CallbackRoute('op:help_security', CallbackGroup.OPERATOR_ADMIN, True, 'памятка: безопасность и антифишинг')` — `bot/aemr_bot/handlers/callback_router.py:90`
- `CallbackRoute('op:audience', CallbackGroup.OPERATOR_ADMIN, True, 'аудитория')` — `bot/aemr_bot/handlers/callback_router.py:91`
- `CallbackRoute('op:reply_cancel', CallbackGroup.OPERATOR_ADMIN, True, 'отмена ответа')` — `bot/aemr_bot/handlers/callback_router.py:92`
- `CallbackRoute('op:replydraft:', CallbackGroup.OPERATOR_ADMIN, True, 'многочастный черновик ответа')` — `bot/aemr_bot/handlers/callback_router.py:96`
- `CallbackRoute('op:reply_cancel:', CallbackGroup.OPERATOR_ADMIN, True, 'отмена выбранного ответа')` — `bot/aemr_bot/handlers/callback_router.py:97`
- `CallbackRoute('locality:', CallbackGroup.CITIZEN_FLOW, False, 'выбор населённого пункта')` — `bot/aemr_bot/handlers/callback_router.py:99`
- `CallbackRoute('topic:', CallbackGroup.CITIZEN_FLOW, False, 'выбор темы')` — `bot/aemr_bot/handlers/callback_router.py:100`
- `CallbackRoute('appeal:show:', CallbackGroup.CITIZEN_FLOW, False, 'показать карточку обращения жителя')` — `bot/aemr_bot/handlers/callback_router.py:102`
- `CallbackRoute('appeal:followup:', CallbackGroup.CITIZEN_FLOW, False, 'дополнить обращение')` — `bot/aemr_bot/handlers/callback_router.py:103`
- `CallbackRoute('appeal:repeat:', CallbackGroup.CITIZEN_FLOW, False, 'подать похожее обращение')` — `bot/aemr_bot/handlers/callback_router.py:104`
- `CallbackRoute('appeal:atts:', CallbackGroup.CITIZEN_FLOW, False, 'посмотреть свои вложения')` — `bot/aemr_bot/handlers/callback_router.py:105`
- `CallbackRoute('appeal:history:', CallbackGroup.CITIZEN_FLOW, False, 'история обращения жителя')` — `bot/aemr_bot/handlers/callback_router.py:106`
- `CallbackRoute('appeal:record_atts:', CallbackGroup.CITIZEN_FLOW, False, 'вложения записи обращения')` — `bot/aemr_bot/handlers/callback_router.py:107`
- `CallbackRoute('appeals:page:', CallbackGroup.CITIZEN_FLOW, False, 'пагинация списка обращений')` — `bot/aemr_bot/handlers/callback_router.py:108`
- `CallbackRoute('draft:', CallbackGroup.CITIZEN_FLOW, False, 'черновик обращения')` — `bot/aemr_bot/handlers/callback_router.py:109`
- `CallbackRoute('broadcast:stop:', CallbackGroup.BROADCAST_ADMIN, True, 'остановить рассылку')` — `bot/aemr_bot/handlers/callback_router.py:111`
- `CallbackRoute('broadcast:cancel-cooldown:', CallbackGroup.BROADCAST_ADMIN, True, 'отменить рассылку до отправки')` — `bot/aemr_bot/handlers/callback_router.py:112`
- `CallbackRoute('op:aud:', CallbackGroup.OPERATOR_ADMIN, True, 'действие с аудиторией')` — `bot/aemr_bot/handlers/callback_router.py:113`
- `CallbackRoute('op:reply:', CallbackGroup.OPERATOR_ADMIN, True, 'ответ по обращению (финальный)')` — `bot/aemr_bot/handlers/callback_router.py:114`
- `CallbackRoute('op:replyint:', CallbackGroup.OPERATOR_ADMIN, True, 'промежуточный ответ')` — `bot/aemr_bot/handlers/callback_router.py:115`
- `CallbackRoute('op:reopen:', CallbackGroup.OPERATOR_ADMIN, True, 'вернуть в работу')` — `bot/aemr_bot/handlers/callback_router.py:116`
- `CallbackRoute('op:close:', CallbackGroup.OPERATOR_ADMIN, True, 'закрыть обращение')` — `bot/aemr_bot/handlers/callback_router.py:117`
- `CallbackRoute('op:erase:', CallbackGroup.OPERATOR_ADMIN, True, 'стереть ПДн по обращению')` — `bot/aemr_bot/handlers/callback_router.py:118`
- `CallbackRoute('op:block:', CallbackGroup.OPERATOR_ADMIN, True, 'заблокировать жителя')` — `bot/aemr_bot/handlers/callback_router.py:119`
- `CallbackRoute('op:unblock:', CallbackGroup.OPERATOR_ADMIN, True, 'разблокировать жителя')` — `bot/aemr_bot/handlers/callback_router.py:120`
- `CallbackRoute('op:atts:', CallbackGroup.OPERATOR_ADMIN, True, 'показать вложения обращения')` — `bot/aemr_bot/handlers/callback_router.py:121`
- `CallbackRoute('op:queue_recreate:', CallbackGroup.OPERATOR_ADMIN, True, 'восстановить панель после проверки MAX')` — `bot/aemr_bot/handlers/callback_router.py:122`
- `CallbackRoute('op:open_card:', CallbackGroup.OPERATOR_ADMIN, True, 'открыть полную карточку с историей')` — `bot/aemr_bot/handlers/callback_router.py:123`
- `CallbackRoute('op:open_page:', CallbackGroup.OPERATOR_ADMIN, True, 'страница открытых обращений')` — `bot/aemr_bot/handlers/callback_router.py:124`
- `CallbackRoute('op:delivery_issues:', CallbackGroup.OPERATOR_ADMIN, True, 'неподтверждённая доставка дополнений')` — `bot/aemr_bot/handlers/callback_router.py:125`
- `CallbackRoute('op:reply_issues:', CallbackGroup.OPERATOR_ADMIN, True, 'неподтверждённые ответы')` — `bot/aemr_bot/handlers/callback_router.py:126`
- `CallbackRoute('op:reply_attempt:', CallbackGroup.OPERATOR_ADMIN, True, 'сохранённый ответ')` — `bot/aemr_bot/handlers/callback_router.py:127`
- `CallbackRoute('op:reply_resolve:', CallbackGroup.OPERATOR_ADMIN, True, 'проверить исход ответа')` — `bot/aemr_bot/handlers/callback_router.py:128`
- `CallbackRoute('op:reply_confirm:', CallbackGroup.OPERATOR_ADMIN, True, 'подтвердить исход ответа')` — `bot/aemr_bot/handlers/callback_router.py:129`
- `CallbackRoute('op:reply_attempt_atts:', CallbackGroup.OPERATOR_ADMIN, True, 'вложения сохранённого ответа')` — `bot/aemr_bot/handlers/callback_router.py:130`
- `CallbackRoute('op:history:', CallbackGroup.OPERATOR_ADMIN, True, 'история обращения оператора')` — `bot/aemr_bot/handlers/callback_router.py:131`
- `CallbackRoute('op:word:', CallbackGroup.OPERATOR_ADMIN, True, 'история обращения в Word')` — `bot/aemr_bot/handlers/callback_router.py:132`
- `CallbackRoute('op:consent:', CallbackGroup.OPERATOR_ADMIN, True, 'редакции согласия жителя')` — `bot/aemr_bot/handlers/callback_router.py:133`
- `CallbackRoute('op:record_atts:', CallbackGroup.OPERATOR_ADMIN, True, 'вложения записи обращения')` — `bot/aemr_bot/handlers/callback_router.py:134`
- `CallbackRoute('op:opadd:', CallbackGroup.OPERATOR_ADMIN, True, 'мастер операторов: добавление')` — `bot/aemr_bot/handlers/callback_router.py:135`
- `CallbackRoute('op:opcard:', CallbackGroup.OPERATOR_ADMIN, True, 'карточка оператора')` — `bot/aemr_bot/handlers/callback_router.py:136`
- `CallbackRoute('op:oprole:', CallbackGroup.OPERATOR_ADMIN, True, 'смена роли — открыть picker')` — `bot/aemr_bot/handlers/callback_router.py:137`
- `CallbackRoute('op:opchrole:', CallbackGroup.OPERATOR_ADMIN, True, 'смена роли — применить')` — `bot/aemr_bot/handlers/callback_router.py:138`
- `CallbackRoute('op:opdeact:', CallbackGroup.OPERATOR_ADMIN, True, 'деактивация — подтверждение')` — `bot/aemr_bot/handlers/callback_router.py:139`
- `CallbackRoute('op:opdeact_ok:', CallbackGroup.OPERATOR_ADMIN, True, 'деактивация — применить')` — `bot/aemr_bot/handlers/callback_router.py:140`
- `CallbackRoute('op:opreact:', CallbackGroup.OPERATOR_ADMIN, True, 'реактивация оператора')` — `bot/aemr_bot/handlers/callback_router.py:141`
- `CallbackRoute('op:setkey:', CallbackGroup.OPERATOR_ADMIN, True, 'экспертный wizard ключа')` — `bot/aemr_bot/handlers/callback_router.py:142`
- `CallbackRoute('op:set:', CallbackGroup.OPERATOR_ADMIN, True, 'иерархическое меню настроек')` — `bot/aemr_bot/handlers/callback_router.py:143`
- `CallbackRoute('op:tmpl:', CallbackGroup.BROADCAST_ADMIN, True, 'шаблоны рассылок')` — `bot/aemr_bot/handlers/callback_router.py:144`
- `CallbackRoute('op:bc:', CallbackGroup.BROADCAST_ADMIN, True, 'история рассылок: карточка / клон / failed')` — `bot/aemr_bot/handlers/callback_router.py:145`
- `CallbackRoute(payload, CallbackGroup.MENU_FALLBACK, False, 'fallback в меню')` — `bot/aemr_bot/handlers/callback_router.py:162`

## Настройки и окружение

Полный SCHEMA (26): `admin_notify_consent`, `admin_notify_monthly_stats`, `admin_notify_open_reminder`, `admin_notify_overdue_reminder`, `admin_notify_pulse`, `admin_notify_subscriptions`, `admin_quiet_hours_enabled`, `admin_quiet_hours_end`, `admin_quiet_hours_start`, `appointment_text`, `broadcast_max_images`, `commit_author_email`, `commit_author_name`, `consent_text`, `electronic_reception_url`, `emergency_contacts`, `localities`, `policy_url`, `sla_followup_hours`, `sla_response_hours`, `sla_work_schedule`, `topics`, `transport_dispatcher_contacts`, `udth_schedule_intermunicipal_url`, `udth_schedule_url`, `welcome_text`.

Алиасы config.py: `ADDRESS_MAX_CHARS`, `ADMIN_GROUP_ID`, `ANSWER_MAX_CHARS`, `APPEAL_TIMEOUT`, `ATTACHMENTS_MAX_PER_APPEAL`, `ATTACHMENTS_PER_RELAY_MESSAGE`, `AUDIT_ERASURE_RETENTION_DAYS`, `AUDIT_LOG_RETENTION_DAYS`, `BACKUP_ALLOW_UNENCRYPTED`, `BACKUP_DAY_OF_WEEK`, `BACKUP_GPG_PASSPHRASE`, `BACKUP_GPG_TIMEOUT_SECONDS`, `BACKUP_HOUR`, `BACKUP_KEEP_COUNT`, `BACKUP_LOCAL_DIR`, `BACKUP_MINUTE`, `BACKUP_PG_DUMP_TIMEOUT_SECONDS`, `BACKUP_RCLONE_TIMEOUT_SECONDS`, `BACKUP_S3_ACCESS_KEY`, `BACKUP_S3_BUCKET`, `BACKUP_S3_ENDPOINT`, `BACKUP_S3_SECRET_KEY`, `BACKUP_VERIFY_DAY`, `BACKUP_VERIFY_ENABLED`, `BACKUP_VERIFY_HOUR`, `BACKUP_VERIFY_MINUTE`, `BACKUP_VERIFY_TIMEOUT_SECONDS`, `BOOTSTRAP_IT_FULL_NAME`, `BOOTSTRAP_IT_MAX_USER_ID`, `BOT_EXTRA_CA_CERT`, `BOT_FIREWALL_MODE`, `BOT_MODE`, `BOT_NO_PROXY`, `BOT_OUTBOUND_PROXY`, `BOT_OUTBOUND_PROXY_FILE`, `BOT_TOKEN`, `BROADCAST_MAX_CHARS`, `BROADCAST_PROGRESS_UPDATE_SEC`, `BROADCAST_RATE_LIMIT_PER_SEC`, `BROADCAST_WIZARD_TTL_SEC`, `DATABASE_URL`, `ERASURE_STATE_DIR`, `FOLLOWUP_MAX_PER_HOUR_PER_APPEAL`, `FOLLOWUP_MIN_INTERVAL_SECONDS`, `HEALTHCHECK_INTERVAL_MIN`, `HEALTHCHECK_PULSE_SECONDS`, `HEALTHCHECK_STALE_SECONDS`, `HEALTHCHECK_URL`, `LIVEZ_POLL_STALE_FACTOR`, `LOG_DIR`, `LOG_FILE_BACKUPS`, `LOG_FILE_MAX_BYTES`, `LOG_LEVEL`, `MAX_API_RETRIES`, `MAX_API_TIMEOUT_SECONDS`, `NAME_MAX_CHARS`, `POLLING_CLIENT_TIMEOUT_BUFFER_SECONDS`, `POLLING_TIMEOUT_SECONDS`, `RECOVER_BATCH_SIZE`, `SEED_DIR`, `SLA_FOLLOWUP_HOURS`, `SLA_LUNCH_END_HOUR`, `SLA_LUNCH_START_HOUR`, `SLA_RESPONSE_HOURS`, `SLA_WORK_END_HOUR`, `SLA_WORK_START_HOUR`, `SUMMARY_MAX_CHARS`, `TZ`, `WEBHOOK_HOST`, `WEBHOOK_PORT`, `WEBHOOK_SECRET`, `WEBHOOK_URL`.

Пример окружения (включая закомментированные): `ABUSECH_AUTH_KEY`, `ADDRESS_MAX_CHARS`, `ADMIN_GROUP_ID`, `ANSWER_MAX_CHARS`, `APPEAL_TIMEOUT`, `ATTACHMENTS_MAX_PER_APPEAL`, `ATTACHMENTS_PER_RELAY_MESSAGE`, `AUDIT_LOG_RETENTION_DAYS`, `BACKUP_ALLOW_UNENCRYPTED`, `BACKUP_DAY_OF_WEEK`, `BACKUP_GPG_PASSPHRASE`, `BACKUP_GPG_TIMEOUT_SECONDS`, `BACKUP_HOUR`, `BACKUP_KEEP_COUNT`, `BACKUP_LOCAL_DIR`, `BACKUP_MINUTE`, `BACKUP_PG_DUMP_TIMEOUT_SECONDS`, `BACKUP_RCLONE_TIMEOUT_SECONDS`, `BACKUP_S3_ACCESS_KEY`, `BACKUP_S3_BUCKET`, `BACKUP_S3_ENDPOINT`, `BACKUP_S3_SECRET_KEY`, `BOOTSTRAP_IT_FULL_NAME`, `BOOTSTRAP_IT_MAX_USER_ID`, `BOT_EXTRA_CA_CERT`, `BOT_FIREWALL_MODE`, `BOT_MODE`, `BOT_NO_PROXY`, `BOT_OUTBOUND_PROXY`, `BOT_OUTBOUND_PROXY_FILE`, `BOT_TOKEN`, `BROADCAST_MAX_CHARS`, `BROADCAST_PROGRESS_UPDATE_SEC`, `BROADCAST_RATE_LIMIT_PER_SEC`, `BROADCAST_WIZARD_TTL_SEC`, `DATABASE_URL`, `ERASURE_STATE_DIR`, `FOLLOWUP_MAX_PER_HOUR_PER_APPEAL`, `FOLLOWUP_MIN_INTERVAL_SECONDS`, `GITHUB_PAT`, `GITHUB_PR_BASE_BRANCH`, `GITHUB_REPO`, `HEALTHCHECK_INTERVAL_MIN`, `HEALTHCHECK_PULSE_SECONDS`, `HEALTHCHECK_STALE_SECONDS`, `HEALTHCHECK_URL`, `LIVEZ_POLL_STALE_FACTOR`, `LOG_DIR`, `LOG_FILE_BACKUPS`, `LOG_FILE_MAX_BYTES`, `LOG_LEVEL`, `MAX_API_RETRIES`, `MAX_API_TIMEOUT_SECONDS`, `NAME_MAX_CHARS`, `PHISHTANK_APP_KEY`, `POLLING_CLIENT_TIMEOUT_BUFFER_SECONDS`, `POLLING_TIMEOUT_SECONDS`, `POSTGRES_DB`, `POSTGRES_PASSWORD`, `POSTGRES_USER`, `RECOVER_BATCH_SIZE`, `SEED_DIR`, `SLA_FOLLOWUP_HOURS`, `SLA_LUNCH_END_HOUR`, `SLA_LUNCH_START_HOUR`, `SLA_RESPONSE_HOURS`, `SLA_WORK_END_HOUR`, `SLA_WORK_START_HOUR`, `SUMMARY_MAX_CHARS`, `THREAT_INTEL_CACHE_PATH`, `TZ`, `WEBHOOK_HOST`, `WEBHOOK_PORT`, `WEBHOOK_SECRET`, `WEBHOOK_URL`.

Алиасы, которых нет в примере (это не утверждение обязательности): `AUDIT_ERASURE_RETENTION_DAYS`, `BACKUP_VERIFY_DAY`, `BACKUP_VERIFY_ENABLED`, `BACKUP_VERIFY_HOUR`, `BACKUP_VERIFY_MINUTE`, `BACKUP_VERIFY_TIMEOUT_SECONDS`.

## Имена текстов бота

- `ADMIN_CARD_TEMPLATE` — `bot/aemr_bot/copy/admin_texts.py:8`
- `ADMIN_FOLLOWUP_TEMPLATE` — `bot/aemr_bot/copy/admin_texts.py:29`
- `ADMIN_REPLY_DELIVERED_INTERMEDIATE` — `bot/aemr_bot/copy/admin_texts.py:48`
- `ADMIN_REPLY_TOO_LONG` — `bot/aemr_bot/copy/admin_texts.py:54`
- `ADMIN_REPLY_INTENT_EXPIRED` — `bot/aemr_bot/copy/admin_texts.py:59`
- `ADMIN_REPLY_NO_APPEAL` — `bot/aemr_bot/copy/admin_texts.py:65`
- `OP_HELP` — `bot/aemr_bot/copy/admin_texts.py:70`
- `OP_HELP_MAIN` — `bot/aemr_bot/copy/admin_texts.py:92`
- `OP_HELP_FULL` — `bot/aemr_bot/copy/admin_texts.py:149`
- `OP_ADD_OPERATORS_USAGE` — `bot/aemr_bot/copy/admin_texts.py:151`
- `OP_ADD_OPERATORS_RESULT` — `bot/aemr_bot/copy/admin_texts.py:164`
- `OP_NOT_AUTHORIZED` — `bot/aemr_bot/copy/admin_texts.py:170`
- `OP_APPEAL_NOT_FOUND` — `bot/aemr_bot/copy/admin_texts.py:175`
- `OP_APPEAL_REOPENED` — `bot/aemr_bot/copy/admin_texts.py:176`
- `OP_APPEAL_ALREADY_OPEN` — `bot/aemr_bot/copy/admin_texts.py:177`
- `OP_APPEAL_BLOCKED_BY_REVOKE` — `bot/aemr_bot/copy/admin_texts.py:180`
- `OP_APPEAL_CLOSED` — `bot/aemr_bot/copy/admin_texts.py:186`
- `OP_APPEAL_CLOSE_UNAVAILABLE` — `bot/aemr_bot/copy/admin_texts.py:187`
- `OP_USER_ERASED` — `bot/aemr_bot/copy/admin_texts.py:191`
- `OP_USER_BLOCKED` — `bot/aemr_bot/copy/admin_texts.py:192`
- `OP_USER_UNBLOCKED` — `bot/aemr_bot/copy/admin_texts.py:193`
- `OP_SETTING_UPDATED` — `bot/aemr_bot/copy/admin_texts.py:194`
- `OP_STATS_EMPTY` — `bot/aemr_bot/copy/admin_texts.py:195`
- `OP_FIND_RESIDENT_USAGE` — `bot/aemr_bot/copy/admin_texts.py:198`
- `OP_FIND_RESIDENT_NOT_FOUND` — `bot/aemr_bot/copy/admin_texts.py:210`
- `OP_FIND_RESIDENT_AMBIGUOUS_PHONE` — `bot/aemr_bot/copy/admin_texts.py:217`
- `OP_FIND_RESIDENT_CARD` — `bot/aemr_bot/copy/admin_texts.py:225`
- `OP_BROADCAST_PROMPT` — `bot/aemr_bot/copy/broadcast_texts.py:11`
- `OP_BROADCAST_TOO_LONG` — `bot/aemr_bot/copy/broadcast_texts.py:28`
- `OP_BROADCAST_CANCELLED_BY_USER` — `bot/aemr_bot/copy/broadcast_texts.py:32`
- `OP_BROADCAST_NO_SUBSCRIBERS` — `bot/aemr_bot/copy/broadcast_texts.py:33`
- `OP_BROADCAST_PREVIEW` — `bot/aemr_bot/copy/broadcast_texts.py:36`
- `OP_BROADCAST_PREVIEW_TRIM_WARN` — `bot/aemr_bot/copy/broadcast_texts.py:47`
- `OP_BROADCAST_EDIT_HINT` — `bot/aemr_bot/copy/broadcast_texts.py:53`
- `OP_BROADCAST_STARTED` — `bot/aemr_bot/copy/broadcast_texts.py:59`
- `OP_BROADCAST_PROGRESS` — `bot/aemr_bot/copy/broadcast_texts.py:60`
- `OP_BROADCAST_FAILED_SUFFIX` — `bot/aemr_bot/copy/broadcast_texts.py:64`
- `OP_BROADCAST_DONE` — `bot/aemr_bot/copy/broadcast_texts.py:65`
- `OP_BROADCAST_FAILED_LINE` — `bot/aemr_bot/copy/broadcast_texts.py:69`
- `OP_BROADCAST_CANCELLED` — `bot/aemr_bot/copy/broadcast_texts.py:70`
- `OP_BROADCAST_LIST_EMPTY` — `bot/aemr_bot/copy/broadcast_texts.py:74`
- `OP_BROADCAST_LIST_HEADER` — `bot/aemr_bot/copy/broadcast_texts.py:75`
- `OP_BROADCAST_LIST_ITEM` — `bot/aemr_bot/copy/broadcast_texts.py:76`
- `OP_BROADCAST_CARD` — `bot/aemr_bot/copy/broadcast_texts.py:79`
- `OP_BROADCAST_CARD_FAILED_LINE` — `bot/aemr_bot/copy/broadcast_texts.py:88`
- `OP_BROADCAST_NOT_FOUND` — `bot/aemr_bot/copy/broadcast_texts.py:90`
- `OP_BROADCAST_FAILED_LIST_EMPTY` — `bot/aemr_bot/copy/broadcast_texts.py:92`
- `OP_BROADCAST_FAILED_LIST_HEADER` — `bot/aemr_bot/copy/broadcast_texts.py:95`
- `OP_BROADCAST_FAILED_LIST_ITEM` — `bot/aemr_bot/copy/broadcast_texts.py:98`
- `OP_BROADCAST_FAILED_LIST_TRUNCATED` — `bot/aemr_bot/copy/broadcast_texts.py:99`
- `OP_BROADCAST_CLONE_NO_SUBSCRIBERS` — `bot/aemr_bot/copy/broadcast_texts.py:103`
- `OP_BROADCAST_WIZARD_EXPIRED` — `bot/aemr_bot/copy/broadcast_texts.py:107`
- `OP_TMPL_LIST_EMPTY` — `bot/aemr_bot/copy/broadcast_texts.py:117`
- `OP_TMPL_LIST_HEADER` — `bot/aemr_bot/copy/broadcast_texts.py:130`
- `OP_TMPL_CARD` — `bot/aemr_bot/copy/broadcast_texts.py:137`
- `OP_TMPL_CARD_LAST_USED` — `bot/aemr_bot/copy/broadcast_texts.py:147`
- `OP_TMPL_CARD_NEVER_USED` — `bot/aemr_bot/copy/broadcast_texts.py:148`
- `OP_TMPL_NEW_NAME_PROMPT` — `bot/aemr_bot/copy/broadcast_texts.py:151`
- `OP_TMPL_NAME_TOO_LONG` — `bot/aemr_bot/copy/broadcast_texts.py:166`
- `OP_TMPL_NAME_EMPTY` — `bot/aemr_bot/copy/broadcast_texts.py:170`
- `OP_TMPL_NAME_TAKEN` — `bot/aemr_bot/copy/broadcast_texts.py:173`
- `OP_TMPL_NEW_TEXT_PROMPT` — `bot/aemr_bot/copy/broadcast_texts.py:182`
- `OP_TMPL_TEXT_TOO_LONG` — `bot/aemr_bot/copy/broadcast_texts.py:200`
- `OP_TMPL_CREATED` — `bot/aemr_bot/copy/broadcast_texts.py:204`
- `OP_TMPL_RENAME_PROMPT` — `bot/aemr_bot/copy/broadcast_texts.py:210`
- `OP_TMPL_RENAMED` — `bot/aemr_bot/copy/broadcast_texts.py:218`
- `OP_TMPL_EDIT_PROMPT` — `bot/aemr_bot/copy/broadcast_texts.py:222`
- `OP_TMPL_EDITED_TEXT_ONLY` — `bot/aemr_bot/copy/broadcast_texts.py:231`
- `OP_TMPL_EDITED_WITH_IMAGES` — `bot/aemr_bot/copy/broadcast_texts.py:234`
- `OP_TMPL_PREVIEW_HEADER_NEW` — `bot/aemr_bot/copy/broadcast_texts.py:241`
- `OP_TMPL_PREVIEW_HEADER_EDIT` — `bot/aemr_bot/copy/broadcast_texts.py:250`
- `OP_TMPL_SEARCH_PROMPT` — `bot/aemr_bot/copy/broadcast_texts.py:259`
- `OP_TMPL_SEARCH_RESULTS_HEADER` — `bot/aemr_bot/copy/broadcast_texts.py:266`
- `OP_TMPL_SEARCH_NOTHING_FOUND` — `bot/aemr_bot/copy/broadcast_texts.py:269`
- `OP_TMPL_CLONE_NAME_PROMPT` — `bot/aemr_bot/copy/broadcast_texts.py:275`
- `OP_TMPL_CLONED` — `bot/aemr_bot/copy/broadcast_texts.py:283`
- `OP_TMPL_DELETE_CONFIRM` — `bot/aemr_bot/copy/broadcast_texts.py:288`
- `OP_TMPL_DELETED` — `bot/aemr_bot/copy/broadcast_texts.py:293`
- `OP_TMPL_CANCELLED` — `bot/aemr_bot/copy/broadcast_texts.py:295`
- `OP_TMPL_NOT_FOUND` — `bot/aemr_bot/copy/broadcast_texts.py:296`
- `OP_TMPL_NO_SUBSCRIBERS` — `bot/aemr_bot/copy/broadcast_texts.py:297`
- `WELCOME` — `bot/aemr_bot/copy/citizen_funnel.py:22`
- `CONTACT_REQUEST` — `bot/aemr_bot/copy/citizen_funnel.py:36`
- `CONTACT_RECEIVED` — `bot/aemr_bot/copy/citizen_funnel.py:42`
- `LOCALITY_REQUEST` — `bot/aemr_bot/copy/citizen_funnel.py:44`
- `GEO_DETECTED_FULL` — `bot/aemr_bot/copy/citizen_funnel.py:54`
- `GEO_DETECTED_LOCALITY_ONLY` — `bot/aemr_bot/copy/citizen_funnel.py:61`
- `GEO_OUTSIDE_EMO` — `bot/aemr_bot/copy/citizen_funnel.py:68`
- `APPEAL_ACCEPTED` — `bot/aemr_bot/copy/citizen_funnel.py:75`
- `APPEAL_LIST_EMPTY` — `bot/aemr_bot/copy/citizen_funnel.py:82`
- `APPEAL_EMPTY_REJECTED` — `bot/aemr_bot/copy/citizen_funnel.py:87`
- `NAME_EMPTY` — `bot/aemr_bot/copy/citizen_funnel.py:92`
- `ADDRESS_EMPTY` — `bot/aemr_bot/copy/citizen_funnel.py:94`
- `CONTACT_RETRY` — `bot/aemr_bot/copy/citizen_funnel.py:99`
- `APPEAL_CARD_TEMPLATE` — `bot/aemr_bot/copy/citizen_funnel.py:117`
- `CITIZEN_REPLY_TEMPLATE` — `bot/aemr_bot/copy/citizen_funnel.py:130`
- `USEFUL_INFO_TITLE` — `bot/aemr_bot/copy/citizen_funnel.py:145`
- `SETTINGS_MENU_TITLE` — `bot/aemr_bot/copy/citizen_funnel.py:147`
- `HELP_USER` — `bot/aemr_bot/copy/citizen_funnel.py:154`
- `SUBSCRIBE_CONFIRMED` — `bot/aemr_bot/copy/citizen_funnel.py:172`
- `SUBSCRIBE_ALREADY_ON` — `bot/aemr_bot/copy/citizen_funnel.py:177`
- `UNSUBSCRIBE_CONFIRMED` — `bot/aemr_bot/copy/citizen_funnel.py:180`
- `UNSUBSCRIBE_ALREADY_OFF` — `bot/aemr_bot/copy/citizen_funnel.py:186`
- `SUBSCRIBE_MINI_CONSENT` — `bot/aemr_bot/copy/citizen_funnel.py:190`
- `APPEAL_TEXT_TRUNCATED` — `bot/aemr_bot/copy/citizen_funnel.py:206`
- `FOLLOWUP_TEXT_TRUNCATED` — `bot/aemr_bot/copy/citizen_funnel.py:212`
- `BROADCAST_HEADER` — `bot/aemr_bot/copy/citizen_funnel.py:218`
- `POLICY_DELIVERED` — `bot/aemr_bot/copy/citizen_funnel.py:222`
- `POLICY_FALLBACK_URL` — `bot/aemr_bot/copy/citizen_funnel.py:226`
- `POLICY_UNAVAILABLE` — `bot/aemr_bot/copy/citizen_funnel.py:231`
- `STATUS_LABELS` — `bot/aemr_bot/copy/citizen_funnel.py:236`
- `CANCELLED` — `bot/aemr_bot/copy/errors_texts.py:6`
- `CANCEL_ALREADY_DONE` — `bot/aemr_bot/copy/errors_texts.py:14`
- `UNKNOWN_INPUT` — `bot/aemr_bot/copy/errors_texts.py:24`
- `CITIZEN_COMMAND_IN_ADMIN_CHAT` — `bot/aemr_bot/copy/errors_texts.py:29`
- `CONSENT_REQUEST` — `bot/aemr_bot/copy/privacy.py:7`
- `CONSENT_DECLINED` — `bot/aemr_bot/copy/privacy.py:42`
- `CONSENT_ACCEPTED` — `bot/aemr_bot/copy/privacy.py:48`
- `GOODBYE_PROMPT` — `bot/aemr_bot/copy/privacy.py:54`
- `CONSENT_STATUS_ACTIVE` — `bot/aemr_bot/copy/privacy.py:66`
- `CONSENT_STATUS_REVOKED` — `bot/aemr_bot/copy/privacy.py:73`
- `CONSENT_STATUS_NEVER` — `bot/aemr_bot/copy/privacy.py:85`
- `CONSENT_REVOKE_CONFIRM` — `bot/aemr_bot/copy/privacy.py:91`
- `CONSENT_REVOKED_OK` — `bot/aemr_bot/copy/privacy.py:105`
- `ERASE_CONFIRM` — `bot/aemr_bot/copy/privacy.py:117`
- `ERASE_REQUESTED` — `bot/aemr_bot/copy/privacy.py:131`
- `SECURITY_INFO_TEXT` — `bot/aemr_bot/copy/security_texts.py:9`
- `OP_HELP_SECURITY` — `bot/aemr_bot/copy/security_texts.py:64`
- `RULES_TEXT` — `bot/aemr_bot/copy/security_texts.py:121`
- `RULES_SHORT` — `bot/aemr_bot/copy/security_texts.py:165`

## Seed и статические данные

Имена и SHA256; бинарные материалы не анализируются как текст.

- `seed/consent.md:1` — 6841 байт, SHA256 `1fdbe032bf819b35fe65cadfac8b335c4bcbe9a368f2821d747d3ceb1a3b115f`
- `seed/contacts.json:1` — 1292 байт, SHA256 `92d6c2601db33e692b27f22ef80b624dab7b46feb4ddabd9ff2f44d6905b0842`
- `seed/geo/buildings.geojson:1` — 1013600 байт, SHA256 `8dc0897e206801b640fc2fe236886d91fe33c5018383def461e4bfd807ac6b49`
- `seed/geo/localities.geojson:1` — 252636 байт, SHA256 `058395634d49f5d9ad337f7f20990678e3f0f66e53d48bdf7f4ed08f2e3605b9`
- `seed/geo/streets.geojson:1` — 1213807 байт, SHA256 `1bf9da199b81ec1a529a651b7835e5a9c7e347e22d0139a6dc88218240c02f7f`
- `seed/holidays.json:1` — 1270 байт, SHA256 `3f8bb6d0e6f6818e39cd0a5a27737273ccdcdd5f9769c1885efcf7d95f2f629b`
- `seed/topics.json:1` — 242 байт, SHA256 `1491030f23f70170b40b6acae9f2d1a1155c094064c44382af2b251c3c3e2994`
- `seed/transport_dispatchers.json:1` — 450 байт, SHA256 `62ba205dceb76a719baa1b07564ff7a110098b8ffcc9df42ad3c5400026a9fc4`
- `seed/welcome.md:1` — 587 байт, SHA256 `eb112d5764948da520a10ea406ca58c656afacb42f47f71005bde103eb6e6beb`

## Прочитанные исходники приложения

Всего 180 Python-файлов; SHA256 каждого исходника:

- `bot/aemr_bot/__init__.py:1` — `48197610f156fa5b4fbd0eef6d5588b7202ccf768a608a633bbbdc7ebf381a45`
- `bot/aemr_bot/config.py:1` — `70ec0aadbd38e244586f4fc53078e309cce5425f0ce359ea8ba1f75c70714a07`
- `bot/aemr_bot/copy/__init__.py:1` — `b7a23b3fde2e7853b5c0fb526f84ac53b2947c9b02542998c4be004529b62871`
- `bot/aemr_bot/copy/admin_texts.py:1` — `45d5493055de949633433257de3d5e3f5004c33efe6c95b0e9679d3a57938e23`
- `bot/aemr_bot/copy/broadcast_texts.py:1` — `1491ebe1102b0303d95978fdb632170202d61e5a91f3bad82ecfe5a2769e0736`
- `bot/aemr_bot/copy/citizen_funnel.py:1` — `31d2b7c9e58b29f677b54535448c2a76d996ad48c74a87779d35bc165a06049b`
- `bot/aemr_bot/copy/draft_preview.py:1` — `61755837b5a7422dd721a620aa72073beae19d5ae299e42d8503923c48c8681b`
- `bot/aemr_bot/copy/errors_texts.py:1` — `20f7b96e7edfb32c7663a686a03b825cfe4dd6149f0087670f6152c6bb8ca0a4`
- `bot/aemr_bot/copy/privacy.py:1` — `6eca01f72bc99500bbd1096f17e07a5e0305c6ceec7126cfd9f461db8e0b46e9`
- `bot/aemr_bot/copy/security_texts.py:1` — `0ef2d9f8efedc45babfa3b40ccbcfb3ce41dc53c4a5177a590d142efcdd0096d`
- `bot/aemr_bot/data/__init__.py:1` — `af0a17d2526a3de2b104c77b4e8105c837ac78fed2b45ff16d408f2536dd95ee`
- `bot/aemr_bot/data/resident_names_v1.py:1` — `c105adf9e5ed5287c2cfd612c0f91f8762b79305a78deb7aa333175a3decf1e3`
- `bot/aemr_bot/data/resident_names_v2.py:1` — `9edfa21d98f110e6171823ed4d2ff7497e6388dc5f1bebb7999c8a1843704be0`
- `bot/aemr_bot/db/__init__.py:1` — `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
- `bot/aemr_bot/db/alembic/env.py:1` — `213d0cf12297ea03c9a5eb979c0c2532de17ee233c9b5f04d28b9529fa904156`
- `bot/aemr_bot/db/alembic/versions/0001_initial.py:1` — `0274ceb1f4208b8d7013666ea5fe829db14184f3ff22e4a9f29dbd6c61f91bfd`
- `bot/aemr_bot/db/alembic/versions/0002_broadcast.py:1` — `480172f4836b9c560f966a62efabf03d8d4c2810d4c2ef6e99b47fed524691ff`
- `bot/aemr_bot/db/alembic/versions/0003_phone_normalized.py:1` — `f041111806caeb24229563e26711f537b0ec4cfc1a22835024febc23f080ffd3`
- `bot/aemr_bot/db/alembic/versions/0004_indexes_and_autovacuum.py:1` — `bb87ff47279107cb93b257f3e8846bc0f67552c30bfc3035d71d20cfd92e8f05`
- `bot/aemr_bot/db/alembic/versions/0005_appeals_locality.py:1` — `a268e52134850a79b7a893e6ff0f6baec6ccd565160347baa83db122a453c79f`
- `bot/aemr_bot/db/alembic/versions/0006_consent_revoked_at.py:1` — `ec9201b7e7b8e0dd81f3f698972f61121d1cffa07618f3ed7191a8e50ccd8fc1`
- `bot/aemr_bot/db/alembic/versions/0007_consent_broadcast_anonymous.py:1` — `1345862c9450491a0596c89e1e00a80ce725399221c5a789debd9e241016792a`
- `bot/aemr_bot/db/alembic/versions/0008_backfill_consent_broadcast.py:1` — `d0e1d77dc416cb57a42126bccf586c932053f871515cb0ed8d593124551ec5c5`
- `bot/aemr_bot/db/alembic/versions/0009_partial_indexes_for_hot_paths.py:1` — `3f2758ee635b068db110dd8ccc997c270711457aeac681fe15f40cfb51b4de80`
- `bot/aemr_bot/db/alembic/versions/0010_pg_ops_hardening.py:1` — `03491bee7a5964c45e7b363af2a97a553487d90bbae23b5a6a175fa95080b9f8`
- `bot/aemr_bot/db/alembic/versions/0011_wizard_state_persistence.py:1` — `0b08f1ceef41d02320a991ee4e4c96f2177eeef2764e613e33212866cecfc814`
- `bot/aemr_bot/db/alembic/versions/0012_messages_appeal_created_index.py:1` — `79a4cf2b35d91c37f44131678aca1f853b7e80edcb1560cee22ceacbace87360`
- `bot/aemr_bot/db/alembic/versions/0013_settings_synced_at.py:1` — `15956f21ce26b6bb151ff5290bc5adc8c4f09ca9ecd162893f7ea16f8d70b889`
- `bot/aemr_bot/db/alembic/versions/0014_broadcasts_attachments.py:1` — `33fd603474ffa2cd0b08998666506e6299dcb8065e39e98fdbee4f3c75125cf0`
- `bot/aemr_bot/db/alembic/versions/0015_broadcast_templates.py:1` — `2f0d09481defff903fe8809ebb855e97d207b44b5e15abd35bdc41a8c7dd1296`
- `bot/aemr_bot/db/alembic/versions/0016_broadcast_template_usage.py:1` — `db4791a68a671f5a7cf341ce95fcab5788f5ed6442a0c434134a8c21a94d97af`
- `bot/aemr_bot/db/alembic/versions/0017_appeals_last_card_mid.py:1` — `4de3f6e76f0bd37f7627ee1c3fa4f079101b058a2e2d4bbfadf6eac91a2c382f`
- `bot/aemr_bot/db/alembic/versions/0018_users_trigram_search_indexes.py:1` — `4f1e9cdb0c1b927c566d8902d423f982008efff6e45798d67c50dd7442023834`
- `bot/aemr_bot/db/alembic/versions/0019_appeals_status_created_index.py:1` — `1bd487c7d85dc27a14d697506131c2d970b2183fca29ba03af1ecf78dc5f867f`
- `bot/aemr_bot/db/alembic/versions/0020_appeals_coordinates.py:1` — `a10b95808865f77d6bba107e3bc1204014f501987869dea9370165e6dac8136f`
- `bot/aemr_bot/db/alembic/versions/0021_drop_appeals_coordinates.py:1` — `a7d2222ffd6d3a1cbebe249f976bae93d7ea3127fa2b85e5391ad1126dc79b30`
- `bot/aemr_bot/db/alembic/versions/0022_users_consent_text_hash.py:1` — `942b1c9ce4d04a593108d06c0995cf0b0d2024d0b8408714146c9e2d56e0658a`
- `bot/aemr_bot/db/alembic/versions/0023_broadcast_deliveries_unique.py:1` — `86b9c2c969f1612d0bc22167bbc1cd157ca987d76fc8e05ea0057d1be885c7a7`
- `bot/aemr_bot/db/alembic/versions/0024_block_legacy_erased_residents.py:1` — `f11d8d8bd10d5dc7ca1d600f50b63c025c898c1442b317d0ecceb96d20bdf846`
- `bot/aemr_bot/db/alembic/versions/0025_appointment_text_okrug.py:1` — `0d4c9479f293365cc18fe43e1b0349bd9cbe6ddf66af7a1996a165bc972e8f76`
- `bot/aemr_bot/db/alembic/versions/0026_durable_receipt.py:1` — `0db3fe0cc7df4537cf17186a8d6ab27ffa574776eab2c962d7ce799d4052c506`
- `bot/aemr_bot/db/alembic/versions/0027_clear_invalid_resident_names.py:1` — `19fd522cbab063a4897c2601d8d3eb405a653eac3bd188875f282cff947dfee8`
- `bot/aemr_bot/db/alembic/versions/0028_appointment_head.py:1` — `befe8a6982b509d5c9948f82306f239c23860b2ce759988fbb00328828517ed4`
- `bot/aemr_bot/db/alembic/versions/0029_consent_records.py:1` — `e501c950f69ea4a8081dc020d8eaee51e1f8a297299de6ef996c3d6020df2eb6`
- `bot/aemr_bot/db/alembic/versions/0030_paused_appeal_draft.py:1` — `4b8e573c29378cd6fb3ab041c558b8bb579c098ab583fe8f8f15508c4c5336c3`
- `bot/aemr_bot/db/alembic/versions/0031_settings_revision.py:1` — `b49fba1dd483d52c5ab7668701204d14dfd5ca61c64d5f01372e8ac65299dd24`
- `bot/aemr_bot/db/alembic/versions/0032_appeal_sla_anchor.py:1` — `50e2903f7b3d3beb310cf72743b20fd61f2114a5bdf05b189b2a37c4e19e4241`
- `bot/aemr_bot/db/alembic/versions/0033_privacy_epoch.py:1` — `f3427fab113419b6f68017076d3b1b590d311db3228985c55fe2b9d625778cc9`
- `bot/aemr_bot/db/alembic/versions/0034_related_appeal.py:1` — `394821cd67b184df62143c182eeaa901466e88cad4754ab2ae01ff07dc133cf4`
- `bot/aemr_bot/db/alembic/versions/0035_message_response_kind.py:1` — `833cb587d48fe30627b376c2f8a5864e6682d6ae8aa4edde928de69d2e515091`
- `bot/aemr_bot/db/alembic/versions/0036_followup_delivery.py:1` — `1c520fe91406ccad14f2f6e059eea5f54701fac4be801922507ccb60942dccc2`
- `bot/aemr_bot/db/alembic/versions/0037_operator_reply_attempt.py:1` — `ca319a652801d1a456c16f1c40d737590851cda7bcee7458d48fb0485956f5cb`
- `bot/aemr_bot/db/alembic/versions/0038_appeal_sla_snapshot.py:1` — `1875f9b84447292e71cee1d13d33f425d02cb1dce0b38433c9f880cb8c779856`
- `bot/aemr_bot/db/alembic/versions/0039_operator_reply_drafts.py:1` — `68d9e389cf69533387c07eec7c20632042c23074eb0ff0d147b96abb888b2c82`
- `bot/aemr_bot/db/alembic/versions/0040_work_schedule.py:1` — `10006049d83e7bd0b14d4e855206b9282bd0d912926965582b6cacea160be0e7`
- `bot/aemr_bot/db/alembic/versions/0041_data_erasure.py:1` — `d3d7735362a1a634bed24326c176888d72abe90e619d6bb61ef646262db79ac4`
- `bot/aemr_bot/db/models.py:1` — `7fd7067fd610ed1d933a3df0a4637a01ac74767dc6d01556c89b3f74dcfba9d3`
- `bot/aemr_bot/db/session.py:1` — `d490c939cde1b84bd2324151f4faf9918344de72ef8f5db53a2c3129a20b94b8`
- `bot/aemr_bot/db/single_instance.py:1` — `608a7b027c69bac843eedc94326a9cf54bb7e29e8ca919599ed2f10d9ac2c69b`
- `bot/aemr_bot/handlers/__init__.py:1` — `81410d47f20ef239c1f354356312058fe52be1b420664534d2246e6709745511`
- `bot/aemr_bot/handlers/_auth.py:1` — `ac0400be43eeceb68b522b69840c068c4c124c7bf451e970e90167d131366231`
- `bot/aemr_bot/handlers/_common.py:1` — `660b14bb2ec951721b07f6741c2aa96851dd66ef7c60f261e4410ebc8017dd36`
- `bot/aemr_bot/handlers/admin_appeal_ops.py:1` — `742e066d184e49acfa35196be54c1ae7e4cff99287ba48e3a878eb328019707b`
- `bot/aemr_bot/handlers/admin_audience.py:1` — `af7826a664278d3f62a107cfcf9bda65005f65ef5b5a4daa4187daee0d8102dc`
- `bot/aemr_bot/handlers/admin_callback_dispatch.py:1` — `b64a1511e40fcacf862b231cec95530a2b659e1f86893fa31d1da8fda06cb395`
- `bot/aemr_bot/handlers/admin_commands.py:1` — `338f04193dad1b1afe96f96898f3207b7dcd4abc673c1e527c8120d41a257216`
- `bot/aemr_bot/handlers/admin_data_erasure.py:1` — `2430682e563f881da34bf9f1907bc88be0e1cc18c856e3d081d01c50680bbfe2`
- `bot/aemr_bot/handlers/admin_operators.py:1` — `e31cc9a07ba4374eaf14b157f9f980fb289ca6f2148d2ead23989566d3126007`
- `bot/aemr_bot/handlers/admin_operators_list.py:1` — `4372a26e2de714ce2507805512d53e96396cddd23efa5e8997d509b333ae345d`
- `bot/aemr_bot/handlers/admin_operators_roles.py:1` — `431e96a8bdadc9869cfca47dc94e439c4fe1291878da5031a8d1904df78a5e01`
- `bot/aemr_bot/handlers/admin_operators_wizard.py:1` — `9e9fd8dbf200401d1cd2876928105efa8c1cd97d8989d82067771f5cba6e01ba`
- `bot/aemr_bot/handlers/admin_panel.py:1` — `2638c7169b896d5f640e5f96fa98513a7c0b04c218e6717d549d8efc70bd5548`
- `bot/aemr_bot/handlers/admin_resident_search.py:1` — `e36444e13ed3c6ef64dcf6134b0444a097cd50424625cbc2023ccedec4a751dd`
- `bot/aemr_bot/handlers/admin_settings.py:1` — `bd625c8ffeb515c6c1ec4b69552bd8e8eaea7886534a272b38aebb105532a11f`
- `bot/aemr_bot/handlers/admin_settings_author.py:1` — `9bda48412b3e901df398c3d7065e53a48429cb8f69ff2b18722a07ce144e4078`
- `bot/aemr_bot/handlers/admin_settings_list.py:1` — `8e2013f4a2ce39a32c23eed73f6ecf8b185b73d2be8485ce33a293ee0612cf0e`
- `bot/aemr_bot/handlers/admin_settings_notify.py:1` — `b81d7ae44c949ab0019e1f2ee878fd48cde0f9901b350e82c4c458416aa395b4`
- `bot/aemr_bot/handlers/admin_settings_obj.py:1` — `2b5a5025165e7e83b0525bd6d46f0e86f362f9ed242f3550e89932438fe86aaf`
- `bot/aemr_bot/handlers/admin_settings_pr.py:1` — `1959217a98db17ae9b7ef3d1ea433cf7808e8f6439c7991c982c68997ea3c233`
- `bot/aemr_bot/handlers/admin_settings_quiet.py:1` — `6d4100cecd47b6003d2f44884a9cd76e85320d4e4e5238430cb468649549f4f5`
- `bot/aemr_bot/handlers/admin_settings_schedule.py:1` — `aa158958ad8d8a2386a8a172d36b4f9098195f328029c6ebb2cf269b00cdfc8e`
- `bot/aemr_bot/handlers/admin_settings_shared.py:1` — `da94a620dd8ac81a0ccca93f2ba05e0a2202abce34a8ebf07019da971cee102e`
- `bot/aemr_bot/handlers/admin_settings_sla.py:1` — `75efe12b9397ef0a6420b224f5c9d8c1282db67f7b031afac977091528271534`
- `bot/aemr_bot/handlers/admin_settings_text.py:1` — `24088843a4955ace02645e4fc00483f82b9515255656d0e2ccb1bac89d5ebe61`
- `bot/aemr_bot/handlers/admin_stats.py:1` — `dc5558ae7e88f71a30b398849f46ca9538f59d6f48208c2a1b406eafdaa20ff9`
- `bot/aemr_bot/handlers/appeal.py:1` — `97a55f467404857b83d9fc7ac92ab00b5d11d04894f24ae209142a5de11db2a8`
- `bot/aemr_bot/handlers/appeal_funnel.py:1` — `df7ed6776b9f5a31bdbb1346d0de655b2aca9a8b968314b8aa272a4c2323d37e`
- `bot/aemr_bot/handlers/appeal_geo.py:1` — `98883857e4bb963b0a2297edbbea3e3cdf6d8d0524dc52d4157cb78c52bc5046`
- `bot/aemr_bot/handlers/appeal_runtime.py:1` — `b751e6c30ee42885aaad6fd58b139c380047aff05f19ff6c0fe0de4811765122`
- `bot/aemr_bot/handlers/broadcast.py:1` — `82161f0f6dd454fa6e18863e410c42f3471442981bb544c3926d49e53d9aff5f`
- `bot/aemr_bot/handlers/broadcast_templates.py:1` — `ae280918d63391c4b9b3fcdf91edffc076e0099e44a705fa45990191b6621575`
- `bot/aemr_bot/handlers/broadcast_templates_crud.py:1` — `e75a9d7ad23458937a2b51c01303c03abf953891009c2ad84cbd2584029c6c3f`
- `bot/aemr_bot/handlers/broadcast_templates_list.py:1` — `f3c4537c079022e3e8ff36e696934533d073f5b8ed38f3ebd21deaa0ef86dce7`
- `bot/aemr_bot/handlers/broadcast_templates_state.py:1` — `2d6d4d2890e5432885a8628f337d79d1cd843822707c77451537019dc9dc17b3`
- `bot/aemr_bot/handlers/broadcast_templates_wizard.py:1` — `c5d88cbce8147cbdc6791c10156e3c05cdbeb22b90d2e5857c2d5fb51d10bb13`
- `bot/aemr_bot/handlers/broadcast_wizard.py:1` — `f1651a4f01d6b92dbe6f31d8deb680c76aed85f918c61b26a57ba4b1bebe7d62`
- `bot/aemr_bot/handlers/callback_payloads.py:1` — `42daf5a8aa8815b45ddfac0c89fdbf42e03140c1804f1af44c5adff9f4c0fd06`
- `bot/aemr_bot/handlers/callback_router.py:1` — `8df42126f3bd3d46249f91b01dfceae47b50af037bca50732009c95a39d9e1d7`
- `bot/aemr_bot/handlers/menu.py:1` — `5b5f3bdbb49456e2faa33c1e1103c989ebb910572b9f2218653a292cc4237dcc`
- `bot/aemr_bot/handlers/operator_draft.py:1` — `77ccb3d8de1c49ca72c30a3a298819218a07abe60fc7b33dd485cff3d324b174`
- `bot/aemr_bot/handlers/operator_reply.py:1` — `f111a7110f7249bb7714738300a374dfb5e5c2561e5b232f2c5f46b88cac5a78`
- `bot/aemr_bot/handlers/resident_name_gate.py:1` — `fd825ba5e981c0893c4dcaa979ad7c782105e2a5a524ca1e9c571f72c39c845e`
- `bot/aemr_bot/handlers/start.py:1` — `6d192c889aba058d088ca9db126d07dbb4eb0b18f7315019dc47649a27a72feb`
- `bot/aemr_bot/health.py:1` — `2f95cf0cdecd3fe6c6cd34011f7fcdfe10f712874b3e06f285c1a68f791fab85`
- `bot/aemr_bot/keyboards.py:1` — `db84ebdc392b48eed2557510bc288f5165b977259a80285cb5cb445e489a6a60`
- `bot/aemr_bot/logging_setup.py:1` — `0137b44569e599ea2d3476b26070fba145e2eb3f92f86bae1bd494ac79c4798e`
- `bot/aemr_bot/main.py:1` — `ac6b0bd49951dc94fcec254cc68b8532f85428f9c4043af49ec9ebede15c473a`
- `bot/aemr_bot/network.py:1` — `35bc7413b8f9fae04464b112f7e671ba81e00bf2e1eb4d74fa321cd4bf26a679`
- `bot/aemr_bot/services/__init__.py:1` — `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
- `bot/aemr_bot/services/admin_bus.py:1` — `3e000e9397ecb4d6558237d3de935e50692c3b6d151c0e3ab7884db369083c21`
- `bot/aemr_bot/services/admin_card.py:1` — `9879c2bf89a66fdc3b6bdb7f97ffe9e53202387a251f98103b774e8e745337e7`
- `bot/aemr_bot/services/admin_events.py:1` — `e36ae0bd8f1b0e80d075d39ba022da96123b56272c102b7cee55047f102f9ced`
- `bot/aemr_bot/services/admin_relay.py:1` — `3a3491d62b0601e0437e4227a4f8af756dd0eb272e2d00ca2cdb928f3b1ba60e`
- `bot/aemr_bot/services/alert_state.py:1` — `a09efe1bf79da36b8d887d778367675077e0de55c480f57a26bd355a86dfdf49`
- `bot/aemr_bot/services/appeal_history.py:1` — `4faa3cd7f10cce7e100e6a14f63c0906315fc8ee6abc5c2f3bd413b6bb9fcba8`
- `bot/aemr_bot/services/appeal_locks.py:1` — `45968c7c532daf6e3a74f012d649edc5cf4e8959414b7a8e8e563edbc4011d5c`
- `bot/aemr_bot/services/appeal_word.py:1` — `5ce6db15366fe47bb85db149c716cadc6f29efa31428b148f5729cb48688924e`
- `bot/aemr_bot/services/appeals.py:1` — `a4ec0d3479037ff37eda503fd702501f30a305e999a2f3befe863e36a4ca00a4`
- `bot/aemr_bot/services/backup_verify.py:1` — `90740609557cf492018e9dcd9ca9b94df955511c7005004d7916d4f54d94bbee`
- `bot/aemr_bot/services/broadcast_templates.py:1` — `5d227ba4bd897612544f30d0ce2ed9543878297c2e5f105b6406d781b69cd296`
- `bot/aemr_bot/services/broadcast_utils.py:1` — `15300e68906d49f69282d29d44724d4857f979d57d4af0ee991d2846296cb9c1`
- `bot/aemr_bot/services/broadcasts.py:1` — `ab80365473f407d5a2b5c69079196df23f26af2c656a960ada770122a26f6358`
- `bot/aemr_bot/services/calendar_ru.py:1` — `73793987226666ac81b302c3046f736e240e6f614ad14680e0fbe0cebd931a1e`
- `bot/aemr_bot/services/card_format.py:1` — `66ea23e091134fee0b80076ab0a35fbf0d59e605e9f2496d0d40906bded7a92b`
- `bot/aemr_bot/services/complete_stats.py:1` — `321750df5126b9e8579c900b048592c64c932775301765c5cc2d044c7001b8fe`
- `bot/aemr_bot/services/consent_history.py:1` — `a75889b2ff304e41f683c080412ee0a6773fb3a4ff66001c24c201f3acd3c081`
- `bot/aemr_bot/services/content_status.py:1` — `368caf4743e7f132853890f6d3bf9c3ab5553a0d83caa2c31ad090c4ba8a9658`
- `bot/aemr_bot/services/cron.py:1` — `e4fbe4b0524a2b090cd16c64f4a3e444ad4021df71690b98c39a9cf0c050e8ee`
- `bot/aemr_bot/services/cron_registry.py:1` — `878ff066481e84c1fda8e2c563f037912f2e0db29e3b2c66d98b13547819252e`
- `bot/aemr_bot/services/data_erasure.py:1` — `094c65b72d6ea8d676236196e6715c5606b3a30fde1966cdcd7545baf8071a5b`
- `bot/aemr_bot/services/data_erasure_delivery.py:1` — `8b7b856b71fc9b6f819c3908c1b73be82346b748674a05455d40e691a7d700fa`
- `bot/aemr_bot/services/data_erasure_journal.py:1` — `2b57bdbbe7423ce46c583f134357b5ef06654bf443cc7d0810822ab927ece9e2`
- `bot/aemr_bot/services/data_erasure_restore.py:1` — `f02d1ff285efd7e6f8b3e0ff15df8fc3136612c8b38403a6a92cd520e8287212`
- `bot/aemr_bot/services/db_backup.py:1` — `1c89a88b3c9b988fc57052164ae71715a20811cd1d338fca80ad2000e3ebeacf`
- `bot/aemr_bot/services/followup_delivery.py:1` — `3e7444a44921f4052e5f62660f69028ef39e669458155d4b5d84acc9835792c8`
- `bot/aemr_bot/services/geo.py:1` — `f975cc0fd72a42efdefd1e5ea349f2f51d7c725ad68cf4142dac5179451f17c8`
- `bot/aemr_bot/services/idempotency.py:1` — `03ddbc5443b1a9cedf0e23b7504e6d6487cced3728dd41055909a38f1f7cef37`
- `bot/aemr_bot/services/inbox.py:1` — `b6c00f6caf0c10e4ee825dc55322f4e7eba7f9a2cadfc6cdeb00f3e5a720dff0`
- `bot/aemr_bot/services/message_labels.py:1` — `a3a9c91d5e95ee90e8294517b79c294587b3a85e7979b6740a3770fa1cc61146`
- `bot/aemr_bot/services/notify_toggles.py:1` — `9ef4f39df424734563027fb29777e43be51249d8fdccb11a92f8425f75f6581e`
- `bot/aemr_bot/services/operator_drafts.py:1` — `bb9fbab034d2b1bf95c098e69f68b258fd1e3749bdffaa5b363c0ff6f018d038`
- `bot/aemr_bot/services/operator_reply_attempts.py:1` — `b3325f61d820e19c5848b4487ceed55b4a523327c2088f0e15a8d7bbf7157ff7`
- `bot/aemr_bot/services/operators.py:1` — `3f9c578b66a16f0facca4e2611d3507eaec43d712d0ef6fd88066d9d689c6634`
- `bot/aemr_bot/services/policy.py:1` — `c12274fae363444ce962571bea2cd1e4126406659cebe75a021bed1aabe008f6`
- `bot/aemr_bot/services/privacy_epoch.py:1` — `31b0c48f2b1e525281d835b13fe4aca54e7ff0021c8797ac8f67c5551bd8bced`
- `bot/aemr_bot/services/progress.py:1` — `b8fcfdb8309b827815b02d265f982850ef0cd883998f3829ee77510cc87622ae`
- `bot/aemr_bot/services/queue_panel.py:1` — `5e8f68898f495dc2c09df5140d2e91102c606a73dd0eefb1cee73d1342fef77b`
- `bot/aemr_bot/services/quiet_hours.py:1` — `ffc3785eb78c735b2d2db33bb635f88bb1bda168a4ffece378d18fe7318adf6b`
- `bot/aemr_bot/services/repo_sync.py:1` — `078afc8b465b33e2c03e7fdb4c8aeceea65d76aab7ae014b5ad31167a534b7c9`
- `bot/aemr_bot/services/resident_drafts.py:1` — `c8206da5601918406d0030bb926cd2bd328fb11cea023ec3a00f2491a9668428`
- `bot/aemr_bot/services/settings_store.py:1` — `6b3e1f5e1af6dc83259d6461a82db0f7c402061324cff8ad9085bfcd6355b63b`
- `bot/aemr_bot/services/sla.py:1` — `268c1dbebff5843997a64b6f16e50cb85760578500ab3d9d1a406be34a6e2019`
- `bot/aemr_bot/services/stats.py:1` — `3bcfa54e5dab07b30a2a13a36b4a6d5ebfe8dbfea554e4f8c1a054229adab789`
- `bot/aemr_bot/services/threat_intel.py:1` — `0495ec5d951639ec74f785f0ad07ac8ed38969471e110bf22146fc3586085ccf`
- `bot/aemr_bot/services/uploads.py:1` — `7532fedc2eaffebb13668770c3bf4d4ccadd0c462be83a7de7766dcdfa083881`
- `bot/aemr_bot/services/users.py:1` — `f0111f9a4a55ee595da9e68169ca6462552acebf7173b036b0d6e945de77aeee`
- `bot/aemr_bot/services/wizard_persist.py:1` — `8c5a43e226bd7d0de545ce41c8e1920389d0d341944f7a5bdef14313f832c231`
- `bot/aemr_bot/services/wizard_registry.py:1` — `f9c46634552c9703e4b46fb06917e4e79ed8f8eb1cbaa7d013dc5f8ed09ceab5`
- `bot/aemr_bot/services/work_schedule.py:1` — `b21600bd6ba4c35aaed5b345af6e702cbd0f5f1d13fa3c489d53f489b1a4b9ad`
- `bot/aemr_bot/texts.py:1` — `ecd7b6129e5e23440c42153fd2fba7188e7c5ac0f233989a9c9f14dc688d43d3`
- `bot/aemr_bot/transport.py:1` — `0245e6af7ccb231b47eb6c3b0b5288dbad7990e0354acf59ded9ecd96603ac30`
- `bot/aemr_bot/ui/__init__.py:1` — `65e166158c8ec1c74a5b8e3677f1ef6d7d1b07caa0043e238011e4075871f14a`
- `bot/aemr_bot/ui/broadcast_keyboards.py:1` — `386afd9084a3de9e223a3c488505c6c1eddaaf815e206c21ddf6ea585c135b99`
- `bot/aemr_bot/ui/citizen_keyboards.py:1` — `d712a517c94b9a06727c82882b8b4a4457c95bdf7484a2dc4021e11d2883ad77`
- `bot/aemr_bot/ui/operator_keyboards.py:1` — `4a79880a0800b916a92d7fb118dcd6ae4e2543e5a95bb4f3cbd9854a526aebf1`
- `bot/aemr_bot/ui/settings_keyboards.py:1` — `30379b246a8a526687672d2d06f03b823526e08f3cede0892bed53f1f45ecf02`
- `bot/aemr_bot/ui/wizard_keyboards.py:1` — `4ac8fb03e5948fcd6f1441e45ab04329ba7342185b9ca69ee53c7292554d94ea`
- `bot/aemr_bot/utils/__init__.py:1` — `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
- `bot/aemr_bot/utils/attachments.py:1` — `eb8f0676c836ca74cb7bbf96b4b54402de43bf3f7937d685767bd814e48ec648`
- `bot/aemr_bot/utils/background.py:1` — `4696843de52fb0f066b402549f9b50bde7df4542a870f67acfaa3ed0eae15149`
- `bot/aemr_bot/utils/delivery_guard.py:1` — `34b65de2d5ae7674e8dec275d137fb1453c7b6650f78e593f40df82758d0c9d1`
- `bot/aemr_bot/utils/event.py:1` — `263f569b6ce374b74a3ae7c81a092d02b3dee1e00f67b98300395a8ba63fe83b`
- `bot/aemr_bot/utils/image_attachments.py:1` — `2cb0fcbcd092d40331ff978eebaed1584f140bd7fcd6778598f97bf5e19d09e7`
- `bot/aemr_bot/utils/menu_tracker.py:1` — `1e434e7ca9c842214a39db7f86f506fe7be05a5bde2665b68d64d36f67a3d89a`
- `bot/aemr_bot/utils/pii_mask.py:1` — `e60686e1da5ef4ff57143de44164f3dd7a520c1218c275104e066ddb96a5b593`
- `bot/aemr_bot/utils/resident_lifecycle.py:1` — `8ea74fbd5a3f0fcede65e2042a54ae715d62f02898b06d0bc785f20f795d3d51`
- `bot/aemr_bot/utils/resident_names.py:1` — `4b93fd8621b16d2a0c2f4debe171fd1a02c768590f7f8ee3feb63891ded0eeeb`
- `bot/aemr_bot/utils/text_pages.py:1` — `3781a4dd48c27dbdc45edb1ac80eb19f55a9f146471cb0c03b0b06209e38263d`
- `bot/aemr_bot/utils/typing_indicator.py:1` — `3da1011abf95b08c319fadb50cb5f4b3223f99aafccc71e369280b58ac3b0dbe`
- `bot/aemr_bot/utils/url_defang.py:1` — `3a0fae93861c897758e96df644da304b1ba767047d77f82f5efe06d8b2182917`
