---
status: applied
applied_in_pr: 89
applied_at: 2026-05-26
superseded_by: bot/aemr_bot/services/threat_intel.py (abuse.ch URLhaus + ThreatFox
  + Kaspersky OpenTIP реализация); cron `threat-intel-refresh` ежечасно :17.
note: Research выбрал abuse.ch как primary + Kaspersky OpenTIP как secondary.
  Реализация в PR #89, далее F11-fix через ChatMembersManager (PR #90).
---

# URL threat-intelligence для aemr-bot: research + рекомендация

Дата: 2026-05-26. Цель — выбрать источник IOC-списков (Indicators of
Compromise — URL/домены/IP известного malware и phishing) для
**входящих** сообщений жителей, чтобы оператор получал
предупреждение, если житель прислал в обращении ссылку из чёрного
списка. Whitelist гос-доменов на исходящих ответах не отменяется,
threat-intel — дополнительный слой на входе.

Контекст исполнения: self-hosted на 1 VPS (4 ГБ RAM), 50–100 обращений
в день, не критичный throughput. Главное требование — **graceful
degradation**: если feed недоступен, бот продолжает работать, просто
без warning'а.

## 1. Kaspersky Threat Intelligence Portal (OpenTIP) + Threat Data Feeds

Свободный уровень — портал OpenTIP (opentip.kaspersky.com): REST API
для lookup'а URL/домена/IP/хеша. Без API-ключа квота 200 запросов в
сутки, с зарегистрированным ключом для community-аккаунта — 2 000
запросов в сутки. Возвращает JSON с zone (Green/Yellow/Red), категорией
угрозы и related-IOC. Формат — single-request lookup, не bulk-выгрузка
feed'а.

Коммерческие Kaspersky Threat Data Feeds (Phishing URL Data Feed,
Malicious URL Data Feed) — bulk JSON по HTTPS, обновление каждые 20
минут, цена через корпоративные каналы (трёхлетние подписки порядка
сотен тысяч рублей по российскому прайсу; точная цена только под NDA).
Для гос-органов РФ Kaspersky **категорически доступен**: компания
российская, санкции против неё не действуют внутри РФ, есть отдельные
гос-договорные каналы через 152-ФЗ-комплаентного интегратора.

Pro: native-русская поддержка, отличное покрытие RU-фишинга, без
санкционных рисков. Con: free-tier — это lookup-API (200–2 000/сут), не
bulk-feed; коммерческие feed'ы дороги и требуют закупки через
полугодовую процедуру 44-ФЗ. Лицензия OpenTIP запрещает коммерческую
перепродажу и автоматическую массовую инжекцию в продакшен без
согласования. Latency lookup — порядка 300–600 мс.

## 2. PhishTank (phishtank.org)

Open-feed классика OpenDNS/Cisco. Формат — XML / CSV / JSON / Serialized
PHP, gzip и bz2 архивы. Обновление **каждый час ровно**, объём
`online-valid.json.bz2` — порядка 5–15 МБ распакованного (десятки тысяч
verified URL за последние недели). Без app-key — несколько download'ов
в сутки на IP, с ключом — unlimited HEAD-проверки через ETag.

