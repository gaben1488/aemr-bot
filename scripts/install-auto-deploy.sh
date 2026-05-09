#!/bin/bash
# Однократная установка auto-deploy на сервере.
#
# Запуск:
#   sudo bash /home/aemr/aemr-bot/scripts/install-auto-deploy.sh
#
# Что делает:
#   1. Генерирует SSH deploy-key /root/.ssh/aemr-bot-deploy (если ещё нет).
#   2. Печатает pubkey, который нужно добавить в GitHub Settings →
#      Deploy keys репозитория gaben1488/aemr-bot, allow write off.
#   3. Конвертирует /home/aemr/aemr-bot/.git/config на ssh-remote
#      (https → git@github.com:gaben1488/aemr-bot.git).
#   4. Ставит scripts/auto-deploy.sh в /usr/local/bin/aemr-bot-autodeploy
#      и cron-задачу root каждые 10 минут.
#
# Удаление:
#   sudo crontab -l | grep -v aemr-bot-autodeploy | sudo crontab -
#   sudo rm /usr/local/bin/aemr-bot-autodeploy /root/.ssh/aemr-bot-deploy*

set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Запускайте под root (sudo bash $0)" >&2
    exit 1
fi

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
KEY=/root/.ssh/aemr-bot-deploy

if [ ! -f "$KEY" ]; then
    mkdir -p /root/.ssh && chmod 700 /root/.ssh
    ssh-keygen -t ed25519 -f "$KEY" -N "" -C "aemr-bot-deploy@$(hostname)" -q
    echo "→ deploy-key создан: $KEY"
else
    echo "→ deploy-key уже есть: $KEY"
fi

echo
echo "===== ДОБАВЬТЕ ЭТОТ PUBLIC KEY В GITHUB ====="
echo "https://github.com/gaben1488/aemr-bot/settings/keys/new"
echo "Title: aemr-bot-server-$(hostname)"
echo "Allow write access: НЕ ВКЛЮЧАТЬ (read-only)"
echo
cat "$KEY.pub"
echo
echo "============================================="
echo
read -p "Добавили pubkey в GitHub? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Прервано. Запустите скрипт повторно после добавления pubkey."
    exit 0
fi

# Конвертируем remote на ssh
cd /home/aemr/aemr-bot
sudo -u aemr git remote set-url origin git@github.com:gaben1488/aemr-bot.git
echo "→ git remote переключён на ssh"

# Тестовый fetch
echo "→ тестовый git fetch с deploy-key..."
GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
    sudo -u aemr git fetch origin main --quiet && \
    echo "  fetch ok" || {
    echo "  FAIL — pubkey не добавлен или нет доступа. Проверьте GitHub Deploy keys."
    exit 1
}

# Установка cron
install -m 0755 "$REPO_ROOT/scripts/auto-deploy.sh" /usr/local/bin/aemr-bot-autodeploy
LINE="*/10 * * * * /usr/local/bin/aemr-bot-autodeploy"
TMP=$(mktemp)
crontab -l 2>/dev/null | grep -v "aemr-bot-autodeploy" > "$TMP" || true
echo "$LINE" >> "$TMP"
crontab "$TMP"
rm "$TMP"

echo "→ cron-задача root: $LINE"
echo
echo "Готово. Каждый push в main будет применён в течение 10 минут."
echo "Логи: journalctl -t aemr-bot-deploy -n 50"
echo "Ручной запуск: sudo /usr/local/bin/aemr-bot-autodeploy"
