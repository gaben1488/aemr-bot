#!/bin/bash
# Watchdog внешнего уровня для бота.
#
# Запускается из cron каждые 5 минут (см. SETUP.md → раздел установки).
# Проверяет liveness бота и:
#   1. После N последовательных провалов /livez рестартует контейнер.
#   2. Если рестарт не помог — постит сообщение в служебную группу
#      через MAX bot API (curl), чтобы оператор увидел проблему даже
#      если внутренний pulse-job не сработал.
#
# ВАЖНО: рестарт управляется только /livez, а не /readyz и не /healthz.
# /readyz проверяет БД. Краткая проблема Postgres или Docker DNS не
# должна перезапускать живой polling-процесс и тем более не должна
# маскировать исходную причину новым restart-loop.
#
# Зависимости: bash, curl, jq, docker compose. Все есть на Ubuntu 20.04
# при стандартной установке Docker Engine.

set -euo pipefail

ENV_FILE=/home/aemr/aemr-bot/infra/.env
COMPOSE_DIR=/home/aemr/aemr-bot/infra
LIVE_URL=http://127.0.0.1:8080/livez
READY_URL=http://127.0.0.1:8080/readyz
STATE_DIR=/var/lib/aemr-bot-watchdog
STATE_FILE=$STATE_DIR/state
LOG_TAG=aemr-bot-watchdog

# Сколько последовательных провалов /livez нужно для авто-рестарта
# контейнера (5 мин × 3 = 15 мин подряд недоступности процесса/event-loop).
MAX_FAILS_BEFORE_RESTART=3
# Сколько провалов до того, как ещё и пнуть оператора в админ-группу.
# Восемь = 40 минут общей недоступности — рестарт уже не сработал,
# нужно человеческое внимание.
MAX_FAILS_BEFORE_ALERT=8

mkdir -p "$STATE_DIR"
fails=$(cat "$STATE_FILE" 2>/dev/null || echo 0)

if curl -fsS --max-time 10 "$LIVE_URL" >/dev/null 2>&1; then
    if [ "$fails" -gt 0 ]; then
        logger -t "$LOG_TAG" "liveness recovered after $fails consecutive fails"
        echo 0 > "$STATE_FILE"
    fi
    # Readiness проверяем только для диагностики. Никаких рестартов по
    # этой ветке: если БД временно недоступна, нужен DB-alert, а не kill
    # живого процесса.
    if ! curl -fsS --max-time 10 "$READY_URL" >/dev/null 2>&1; then
        logger -t "$LOG_TAG" "readiness degraded: /livez ok, /readyz failed (DB or dependency issue)"
    fi
    exit 0
fi

fails=$((fails + 1))
echo "$fails" > "$STATE_FILE"
logger -t "$LOG_TAG" "/livez unreachable (consecutive fails=$fails)"

if [ "$fails" -eq "$MAX_FAILS_BEFORE_RESTART" ]; then
    logger -t "$LOG_TAG" "auto-restart of bot container triggered by liveness failure"
    if ! (cd "$COMPOSE_DIR" && docker compose restart bot >/dev/null 2>&1); then
        logger -t "$LOG_TAG" "docker compose restart failed"
    fi
fi

if [ "$fails" -ge "$MAX_FAILS_BEFORE_ALERT" ]; then
    MAX_AUTH=$(awk -F= '$1=="BOT_TOKEN"{print substr($0,index($0,$2))}' "$ENV_FILE" | head -1)
    ADMIN_GROUP_ID=$(awk -F= '$1=="ADMIN_GROUP_ID"{print substr($0,index($0,$2))}' "$ENV_FILE" | head -1)
    if [ -z "${MAX_AUTH:-}" ] || [ -z "${ADMIN_GROUP_ID:-}" ]; then
        logger -t "$LOG_TAG" "BOT_TOKEN or ADMIN_GROUP_ID empty in .env — alert skipped"
        exit 1
    fi
    minutes=$((fails * 5))
    text="⛑️ Бот не отвечает на /livez уже ${minutes} минут. Авторестарт не помог. Проверьте на сервере: docker compose ps; docker compose logs --tail 200 bot. Для БД отдельно: curl -fsS http://127.0.0.1:8080/readyz."
    payload=$(jq -nc --arg t "$text" '{text: $t}')
    if curl -fsS --max-time 15 -X POST \
        "https://platform-api.max.ru/messages?chat_id=${ADMIN_GROUP_ID}" \
        -H "Authorization: ${MAX_AUTH}" \
        -H 'Content-Type: application/json' \
        -d "$payload" >/dev/null 2>&1; then
        logger -t "$LOG_TAG" "alert posted to admin group (fails=$fails)"
    else
        logger -t "$LOG_TAG" "FAILED to post alert (fails=$fails)"
    fi
fi