Состояние в 2026: проект пережил кризис 2023 («registration closed,
rethinking from the ground up»), но developer-портал снова раздаёт
ключи, dumps обновляются ежечасно. Цена 0, лицензия — открытая для
некоммерческих и внутрикорпоративных проверок (для гос-бота это
допустимый use-case, перепродажу feed'а мы не делаем).

Pro: бесплатно, человеко-валидированные URL (низкий false-positive),
hourly cadence достаточен для 50 обращений в сутки. Con: после кризиса
2023 reliability ниже исторической, app-key регистрация иногда «closed»,
покрытие RU-фишинга слабое (PhishTank сильнее по EN/брендовому
фишингу — PayPal, Microsoft, банки US/EU).

## 3. OpenPhish community feed

Текстовый список URL, free-tier обновляется **раз в 12 часов** на
GitHub (`openphish/public_feed`). Объём — несколько тысяч URL.
Premium-тариф — каждые 5 минут с metadata (target brand, ASN,
geolocation), цена под запрос (порядка 1–5 k$/мес по индустрии).
Premium бесплатно отдаётся law-enforcement, national CERT'ам,
академии — гос-орган МО формально подходит, но процедура одобрения
непрозрачна и небыстра.

Pro: text-format тривиален в парсинге, GitHub-доставка надёжна. Con:
12-часовая частота — пропустим свежий фишинг первых полусуток, что для
phishing критично (медианная жизнь URL — 24–48 часов).

## 4. abuse.ch URLhaus

Swiss non-profit (под Spamhaus с 2023), фокус — **malware
distribution** URL (payload-делiver, не phishing). Форматы: CSV
(`csv_online`, `csv_recent`), JSON, plain-text, Snort/Suricata-rules,
ClamAV-signatures, DNS RPZ. Update каждые 5 минут. Объём «online URLs»
порядка 5–20 МБ CSV, full dump за 90 дней — десятки МБ. Бесплатно,
требуется Auth-Key через abuse.ch Authentication Portal (мгновенная
регистрация). Fair-use лицензия, коммерческая перепродажа feed'а под
запрет.

Pro: высокое качество, real-time cadence, минимальный false-positive,
санкционно-нейтральная Швейцария. Con: **только malware-URL, не
phishing** — не покрывает основной угрозы для жителя (фишинговое письмо
«ваш пенсионный фонд»).

## 5. abuse.ch ThreatFox

Тот же оператор, что URLhaus, но шире: IOC (URL, домены, IP, хеши)
любых семейств malware, C2-серверы, sinkhole'ы. JSON-API + bulk-export
(MISP, CSV, JSON, host-file, Suricata). Update real-time, expiry IOC —
6 месяцев (с 2025-05-01). Auth-Key обязателен, бесплатно, fair-use.

Pro: один источник для URL + домен + IP, удобен для unified IOC-set.
Con: bias к malware-инфраструктуре (C2, payload), не targeted-phishing;
размер base — порядка 50–200 МБ JSON (но `domain-only` host-file —
единицы МБ).

## 6. Google Safe Browsing API v4

Free-tier — 10 000 запросов в сутки на Lookup API, либо локальный
Update API с bloom-filter-подобной hash-prefix-базой (полная база порядка
сотен МБ, обновления дельтой каждые 30 мин). Покрытие огромное (Google
видит весь web).

**Legal-блокер для aemr-bot**: API формально доступен (геоблока нет на
endpoint), но (а) ToS запрещает «non-personal» использование без Web
Risk коммерческой лицензии, (б) РФ-регуляторика 2025–2026 движется к
тотальному ограничению Google-сервисов, законопроект Госдумы декабря
2025 о localizaiton data может сделать использование Google API в
гос-системе формально проблемным с точки зрения 152-ФЗ и
импортозамещения. Для гос-бота МО Камчатки — **не рекомендую**.

Pro: лучший recall в мире. Con: legal-серая зона для российского
гос-органа, риск внезапной недоступности по политическим мотивам, ToS
запрещает гос-применение без enterprise-договора.

## 7. Роскомнадзор / НКЦКИ реестры

«Единый реестр запрещённых сайтов» (vigruzki.rkn.gov.ru) — это
**цензурный** реестр (экстремизм, наркотики, суициды, азартные игры),
не threat-intel против malware/phishing. Доступ — только для лиц со
статусом «оператор связи» и квалифицированной ЭЦП от
Минцифры-аккредитованного УЦ; обновление каждый час. Для бота МО это
**нерелевантный инструмент**: жителю-фишеру не нужно слать ссылку на
заблокированный наркосайт.

НКЦКИ ФинЦЕРТ (Банк России) отдаёт банковский фишинг-фид, но доступ —
только для лицензированных финорганизаций. ГосСОПКА-фид —
ограниченного распространения, для субъектов КИИ (объектов критической
информационной инфраструктуры); aemr-bot формально под определение не
попадает.

Не подходит ни один из РКН-реестров по mandate'у.

## 8. Российские коммерческие: BI.ZONE, Positive Technologies, Group-IB

Все три отдают threat-intel feed только корпоративным клиентам по
платной подписке, через прямой договор. BI.ZONE-CERT публикует
research-отчёты, но не bulk-feed для self-host'а. Group-IB как
юрлицо ушёл из РФ в 2023, российская часть — F.A.C.C.T., тоже
коммерчески. Positive Technologies под санкциями США/ЕС, но это не
блокирует use внутри РФ.

Для гос-органа возможен бесплатный канал через CERT-GOV-RU (НКЦКИ при
ФСБ), но процедура регистрации непрозрачна и недокументирована
публично. Для бота МО уровня — overkill.

## Сводная таблица

| Feed | Цена | Формат | Cadence | Размер | Latency | Покрытие RU |
|---|---|---|---|---|---|---|
| Kaspersky OpenTIP API | free 200/сут | JSON lookup | live | n/a | 300–600 мс | отличное |
| Kaspersky Phishing Feed | ~100k+ ₽/год | JSON bulk | 20 мин | ~10–50 МБ | offline | отличное |
| PhishTank | free | JSON/CSV bz2 | 1 час | 5–15 МБ | offline | слабое |
| OpenPhish community | free | text | 12 часов | ~1 МБ | offline | слабое |
| URLhaus | free | CSV/JSON | 5 мин | 5–20 МБ | offline | среднее (malware-only) |
| ThreatFox | free | JSON/host-file | live | 5–200 МБ | offline | среднее |
| Safe Browsing API | free 10k/сут | hash-prefix | 30 мин | сотни МБ | offline | отличное |
| РКН vigruzki | gov-only | XML+ЭЦП | 1 час | — | offline | нерелевантно |

## Архитектурная рекомендация для aemr-bot

**Базовая комбинация (free, self-host, защита от падения):**

Локальный set из трёх ежечасно-обновляемых выгрузок:

- **URLhaus** `csv_online` — malware-payload URL'ы (5 мин cadence,
  ставим cron на 1 час чтобы не bить API);
