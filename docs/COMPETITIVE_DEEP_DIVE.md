# COMPETITIVE_DEEP_DIVE — расширенный анализ + MAX features + roadmap

**Дата:** 2026-05-10. Расширение `COMPETITIVE_BRIEF.md` через WebSearch/WebFetch.

## A. Конкуренты — реальные продукты

| # | Продукт | Платформа | Ключевые фичи | SLA / отзывы |
|---|---|---|---|---|
| 1 | **Госуслуги.Решаем-вместе (ПОС)** | mobile + web + виджеты | категории, фото/видео/файлы, статус-таймлайн, ЕСИА, 23 области, 710 подкатегорий | 30 раб. дней (59-ФЗ); 3.1★ Google Play |
| 2 | **Помощник Москвы 2.0** | iOS + Android | фиксация нарушений ПДД/парковки, фото+GPS+время, баллы, тёмная тема | 1.9★ RuStore — критика что только парковка |
| 3 | **Активный гражданин (Москва)** | iOS + Android + web | опросы по благоустройству, баллы → льготы, ЕСИА | 4.3★; не для жалоб, co-creation |
| 4 | **Народный контроль РТ** | iOS + Android + web | категории, GPS, фото-видео, публичная карта «до/после» | работает с 2014, 10-дневный SLA |
| 5 | **Народный инспектор РТ** | iOS + Android | подача нарушений ПДД с фотофиксацией → штраф | узко: только ПДД |
| 6 | **«Моя Казань» @kznhelpbot** | Telegram | дерево тем → вопросы → автоформирование заявки | альтернатива ПОС |
| 7 | **Добродел (Подмосковье)** | iOS + Android + web | 710 подкатегорий, объединён с Госуслугами МО | работает с 2015 |
| 8 | **@Ufahotbot (Уфа)** | Telegram | сезонные обращения (зимние: уборка, отопление) | узкая тематика |
| 9 | **@mfc02_bot (Башкортостан)** | Telegram | запись/отмена в МФЦ, статус, адреса | сервисный |
| 10 | **adm_vl_bot (Владивосток)** | Telegram | официальный канал админ. (бот + канал) | UX-данных нет |
| 11 | **Госуслуги-бот в MAX** | MAX | привязка по номеру = mos.ru, статус заявлений, push | СФР, 2026 |
| 12 | **SeeClickFix / CivicPlus 311** | iOS + Android + web + chatbot + voice | фото+GPS, голосование на жалобах, omnichannel inbox, anon/named/guest, neighbourhood feed | США, 600+ типов |
| 13 | **FixMyStreet (UK, mySociety)** | web + mobile + open-source | карта-репорт, OSM, gateway по адресу в нужный совет | 20+ стран, v6 — geo-button |
| 14 | **311 Toronto** | iOS + Android + web + voice + live agent | 600+ сервисов, GPS+фото, SMS/email tracking, neighbourhood map, 50+ языков | бенчмарк UX |
| 15 | **GOV.UK Design System** | web (референс) | паттерны форм, ошибок; WCAG 2.1 AA обязателен | золотой стандарт |

**Не нашёл данных** по специализированным мессенджер-ботам приёма обращений в Калуге, Туле, Якутии, ХМАО, Челябинске, Сочи, Краснодаре, Екатеринбурге, Самаре — у этих городов есть только веб-приёмные и новостные Telegram-каналы.

---

## B. MAX Bot Platform — что доступно

