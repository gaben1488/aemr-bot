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

set -euo pipefail

REPO_DIR=/home/aemr/aemr-bot
COMPOSE_DIR=$REPO_DIR/infra
SSH_KEY=/root/.ssh/aemr-bot-deploy
LOG_TAG=aemr-bot-deploy

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

# Сохраняем .env (в git его нет, при reset --hard не пострадает,
# но на всякий случай делаем копию)
cp "$COMPOSE_DIR/.env" /tmp/aemr-bot.env.bak.$$

git reset --hard "$REMOTE" --quiet
chown -R aemr:aemr "$REPO_DIR"

# Project name закрепляется в первой строке docker-compose.yml — после
# git reset она восстанавливается из репо где её НЕТ. Подставляем заново.
if ! head -1 "$COMPOSE_DIR/docker-compose.yml" | grep -q "^name: aemr-bot"; then
    sed -i '1i name: aemr-bot\n' "$COMPOSE_DIR/docker-compose.yml"
fi

# Пересборка от пользователя aemr (он в docker-group)
su - aemr -c "cd $COMPOSE_DIR && docker compose up -d --build" 2>&1 | logger -t "$LOG_TAG"

# Проверка healthcheck через 30 сек
sleep 30
if curl -fsS --max-time 10 http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
    logger -t "$LOG_TAG" "deploy ok: $REMOTE healthy"
    rm -f /tmp/aemr-bot.env.bak.$$
else
    logger -t "$LOG_TAG" "DEPLOY FAILED: /healthz unreachable after 30s, manual check required"
    # Не делаем автоматический rollback: лучше человек посмотрит логи и решит.
    exit 1
fi