- **PhishTank** `online-valid.json.bz2` — verified phishing URL
  (hourly), несмотря на слабое RU-покрытие даёт high-precision base;
- **ThreatFox** `host-file` (domain-only) — C2/malware-домены, мелкий
  объём.

Хранить как in-memory Python `set[str]` нормализованных хостов (точный
match по hostname + lookup по полному URL для path-specific entries из
URLhaus/PhishTank). Cron-update раз в час через `apscheduler` (он уже в
проекте — см. broadcast scheduler), staleness budget — 6 часов: если
свежее обновление не пришло за 6 часов, оператору в admin chat падает
тихий warning, но бот продолжает работу со стейл-set'ом. **Никаких
live-API-вызовов на hot-path** — это ключевое требование reliability.

Поверх локального set'а — опциональная **Kaspersky OpenTIP** проверка
для URL'ов, **не найденных** в локальном set'е, с rate-limit 100/сут
(половина free-квоты, чтобы оставался запас) и hard timeout 1.5 с.
Kaspersky закрывает дыру в RU-покрытии и даёт zone-score, который у
abuse-feed'ов отсутствует. Любая ошибка/таймаут API — `try/except` →
без warning'а, не блокирует обработку обращения.

Reglament: warning оператору отдаётся **советательно**, не блокирует
сообщение жителя — у нас appeal от гражданина, который мог переслать
полученный фишинг с просьбой разобраться, удалять такое сообщение
нельзя. В admin card'е секция «⚠️ Подозрительные ссылки» со списком и
источником (URLhaus/PhishTank/Kaspersky-Red).

**Чего не делать:** не подключать Safe Browsing API (legal-серая
зона + потенциальная недоступность), не пытаться получить vigruzki.rkn
(нет статуса оператора связи, нерелевантный mandate), не закупать
Kaspersky Threat Data Feeds коммерческие (overkill для 50–100
обращений/сутки), не делать live-lookup на каждый URL (точка отказа).

**Оценка трудозатрат:** ~200 строк Python (downloader + normalizer +
set-lookup + admin-card-секция + cron-задача + healthcheck-метрика),
~5–30 МБ диска под кэш выгрузок, ~10–50 МБ RAM под loaded set,
~3 HTTP-запроса в час суммарно — pull-only, никаких входящих
соединений, идеально для self-host VPS.

## Sources

- [Kaspersky Threat Intelligence Portal](https://opentip.kaspersky.com/Help/Doc_data/WorkingWithAPI.htm)
- [Kaspersky Threat Data Feeds](https://www.kaspersky.com/enterprise-security/threat-data-feeds)
- [Kaspersky Phishing URL Data Feed (OEM)](https://usa.kaspersky.com/phishing-url-data-feed)
- [PhishTank Developer Info](https://www.phishtank.com/developer_info.php)
- [PhishTank API](https://phishtank.com/api_info.php)
- [OpenPhish Phishing Feeds](https://openphish.com/phishing_feeds.html)
- [OpenPhish community public_feed (GitHub)](https://github.com/openphish/public_feed)
- [URLhaus Community API](https://urlhaus.abuse.ch/api/)
- [URLhaus Feeds](https://urlhaus.abuse.ch/feeds/)
- [ThreatFox Community API](https://threatfox.abuse.ch/api/)
- [Google Safe Browsing v4 Overview](https://developers.google.com/safe-browsing/v4)
- [Google Safe Browsing v4 Usage Restrictions](https://developers.google.com/safe-browsing/v4/usage-limits)
- [Roskomnadzor vigruzki (operator portal)](https://vigruzki.rkn.gov.ru/)
- [EAIS Roskomnadzor (public lookup)](https://eais.rkn.gov.ru/)
- [BI.ZONE Digital Risk Protection](https://bi.zone/eng/catalog/products/digital-risk-protection/)
- [Russia tightens internet rules, may block Google services (Dec 2025)](https://www.androidheadlines.com/2025/12/russia-moves-closer-to-a-full-block-of-google-services-in-tightening-internet-rules.html)
