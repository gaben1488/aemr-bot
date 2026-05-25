# Scam vectors against citizens via gov-bot

> Дата: 2026-05-26. HEAD `a2a2b87`. Research-аудит социально-инженерных
> атак с использованием aemr-bot или его клонов. Жертва — пожилые
> жители Камчатки. Только рекомендации, без реализации.

Контекст 2026: MAX — национальный мессенджер, подключены Госуслуги и
десятки тысяч госканалов. VK за июль 2025 заблокировал 10 000 номеров
мошенников и 32 000 вредоносных документов; к весне 2026 фон вырос «в
разы» ([paperpaper](https://paperpaper.io/moshennikov-v-max-stanovitsya-vsyo-bolshe-p/),
[РБК Компании](https://companies.rbc.ru/news/TjVEKHFPbn/moshenniki-v-messendzhere-max-riski-shemyi-i-otvetstvennost/)).
За год у пожилых украли 1.5 млрд руб
([news.ru](https://news.ru/society/samoj-rasprostranennoj-shemoj-moshennikov-protiv-pensionerov-stala-socinzheneriya)).

---

### Vector 1: Fake-bot phishing (impersonation)

**Threat model.** Атакующий регистрирует MAX-бота с похожим username
(`aemr_help`, `aemr_bot_official`) и обзванивает жителей Елизовского
района. Пожилые не различают `aemr_bot` от `aemr_bot_official`.

**Real-world precedent.** Фейковый «Пенсионный фонд» с приглашениями
([Ведомости](https://www.vedomosti.ru/society/news/2025/09/05/1137218-prizvali-ne-verit),
[Банки.ру](https://www.banki.ru/news/lenta/?id=11017414)); звонки «от
сотрудника администрации» о грамотах ([РБК](https://www.rbc.ru/rbcfreenews/6919a0af9a7947cd2015259b));
фейковый Signal Support Bot — государственный actor использует
identical UI ([The Hacker News](https://thehackernews.com/2026/03/fbi-warns-russian-hackers-target-signal.html)).

**aemr-bot exposure.** Никакой защиты. У бота нет «метки A+»
(Roskomnadzor registry, [smmplanner](https://smmplanner.com/blog/max-i-ofitsialnyie-viedomstva-kak-oni-rabotaiut-vmiestie/)).
В `seed/welcome.md:1-9` и `bot/aemr_bot/texts.py:1-8` (`WELCOME`) нет
упоминания verified-метки и настоящего username.

**Existing defense.** Нет.

**Recommended addition.**
1. Зарегистрировать бота в реестре MAX «Метка A+» через РКН — после
   этого клиент MAX сам рисует значок верификации.
2. В `welcome.md` явная строка: настоящий username + ссылка на
   страницу администрации, где он опубликован.
3. Договорённость с пресс-службой Администрации: любое изменение
   username — публикация на elizovomr.ru.

**Severity:** 🔴

---

### Vector 2: Malicious URL via settings

**Threat model.** Скомпрометированный IT-оператор меняет `policy_url`
/ `electronic_reception_url` / `udth_schedule_url` на phish-домен.
Жители кликают и попадают на клон Госуслуг.

**Real-world precedent.** Фейковый ПФР с подменёнными ссылками —
массово в 2025 ([CNews](https://www.cnews.ru/news/top/2025-09-08_v_rossijskoj_kiberpolitsii)).

**aemr-bot exposure.** Защита есть: `bot/aemr_bot/services/settings_store.py:29-43,136-144`
— whitelist `elizovomr.ru`/`kamgov.ru`/`gosuslugi.ru`/`kamchatka.gov.ru`
со всеми поддоменами. Применяется к 4 URL-ключам со схемой `url=True`
(строки 103-106). Audit-trail `before/after` в
`handlers/admin_settings.py:909-938`. **Дыра:** whitelist не привязан
к путям — любой `https://elizovomr.ru/<что_угодно>` проходит;
subdomain takeover тоже не ловится.

**Existing defense.** SEC #4 whitelist. Audit-log `setting_update`.
GitHub-PR review (`_create_pr`).

**Recommended addition.**
1. Внешний cron (вне бота): резолвить каждый URL, проверять, что в
   HTML есть маркер «Администрация Елизовского». Расхождение → алёрт
   в admin-группу.
2. Включить diff чувствительных URL в дневной pulse.
3. В SECURITY.md: расширение whitelist требует второго ревьюера.

**Severity:** 🟡

---

### Vector 3: Markdown/HTML injection в welcome/consent/appointment

**Threat model.** IT-оператор через `op:set:text:welcome_text`
(`admin_settings.py:217-220`) кладёт `<a href="phish">` или
`[click](phish)`. На главном экране житель видит «ссылку от
Администрации».

**Real-world precedent.** Кража MAX-аккаунтов через короткие ссылки
от доверенного отправителя ([Хабр](https://habr.com/ru/news/952312/),
[Сбер](https://www.sberbank.ru/ru/person/kibrary/reminders/chto-delat-esli-vzlomali-messendzher-makh)).

**aemr-bot exposure.** Удивительный (счастливый) вывод:
**`welcome_text`/`consent_text` в БД хранятся, но НЕ рендерятся
жителю.** `handlers/menu.py:120-124` шлёт hardcoded `texts.WELCOME`;
`start.py:79,92` — то же. UI редактирования есть
(`admin_settings.py:306-307`, `keyboards.py:953-954`), но конечная
точка чтения не подключена — dormant capability. `appointment_text`
(`menu.py:773-780`) реально идёт жителю как plain text, parse-mode не
выставляется (HTML включён только в `services/progress.py:153`); сейчас
безопасно, но MAX в будущем может включить auto-link для markdown.

**Existing defense.** Случайная — отсутствие чтения. SEC #4 whitelist
не покрывает text-ключи.

**Recommended addition.**
1. Решить судьбу `welcome_text`/`consent_text`: удалить schema-ключ
   и UI (мёртвый код провоцирует ложную безопасность) либо подключить
   с одновременным sanitizer'ом.
2. На все free-text ключи жителю (`appointment_text`,
   `broadcast.text`) — валидатор: запрет URL вне gov-whitelist,
   запрет HTML-тегов, запрет markdown `[text](url)`.
3. Audit-log пометить такие правки `risk=high` — отдельный фильтр в
   pulse.

**Severity:** 🟡

---

### Vector 4: Broadcast spoofing (compromised operator)

**Threat model.** Скомпрометирован `coordinator`/`it`. Атакующий
запускает `/broadcast`: «СРОЧНО! Карта газоснабжения изменилась,
оплатите перепрошивку счётчика [phish-link]». При 5 000 подписчиков и
1 RPS — 80 минут на полную доставку.

**Real-world precedent.** Фейковая рассылка от ПФР с +30% к пенсии
([CNews](https://www.cnews.ru/news/top/2025-09-08_v_rossijskoj_kiberpolitsii));
СМС о «блокировке пенсии» ([pencioner.ru](https://www.pencioner.ru/news/tsifry-i-fakty/skhemy-moshennichestva-2026-kak-ne-poteryat-pensiyu-feykovye-soobshcheniya-o-blokirovke-pensii/)).

**aemr-bot exposure.** Высокая. `handlers/broadcast.py:_handle_confirm:277-339`
— один оператор жмёт «Разослать», задача стартует в фоне. Нет:
- four-eyes approval,
- cooldown между двумя рассылками (`broadcast_rate_limit_per_sec=1.0`
  — это per-message rate, **не** global cooldown),
- URL-фильтра на `broadcast.text` (любой текст 0-1000 симв,
  `config.broadcast_max_chars`).

Кнопка «⛔ stop» (`broadcast.py:407-422`) доступна, но 100-500 жителей
получат сообщение до реакции.

**Existing defense.** RBAC: только `it`/`coordinator`
(`broadcast.py:128`). Audit-log `broadcast_send`. Лимит текста.

**Recommended addition.**
1. **Two-man rule.** После confirm первого — карточка «⏳ Ожидает
   подтверждения второго оператора»; рассылка стартует только после
   второго `it`/`coordinator` нажатия в течение N минут.
2. **Cooldown 10 мин** между двумя broadcast от одного оператора.
3. **URL-фильтр на тело рассылки** — gov-whitelist, аналогично
   settings.
4. **Delay-window 60 сек после confirm.** Карточка «через 60 сек
   уйдёт N подписчикам — ⛔ Отменить». Атака не успевает обогнать
   реакцию.

**Severity:** 🔴

---

### Vector 5: Operator account takeover

**Threat model.** Аккаунт IT-оператора скомпрометирован (фишинг,
перехват SMS, утерянный телефон). Атакующий получает: рассылки,
ответы жителям, `/setting`, `/erase`, `/add_operators`.

**Real-world precedent.** MAX-аккаунты массово крадутся через
перехват кодов входа ([РБК](https://www.rbc.ru/rbcfreenews/68b973039a7947a6b118c36b),
[appleinsider](https://appleinsider.ru/tips-tricks/moshenniki-prosyat-ustanovit-max-zachem-im-eto-nuzhno.html));
Star Blizzard и Microsoft device-code атаки ([SC Media](https://www.scworld.com/perspective/how-phishing-changed-in-2025-and-what-to-expect-in-2026-and-beyond)).

**aemr-bot exposure.** Идентификация — только по `max_user_id`
(`SECURITY.md` §4.4-4.5). 2FA нет. SEC #6 проверяет `is_active`
перед reply (это закрывает «уволен», не «скомпрометирован прямо
сейчас»).

**Existing defense.** RBAC + audit_log + SEC #6.

**Recommended addition.**
1. **Второй фактор для критических команд** (`/erase`,
   `/add_operators`, broadcast, smena `welcome_text`): PIN-код,
   выданный оператору вне MAX (корпоративная почта или физический
   токен).
2. **Anomaly-detection cron**: счётчик мутирующих действий за час;
   при превышении нормы — алёрт.
3. **Geo/device-fingerprint check.** MAX даёт device info при
   `MessageCreated`; новое устройство → soft-warning в админ-чат.
4. В RUNBOOK — пошаговая процедура для первых 5 минут после звонка
   «у меня украли телефон».

**Severity:** 🔴

---

### Vector 6: Followup link injection (citizen → operator)

**Threat model.** Житель (или fake-житель) дополняет обращение
текстом с фишинговой ссылкой или картинкой с QR-кодом. Оператор
кликает «из любопытства» — фишится сам, см. Vector 5.

**Real-world precedent.** Социалка через входящие коммуникации —
стандарт ([РБК Компании](https://companies.rbc.ru/news/TjVEKHFPbn/moshenniki-v-messendzhere-max-riski-shemyi-i-otvetstvennost/)).

**aemr-bot exposure.** Полная. `handlers/appeal_funnel.on_awaiting_followup_text:566-727`
принимает любой текст + вложения, релейит в админ-чат через
`services/admin_relay.relay_attachments_to_admin`. Никакой sanitation
URL. На outgoing operator reply
(`operator_reply._send_reply_to_citizen:316-384`) — **тоже нет
URL-фильтра**: оператор может ответить жителю любой ссылкой.

**Existing defense.** SEC #5 — rate-limit на followup (5/час, min 30s),
не контент. HTML-escape только в funnel-карте (`progress.py`), не в
admin-карточке.

**Recommended addition.**
1. **URL-маркер в карточке оператора.** Переписать ссылки внутри
   followup жителя как `[citizen-link: example.com]` без активной
   гиперссылки + видимый warning «не кликать без проверки».
2. **Outgoing URL whitelist на operator reply** или soft-confirm: «вы
   хотите послать жителю ссылку foo.com вне gov-whitelist —
   подтвердите».
3. Training-страница в `docs/` для операторов: «как опознать фишинг
   во входящем».

**Severity:** 🟡

---

### Vector 7: PII-фишинг через support-impersonation

**Threat model.** Атакующий пишет жителю в личку: «Я оператор бота,
для верификации обращения #123 пришлите фото паспорта и СНИЛС». Жертва
ассоциирует «бот» с доверенным источником.

**Real-world precedent.** Звонки «из городской администрации» о
выплатах ([РБК](https://www.rbc.ru/rbcfreenews/6919a0af9a7947cd2015259b));
фейковый сотрудник Госуслуг с просьбой кода ([sfr.gov.ru](https://sfr.gov.ru/projects/moshenniki_v_socialnom_sektore/)).

**aemr-bot exposure.** Высокая. В `seed/welcome.md`, `seed/consent.md`,
`texts.WELCOME` **нет ни строки** «мы НИКОГДА не запрашиваем
СНИЛС/паспорт/коды/деньги». Жители не знают, что атака возможна.

**Existing defense.** Нет.

**Recommended addition.**
1. В `welcome.md` и `consent.md` блок «❌ Что бот НИКОГДА не делает»:
   не просит паспорт, не просит SMS-коды, не пишет первым в личку, не
   просит денег. Текст стабильный, не настраиваемый через UI (или
   защищённый whitelist-валидатором).
2. В `card_format.citizen_reply` (footer ответа оператора): «Если
   кто-то пишет вам "от Администрации" и просит данные — это не мы.
   Сообщите по телефону XXX».
3. Памятка «как опознать настоящего бота» на elizovomr.ru.

**Severity:** 🔴

---

### Vector 8: Currency-scam через настройки контактов

**Threat model.** Компрометированный it подменяет номер в
`emergency_contacts` или `transport_dispatcher_contacts` на свой.
Житель звонит «диспетчеру ЖКХ», тот разводит на «оплату госпошлины».

**Real-world precedent.** Подмена реквизитов СБП — массовая практика;
звонки от «диспетчера ЖКХ» с просьбой оплатить
([sfr.gov.ru](https://sfr.gov.ru/projects/moshenniki_v_socialnom_sektore/)).

**aemr-bot exposure.** Есть. `settings_store.SCHEMA:108-113` —
проверка `item_keys={name, phone}` без формата телефона, без
whitelist на код страны/региона. Можно поставить `phone: "8-800-..."`
(платная линия) или зарубежный номер. `appointment_text` (`SCHEMA:107`)
— свободный текст, в DEFAULTS уже есть номер «8 (415-31) 7-25-29»,
легко подменяется.

**Existing defense.** Audit-trail. PR-workflow.

**Recommended addition.**
1. **Regex на формат телефона** в `emergency_contacts.phone` /
   `transport_dispatcher_contacts.phone`: российские мобильные/городские,
   обязательный код Камчатки `8 (415` для диспетчерских.
2. **Diff-снапшот**: эталон `seed/contacts.json` из git, ежечасный diff
   с production. Расхождение → алёрт.
3. В `appointment_text` запрет строк, похожих на платёжные реквизиты
   (БИК, расчётный счёт, карта): regex `\d{20}`/`\d{16}` → reject.

**Severity:** 🟡

---

### Vector 9: 2026-актуальные скамы для РФ — применимость

**Threat model.** AI voice cloning (elderly), fake gov-bots, false
subsidies, фейковый ПФР, перехват SMS-кодов через MAX. Любой может
использовать aemr-bot как side-channel доверия.

**Real-world precedent.** AI-голос: успешность атак выросла с 12% в
2024 до 34% в 2026, потери в США $2.3B ([UnboxFuture](https://www.unboxfuture.com/2026/04/ai-voice-cloning-scams-targeting.html?m=1),
[SavingAdvice](https://www.savingadvice.com/articles/2026/05/21/10736407_ai-voice-cloning-scams-explode-one-in-four-people-have-encountered-them-losing-up-to-15000.html)).
Перехват SMS из MAX от Госуслуг — основной риск
([Сбер](https://www.sberbank.ru/ru/person/kibrary/reminders/chto-delat-esli-vzlomali-messendzher-makh)).

**aemr-bot exposure.** Бот хранит телефоны жителей; после breach БД
атакующий получит готовый list для звонков «от мэра» с клонированным
голосом. Сам бот SMS-коды не просит, но «привычка писать всё в бот»
снижает порог недоверия к poor-impersonator.

**Existing defense.** Нет специфической.

**Recommended addition.**
1. **Минимизация PII**: рассмотреть hash-only режим для phone,
   plaintext только в активной переписке, стирать после answer/close.
2. **Educational push**: раз в N месяцев — broadcast «памятка
   безопасности» со свежими приметами скам-схем.
3. В `docs/PRIVACY.pdf` и `Политика.md` явный пункт: «Администрация
   не звонит первой по обращениям — только в ответ на ваше».

**Severity:** 🟡
