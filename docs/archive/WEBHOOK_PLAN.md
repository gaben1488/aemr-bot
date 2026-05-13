# WEBHOOK_PLAN — переход с long polling на webhook через Caddy сайта `dash`

**Дата:** 2026-05-10. **Применил:** `engineering:system-design` + `operations:change-request`.

## Цель

Переключить aemr-bot с режима long polling на webhook через защищённый HTTPS-эндпоинт с минимальной attack surface. Работать поверх существующей инфраструктуры edge-Caddy сайта `dash`, не сломав сайт.

## Когда применять этот план

Webhook **не нужен** при текущем потоке (5-6 обращений/день). Polling работает, задержка 5-30 сек неощутима. Внедрение оправдано в одном из случаев:

- Объём вырос ≥10 обращений/мин (мгновенная реакция критична)
- Появилась интеграция с внешним сервисом, требующим webhook (например, Госуслуги-маршрутизация в нашу администрацию)
- Параноидально-безопасное архитектурное решение от руководства

## Текущее состояние сервера 193.233.244.217

```
edge-Caddy (aemr-caddy-1):
  слушает 0.0.0.0:80 + 443
  сеть aemr_aemr
  DOMAIN=:80 (HTTP по IP, без HTTPS)
  reverse: /api/* → server:3000, /* → web:80

aemr-bot (aemr-bot-bot-1):
  порт 127.0.0.1:8080 (только localhost)
  сеть aemr-bot_default (изолирована)

База: PostgreSQL контейнер aemr-bot-db-1
```

Edge-Caddy и бот **в разных сетях Docker** — Caddy не достучится к боту по имени без external-network подключения.

---

## Этапы внедрения

### Этап 0 — От пользователя (предварительно)

| Что | Зачем | Кто делает |
|---|---|---|
| Выбрать поддомен `bot-feedback.<домен>.ru` или отдельный домен | MAX требует HTTPS для webhook подписки. Сейчас сайт работает по IP без HTTPS — webhook невозможен | Владелец |
| Купить домен в `reg.ru` / `nic.ru` (если ещё нет) | DNS-управление | Владелец |
| Прописать DNS A-record на 193.233.244.217 | Caddy получит Let's Encrypt-сертификат автоматически | Владелец |

**Ориентировочно:** 200-400 руб/год (.ru), 30 минут на регистрацию + до 24 часов на DNS-распространение.

### Этап 1 — Подготовка переменных окружения

В `/home/aemr/dash/deploy/.env.production`:

```bash
# Раньше: DOMAIN=:80 (без HTTPS)
DOMAIN=dash.<выбранный-домен>.ru
WEBHOOK_DOMAIN=bot-feedback.<выбранный-домен>.ru
```

В `/home/aemr/aemr-bot/infra/.env` (новые ключи):

```bash
BOT_MODE=webhook
WEBHOOK_URL=https://bot-feedback.<выбранный-домен>.ru/max/webhook
WEBHOOK_SECRET=<генерация: openssl rand -hex 32>
WEBHOOK_LISTEN_HOST=0.0.0.0
```

### Этап 2 — Изменения в `Caddyfile` сайта `dash`

`/home/aemr/dash/deploy/Caddyfile`:

```caddy
# Существующий блок — без изменений
{$DOMAIN} {
    encode gzip
    handle /api/* { reverse_proxy server:3000 }
    handle { reverse_proxy web:80 }
    log { output stdout, format console }
}

# Новый блок — webhook бота. Изолированный домен, только POST /max/webhook,
# IP-allowlist (когда будут известны IP MAX-серверов), rate-limit.
{$WEBHOOK_DOMAIN} {
    encode gzip

    # Только POST на /max/webhook, ничего больше
    @bot-webhook {
        method POST
        path /max/webhook
    }

    handle @bot-webhook {
        # Прокси к боту в сети aemr-bot_default через external network bridge
        reverse_proxy aemr-bot-bot-1:8080 {
            header_up X-Real-IP {remote_host}
            header_up X-Forwarded-For {remote_host}
        }
    }

    # Всё остальное на этом домене → 404 без раскрытия
    handle { respond 404 }

    log { output stdout, format console }
}
```

### Этап 3 — Подключение бота к сети `aemr_aemr`

`/home/aemr/aemr-bot/infra/docker-compose.yml` — добавить external network:

```yaml
networks:
  default:
  aemr_external:
    external: true
    name: aemr_aemr

services:
  bot:
    networks:
      - default
      - aemr_external
    # listen на 0.0.0.0:8080 уже сделано в коде main.py:102-137
    ports:
      - "127.0.0.1:8080:8080"  # оставляем для healthz
```

### Этап 4 — Параноидальная защита

В `Caddyfile` блока webhook добавить:

```caddy
{$WEBHOOK_DOMAIN} {
    encode gzip

    # 1. IP-allowlist (когда известны IP MAX-серверов — спросить
    #    у MAX support@max.ru или посмотреть в логах после первого
    #    тестового запроса)
    @max-ip {
        remote_ip <IP-сети-MAX-серверов>
    }

    # 2. Защищённый webhook
    @bot-webhook {
        method POST
        path /max/webhook
        header X-Max-Secret <первые-8-символов-WEBHOOK_SECRET-для-фильтра>
    }

    # 3. Rate-limit (требует caddy-rate-limit плагин)
    rate_limit {
        zone bot_webhook {
            key {remote_ip}
            events 100
            window 1m
        }
    }

    handle @bot-webhook {
        reverse_proxy aemr-bot-bot-1:8080
    }

    handle { respond 404 }
}
```

**Слои защиты:**

1. **Caddy уровень**: только POST, только путь `/max/webhook`, только IP-сети MAX, rate limit 100/мин на IP, заголовок `X-Max-Secret` для дополнительной фильтрации
2. **Бот уровень**: HMAC-проверка `WEBHOOK_SECRET` через `hmac.compare_digest` (уже в `main.py:102-137`)
3. **Изоляция**: бот по-прежнему слушает только через Caddy-прокси; healthz endpoint остаётся на 127.0.0.1 (без Caddy)

### Этап 5 — Регистрация webhook у MAX

После запуска бота в режиме webhook вызвать (один раз):

```bash
ssh root@193.233.244.217 'su - aemr -c "cd /home/aemr/aemr-bot/infra && docker compose exec bot python -c \"
import asyncio
from aemr_bot.config import settings
from maxapi import Bot
async def main():
    bot = Bot(settings.bot_token)
    await bot.subscribe(settings.webhook_url)
    print(\\\"webhook registered\\\")
asyncio.run(main())
\""'
```

Сверить в логах Caddy и бота, что первый webhook-запрос пришёл и обработался.

### Этап 6 — Проверка работоспособности

```bash
# 1. Caddy получил Let's Encrypt
curl -I https://bot-feedback.<домен>.ru/  # ожидаем 404 (handle { respond 404 })

# 2. Webhook endpoint открыт только для POST
curl -X GET https://bot-feedback.<домен>.ru/max/webhook  # 404
curl -X POST https://bot-feedback.<домен>.ru/max/webhook  # 401 без секрета
curl -X POST -H "X-Max-Secret: <prefix>" https://bot-feedback.<домен>.ru/max/webhook  # 401 без HMAC, лог в боте

# 3. Бот в логах принимает
ssh root@193.233.244.217 'su - aemr -c "cd /home/aemr/aemr-bot/infra && docker compose logs --tail 20 bot | grep webhook"'

# 4. Тестовое обращение от живого жителя — должно прийти за <100ms
```

---

## Roll-back план (5 минут)

Если что-то идёт не так:

```bash
ssh root@193.233.244.217 << 'EOF'
# 1. Вернуть бот в polling
sed -i 's/BOT_MODE=webhook/BOT_MODE=polling/' /home/aemr/aemr-bot/infra/.env
su - aemr -c 'cd /home/aemr/aemr-bot/infra && docker compose restart bot'

# 2. Откатить Caddyfile (если нужно — только если внесли в site Caddy)
cd /home/aemr/dash/deploy
git checkout Caddyfile  # если под контролем git
docker compose -f docker-compose.yml --env-file .env.production restart caddy

# 3. Удалить webhook у MAX (опционально, polling его игнорирует)
docker compose -f /home/aemr/aemr-bot/infra/docker-compose.yml exec bot python -c "
import asyncio
from aemr_bot.config import settings
from maxapi import Bot
asyncio.run(Bot(settings.bot_token).unsubscribe(settings.webhook_url))
"
EOF
```

Roll-back протестирован мысленно: polling-handler в коде остаётся (см. `main.py:_run_polling`), переключение через `BOT_MODE=polling` без миграций.

---

## Open questions (требуют ответа от пользователя)

1. **Какой выбранный поддомен?** Без этого этапы 1-2 заблокированы.
2. **Готов ли остановить сайт `dash` на 5 минут для переконфигурации Caddy?** Альтернатива: внести правки и сделать `caddy reload` — без даунтайма, если Caddyfile валиден.
3. **IP-сети MAX-серверов известны?** Если нет — добавить IP-allowlist на этапе 5 после первого тестового запроса.
4. **Когда внедрять?** Параметры trigger:
   - Сейчас (даже при низком объёме) — для архитектурной чистоты
   - Когда объём ≥10/мин — практический trigger
   - Когда появится интеграция с Госуслугами — мандатный trigger

---

## Что НЕ делаем в этом плане

- Не публикуем бот через DNS на главном домене сайта (только отдельный поддомен)
- Не делаем второй edge-Caddy (конфликт по 443)
- Не переподключаем бот в основную сеть `aemr_aemr` без external-network паттерна (бот должен оставаться в своей сети как primary)
- Не открываем дополнительные порты наружу (только 443 уже открыт)
- Не убираем `127.0.0.1:8080:8080` для healthz — это нужно для самоконтроля бота из cron-задач