| # | Возможность | Поддержка | Используем? |
|---|---|---|---|
| 1 | Текст до 4000 симв., Markdown+HTML | да | да |
| 2 | Image / Video / File attachment (до 4 ГБ resumable) | да | да |
| 3 | Audio attachment | ❓ требует проверки | нет |
| 4 | Sticker / Share / Location / Contact attachment | да | частично (contact да) |
| 5 | Inline-keyboard до 210 кнопок, 30 рядов | да | да |
| 6 | Button: callback (response = update + notify одним вызовом) | да | да |
| 7 | Button: link, request_geo_location, request_contact | да | да |
| 8 | Button: open_app (Mini-App через MAX Bridge) | да | **нет — большой потенциал** |
| 9 | Button: message (текст в бот от имени user) | да | нет |
| 10 | Button: clipboard (копирует payload) | да | нет |
| 11 | Button: payment | ❌ нет | n/a |
| 12 | Edit message (PUT /messages, до 24 ч) | да | частично |
| 13 | Reply / quote / forward | ❓ Bot API не документирует | требует проверки |
| 14 | Polls в групповых чатах (запущены апр.2026) | да | нет |
| 15 | Reactions (быстрые 2-tap; negative отключены) | в клиенте; в Bot API ❓ | нет |
| 16 | Silent / disable_notification | ❓ нет в docs | n/a |
| 17 | Webhook (port 443, CA-cert, 24h timeout) | да | да |
| 18 | Long polling (только dev) | да | нет (мы webhook) |
| 19 | Mark-as-seen | да | нет |
| 20 | Get chat message history (уникум, нет в TG) | да | **нет — потенциал** |
| 21 | Pin/unpin message | да | нет |
| 22 | Inline-mode @mention в любом чате | ❌ нет | n/a |
| 23 | Reply Keyboard (замена клавиатуры) | ❌ нет | n/a |
| 24 | WebApp MainButton / themeParams / CloudStorage | ❌ нет (отличие от TG) | n/a |
| 25 | GigaChat AI в клиенте (расшифровка аудио) | да (клиентская фича) | n/a |

---

## C. Roadmap фич — 27 идей

Шкала 1-5: 5 = max. Сложность 5 = высокая. MAX-fit = насколько фича соответствует возможностям платформы.

| # | Фича | Польза | Сложн. | MAX-fit | У кого есть |
|---|---|---|---|---|---|
| 1 | Edit-message с галочками выбора (заменить append echo на in-place) | 4 | 2 | 5 | nobody |
| 2 | Прогресс-бар воронки в edit («Шаг 3/5 ▓▓▓░░») | 4 | 2 | 5 | GOV.UK pattern |
| 3 | Публичная карта обращений (read-only точки) | 5 | 3 | 4 | Татарстан, FixMyStreet, SeeClickFix |
| 4 | «Похоже на ваше прошлое обращение» (дедуп) | 4 | 3 | 5 | SeeClickFix |
| 5 | Голосовое обращение (audio → транскрибация) | 5 | 4 | 4 | ❓ MAX audio; нет в РФ-ботах |
| 6 | Mini-app: сложная форма с картой+фото-предпросмотром | 4 | 5 | 5 | nobody на MAX |
| 7 | Clipboard-кнопка «скопировать №обращения» | 3 | 1 | 5 | nobody |
| 8 | Pinned-сводка в admin_group: неотвеченные >SLA | 3 | 2 | 4 | внутреннее |
| 9 | ML-классификатор темы (BERT/GigaChat) → авто-dispatch | 4 | 5 | 3 | NYC 311 research |
| 10 | Шаблоны быстрых ответов оператора (snippets с {name}/{addr}) | 4 | 2 | 5 | SeeClickFix CRM |
| 11 | AI-черновик ответа оператору (RAG по прошлым) | 4 | 5 | 3 | leewayhertz |
| 12 | Дашборд для главы: DAU, SLA, топ-категории | 5 | 3 | 4 | SeeClickFix CRM |
| 13 | Weekly digest жителям «что починили» | 4 | 2 | 5 | SeeClickFix feed |
| 14 | NPS-опрос «как мы справились?» после закрытия | 4 | 2 | 5 | 311 Toronto, GDS |
| 15 | Polls в admin_group для голосования по приоритетам | 2 | 1 | 5 | n/a |
| 16 | Подписка на район (push при новом обращении рядом) | 3 | 3 | 4 | SeeClickFix, FixMyStreet |
| 17 | «До/после» ремонта — фото от оператора | 4 | 2 | 4 | Татарстан |
| 18 | ЕСИА-авторизация (отдельная сессия) | 4 | 5 | 3 | все крупные |
| 19 | TTS-озвучка ответов для слабовидящих | 3 | 4 | 3 | n/a |
| 20 | Высококонтрастный режим / large-text меню | 3 | 2 | 5 | GDS WCAG |
| 21 | Многоязычие (рус/англ/корякский) | 2 | 3 | 5 | 311 Toronto |
| 22 | Open-data API (CSV/JSON для журналистов) | 3 | 2 | n/a | FixMyStreet, SeeClickFix |
| 23 | Календарь приёма главы (mini-app со слотами) | 4 | 4 | 5 | МФЦ02 Башкортостан |
| 24 | Сезонные шоткаты (зима: уборка/отопление) | 4 | 1 | 5 | Уфа @Ufahotbot |
| 25 | Channel «Новости Елизовского МО» + push о ЧС | 4 | 1 | 5 | СФР Госуслуги в MAX |
| 26 | Mark-as-seen для оператора («житель прочитал ответ») | 3 | 2 | 5 | nobody — MAX-уникум |
| 27 | History-context welcome: «Вы на шаге 3 — продолжить?» | 4 | 2 | 5 | nobody — MAX даёт history API |

