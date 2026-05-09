#!/bin/bash
# Watchdog внешнего уровня для бота.
#
# Запускается из cron каждые 5 минут (см. SETUP.md → раздел установки).
# Проверяет, отвечает ли бот на /healthz, и:
#   1. После N последовательных провалов рестартует контейнер.
#   2. Если рестарт не помог — постит сообщение в служебную группу
#      через MAX bot API (curl), чтобы оператор увидел проблему даже
#      если внутренний pulse-job не сработал.
#
# Идея: docker уже держит контейнер `unless-stopped`, healthcheck
# маркирует unhealthy, но docker сам по себе не рестартует unhealthy
# контейнеры. Этот скрипт закрывает зазор «healthcheck говорит больно,
# но никто не лечит», и заодно делает out-of-band-уведомление через
# MAX, потому что внутренний pulse в этой ситуации тоже не работает.
#
# Зависимости: bash, curl, jq, docker compose. Все есть на Ubuntu 20.04
# при стандартной установке Docker Engine.

set -euo pipefail

ENV_FILE=/home/aemr/aemr-bot/infra/.env
COMPOSE_DIR=/home/aemr/aemr-bot/infra
HEALTH_URL=http://127.0.0.1:8080/healthz
STATE_DIR=/var/lib/aemr-bot-watchdog
STATE_FILE=$STATE_DIR/state
LOG_TAG=aemr-bot-watchdog

# Сколько последовательных провалов /healthz нужно для авто-рестарта
# контейнера (5 мин × 3 = 15 мин подряд недоступности).
MAX_FAILS_BEFORE_RESTART=3
# Сколько провалов до того, как ещё и пнуть оператора в админ-группу.
# Восемь = 40 минут общей недоступности — рестарт уже не сработал,
# нужно человеческое внимание.
MAX_FAILS_BEFORE_ALERT=8

mkdir -p "$STATE_DIR"
fails=$(cat "$STATE_FILE" 2>/dev/null || echo 0)

if curl -fsS --max-time 10 "$HEALTH_URL" >/dev/null 2>&1; then
    if [ "$fails" -gt 0 ]; then
        logger -t "$LOG_TAG" "recovered after $fails consecutive fails"
        echo 0 > "$STATE_FILE"
    fi
    exit 0
fi

fails=$((fails + 1))
echo "$fails" > "$STATE_FILE"
logger -t "$LOG_TAG" "/healthz unreachable (consecutive fails=$fails)"

if [ "$fails" -eq "$MAX_FAILS_BEFORE_RESTART" ]; then
    logger -t "$LOG_TAG" "auto-restart of bot container triggered"
    cd "$COMPOSE_DIR" && docker compose restart bot >/dev/null 2>&1 || \
        logger -t "$LOG_TAG" "docker compose restart failed"
fi

if [ "$fails" -ge "$MAX_FAILS_BEFORE_ALERT" ]; then
    BOT_TOKEN=$(grep -E '^BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d'=' -f2-)
    ADMIN_GROUP_ID=$(grep -E '^ADMIN_GROUP_ID=' "$ENV_FILE" | head -1 | cut -d'=' -f2-)
    if [ -z "${BOT_TOKEN:-}" ] || [ -z "${ADMIN_GROUP_ID:-}" ]; then
        logger -t "$LOG_TAG" "BOT_TOKEN or ADMIN_GROUP_ID empty in .env — alert skipped"
        exit 1
    fi
    minutes=$((fails * 5))
    text="⛑️ Бот не отвечает на /healthz уже ${minutes} минут. Авторестарт не помог. Проверьте на сервере: docker compose ps; docker compose logs --tail 200 bot."
    payload=$(jq -nc --arg t "$text" '{text: $t}')
    if curl -fsS --max-time 15 -X POST \
        "https://platform-api.max.ru/messages?access_token=${BOT_TOKEN}&chat_id=${ADMIN_GROUP_ID}" \
        -H 'Content-Type: application/json' \
        -d "$payload" >/dev/null 2>&1; then
        logger -t "$LOG_TAG" "alert posted to admin group (fails=$fails)"
    else
        logger -t "$LOG_TAG" "FAILED to post alert (fails=$fails)"
    fi
fi
