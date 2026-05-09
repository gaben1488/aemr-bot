#!/bin/bash
# Однократная установка внешнего watchdog-скрипта `healthwatch.sh`
# в /usr/local/bin и постановка cron-задачи root для запуска каждые
# 5 минут.
#
# Запускать на сервере один раз после первого `docker compose up -d`.
# Скрипт идемпотентен: повторный запуск не плодит дубликаты в crontab.
#
# Использование:
#   sudo bash /home/aemr/aemr-bot/scripts/install-healthwatch.sh
#
# Для удаления:
#   sudo crontab -l | grep -v aemr-bot-healthwatch | sudo crontab -
#   sudo rm /usr/local/bin/aemr-bot-healthwatch /var/lib/aemr-bot-watchdog/state

set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Запускайте под root (sudo bash $0)" >&2
    exit 1
fi

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
SOURCE=$REPO_ROOT/scripts/healthwatch.sh
TARGET=/usr/local/bin/aemr-bot-healthwatch

install -m 0755 "$SOURCE" "$TARGET"
echo "→ установлен $TARGET"

# Cron — каждые 5 минут. Логи пишутся через `logger`, видны в
# `journalctl -t aemr-bot-watchdog`.
LINE="*/5 * * * * $TARGET"
TMP=$(mktemp)
crontab -l 2>/dev/null | grep -v "aemr-bot-healthwatch" > "$TMP" || true
echo "$LINE" >> "$TMP"
crontab "$TMP"
rm "$TMP"
echo "→ cron-задача root: $LINE"

mkdir -p /var/lib/aemr-bot-watchdog
echo 0 > /var/lib/aemr-bot-watchdog/state
echo "→ state-каталог /var/lib/aemr-bot-watchdog инициализирован"

echo
echo "Готово. Для просмотра логов watchdog:"
echo "  journalctl -t aemr-bot-watchdog -n 50"
echo "Тестовый запуск:"
echo "  sudo $TARGET && echo OK"