---

## D. Топ-10 высокоценных дешёвых фич — to-do

1. **Edit-message с галочками** (#1, ~3д): заменить append echo на edit предыдущего сообщения; хелпер `edit_with_check()` в `app/handlers/start.py`.
2. **Прогресс-бар воронки** (#2, ~2д): «Шаг N/5 ▓▓▓░░» в каждый FSM-шаг через edit; метаданные в `app/fsm/steps.py`.
3. **Шаблоны быстрых ответов** (#10, ~3д): таблица `op_templates(id,title,body,vars)` + кнопки в admin_group; подстановка `{name}`, `{addr}`.
4. **Clipboard-кнопка №обращения** (#7, ~1ч): `clipboard:appeal_<N>` в карточке готового обращения.
5. **Pinned-сводка >SLA в admin_group** (#8, ~2д): cron каждые 30 мин обновляет pinned через PUT /messages.
6. **NPS-опрос через 24ч после закрытия** (#14, ~2д): 3 callback 😞/😐/😊, лог в БД.
7. **Сезонные шоткаты** (#24, ~1д): ноябрь-март кнопка «❄️ Снег/уборка» в стартовом меню с pre-filled темой.
8. **Channel + push о ЧС** (#25, ~3д): MAX-канал, кнопка «📢 Подписаться на ЧС».
9. **Weekly digest «что починили»** (#13, ~3д): cron вс 18:00 → подписавшимся.
10. **History-context welcome** (#27, ~2д): /start через `GET /messages` показывает «Вы остановились на шаге адрес — продолжить?».

---

## E. Источники

**MAX:** dev.max.ru/docs-api, dev.max.ru/docs/webapps/introduction, vc.ru/telegram/2799410-sravnenie-max-bot-api-i-telegram-bot-api, github.com/max-messenger/max-botapi-python, ura.news/articles/1053090494 (polls апр.2026), vk.company/ru/press/releases/12295, sfr.gov.ru/press_center/news~2026/02/18/278939, ura.news/articles/1053085237.

**РФ-конкуренты:** tatar-inform.ru @kznhelpbot, ufacity.info/press/news/421022.html @Ufahotbot, ufacitynews.ru @mfc02_bot, t.me/s/adm_vl, play.google.com/store/apps/details?id=com.uip.crowdcontrol (Народный контроль РТ), digital.tatarstan.ru/gis-narodniy-inspektor.htm, uslugi.mosreg.ru/app (Добродел), play.google.com/store/apps/details?id=ru.mos.helper (Помощник Москвы), kommersant.ru/doc/5735728, ag.mos.ru, gosuslugi.ru/help/obratitsya_v_pos.

**Зарубеж + UX:** seeclickfix.com, en.wikipedia.org/wiki/SeeClickFix, fixmystreet.org, github.com/mysociety/fixmystreet, apps.apple.com/us/app/311-toronto/id1558520141, design-system.service.gov.uk.

**AI/ML routing:** arxiv.org/html/2605.06482 (NYC 311 RL), leewayhertz.com/ai-in-complaint-management.

---

**Шелф-лайф:** 3 месяца. Перечитать после релиза P0 (карта, edit+галочки, шаблоны).
