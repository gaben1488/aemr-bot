#!/bin/bash
# Auto-deploy на сервере: тащит изменения с github и пересобирает контейнер,
# только если есть новые коммиты в origin/main.
#
# Запускается из cron от root каждые 10 минут (или ручным `bash auto-deploy.sh`).
# Требует деплой-ключа в /root/.ssh/aemr-bot-deploy с pubkey в GitHub Settings →
# Deploy keys (read-only) репозитория gaben1488/aemr-bot.
#
# Идемпотентен: если ничего не изменилось — выходит, контейнер не трогает.
# Лог пишется в /var/log/aemr-bot-deploy.log с ротацией через journald (logger).
#
# Health-gate с автоматическим rollback:
# Если новый образ не отвечает на /livez через 60 секунд после старта,
# скрипт автоматически откатывается на предыдущий коммит (PREV_LOCAL) и
# пересобирает. /readyz диагностирует БД, но НЕ управляет rollback: краткая
# проблема Postgres не должна откатывать валидный код.

set -euo pipefail

REPO_DIR=/home/aemr/aemr-bot
COMPOSE_DIR=$REPO_DIR/infra
SSH_KEY=/root/.ssh/aemr-bot-deploy
LOG_TAG=aemr-bot-deploy
HEALTH_TIMEOUT_SEC=60
HEALTH_POLL_INTERVAL_SEC=5
LIVE_URL=http://127.0.0.1:8080/livez
READY_URL=http://127.0.0.1:8080/readyz

# Git с явным ssh-ключом (deploy-key) — даже если у root есть свой github-ключ,
# для этого репо берём именно deploy-key (минимум прав).
export GIT_SSH_COMMAND="ssh -i $SSH_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

cd "$REPO_DIR"

# fetch без чекаута; проверяем разницу с remote
git fetch origin main --quiet || {
    logger -t "$LOG_TAG" "git fetch failed"
    exit 1
}

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    # Ничего не изменилось — выходим тихо
    exit 0
fi

logger -t "$LOG_TAG" "new commits: $LOCAL → $REMOTE, redeploying"

# Запоминаем предыдущий рабочий коммит — пригодится для rollback'а.
PREV_LOCAL=$LOCAL

# Сохраняем .env (в git его нет, при reset --hard не пострадает,
# но на всякий случай делаем копию)
cp "$COMPOSE_DIR/.env" /tmp/aemr-bot.env.bak.$$

git reset --hard "$REMOTE" --quiet
chown -R aemr:aemr "$REPO_DIR"

# Self-sync wrapper'ов в /usr/local/bin. Без этого блока копии,
# установленные install-auto-deploy.sh ОДИН РАЗ, застывают на версии
# момента установки: git подтягивает свежий scripts/*.sh в репо, но
# cron продолжает запускать старый код из /usr/local/bin. Именно так
# на сервере месяцами жил watchdog, который бил по /healthz и слал
# алерты с устаревшим `access_token=` в query (MAX давно требует
# Authorization header). Теперь каждый деплой переустанавливает оба
# wrapper'а из репо — расхождение кода и прода невозможно.
install -m 0755 "$REPO_DIR/scripts/auto-deploy.sh" /usr/local/bin/aemr-bot-autodeploy
install -m 0755 "$REPO_DIR/scripts/healthwatch.sh" /usr/local/bin/aemr-bot-healthwatch

# Пересборка от пользователя aemr (он в docker-group)
su - aemr -c "cd $COMPOSE_DIR && docker compose up -d --build" 2>&1 | logger -t "$LOG_TAG"

# Health-gate: ждём до HEALTH_TIMEOUT_SEC секунд /livez=200.
# Опрашиваем каждые HEALTH_POLL_INTERVAL_SEC, чтобы не «висеть» все 60.
healthy=0
elapsed=0
while [ "$elapsed" -lt "$HEALTH_TIMEOUT_SEC" ]; do
    if curl -fsS --max-time 5 "$LIVE_URL" >/dev/null 2>&1; then
        healthy=1
        break
    fi
    sleep "$HEALTH_POLL_INTERVAL_SEC"
    elapsed=$((elapsed + HEALTH_POLL_INTERVAL_SEC))
done

if [ "$healthy" -eq 1 ]; then
    logger -t "$LOG_TAG" "deploy ok: $REMOTE livez healthy после ${elapsed}s"
    if ! curl -fsS --max-time 5 "$READY_URL" >/dev/null 2>&1; then
        logger -t "$LOG_TAG" "deploy warning: /livez ok, но /readyz failed — проверьте БД/зависимости без rollback"
    fi
    rm -f /tmp/aemr-bot.env.bak.$$
    exit 0
fi

# Health-gate провалился — auto-rollback на предыдущий коммит.
logger -t "$LOG_TAG" "DEPLOY FAILED: /livez unreachable за ${HEALTH_TIMEOUT_SEC}s, ROLLBACK на $PREV_LOCAL"
git reset --hard "$PREV_LOCAL" --quiet
chown -R aemr:aemr "$REPO_DIR"
# Wrapper'ы тоже откатываем к PREV_LOCAL — иначе на сервере останется
# свежий код деплой-скрипта, а репо на старом коммите: рассинхрон.
install -m 0755 "$REPO_DIR/scripts/auto-deploy.sh" /usr/local/bin/aemr-bot-autodeploy
install -m 0755 "$REPO_DIR/scripts/healthwatch.sh" /usr/local/bin/aemr-bot-healthwatch
su - aemr -c "cd $COMPOSE_DIR && docker compose up -d --build" 2>&1 | logger -t "$LOG_TAG"

# Дать предыдущему коммиту 30 сек подняться. Мы этому не верим в смысле
# health-check (если предыдущий тоже сломан, нам некуда откатываться) —
# просто фиксируем факт и возвращаем non-zero, чтобы дежурный получил
# алерт через journald.
sleep 30
if curl -fsS --max-time 5 "$LIVE_URL" >/dev/null 2>&1; then
    logger -t "$LOG_TAG" "ROLLBACK ok: вернулись на $PREV_LOCAL"
    if ! curl -fsS --max-time 5 "$READY_URL" >/dev/null 2>&1; then
        logger -t "$LOG_TAG" "ROLLBACK warning: /livez ok, но /readyz failed — причина вероятно в БД/зависимостях"
    fi
else
    logger -t "$LOG_TAG" "ROLLBACK FAILED: предыдущий коммит тоже не отвечает на /livez, требуется ручное вмешательство"
fi
exit 1
