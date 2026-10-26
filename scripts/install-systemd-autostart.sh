#!/usr/bin/env bash
# Установить systemd-юнит aemr-bot для автозапуска при перезагрузке VPS.
#
# Идемпотентный скрипт: можно запускать несколько раз, ничего не
# сломается. Если юнит уже установлен — просто перечитает конфиг.
#
# Использование на VPS:
#   ssh aemr@193.233.244.217
#   cd /home/aemr/aemr-bot
#   sudo bash scripts/install-systemd-autostart.sh
#
# После установки проверьте автозапуск:
#   sudo systemctl status aemr-bot.service
#   sudo systemctl is-enabled aemr-bot.service     # должно быть `enabled`
#
# Чтобы убедиться что всё работает — перезагрузите сервер и проверьте:
#   sudo reboot
#   # подождите минуту, потом снова ssh:
#   docker compose -f /home/aemr/aemr-bot/infra/docker-compose.yml ps
#
# Откат (отключить автозапуск):
#   sudo systemctl disable aemr-bot.service
#   sudo systemctl stop aemr-bot.service
#   sudo rm /etc/systemd/system/aemr-bot.service
#   sudo systemctl daemon-reload

set -euo pipefail

REPO_DIR="${REPO_DIR:-/home/aemr/aemr-bot}"
UNIT_SRC="$REPO_DIR/infra/aemr-bot.service"
UNIT_DST="/etc/systemd/system/aemr-bot.service"

if [ "$EUID" -ne 0 ]; then
    echo "Скрипт нужно запускать через sudo: sudo bash $0" >&2
    exit 1
fi

if [ ! -f "$UNIT_SRC" ]; then
    echo "Не найден исходный юнит: $UNIT_SRC" >&2
    echo "Убедитесь, что репозиторий клонирован в $REPO_DIR" >&2
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "docker не установлен. Сначала поставьте Docker по инструкции в docs/SETUP.md" >&2
    exit 1
fi

if ! systemctl is-enabled docker.service >/dev/null 2>&1; then
    echo "ВНИМАНИЕ: docker.service не enabled. Включаю..."
    systemctl enable docker.service
fi

echo "==> Копирую юнит в $UNIT_DST"
# Render the selected checkout path rather than silently starting the default one.
# Escape systemd quoted values (backslash, quote, specifier percent).
UNIT_WORKDIR="${REPO_DIR//\\/\\\\}/infra"
UNIT_WORKDIR="${UNIT_WORKDIR//\"/\\\"}"
UNIT_WORKDIR="${UNIT_WORKDIR//%/%%}"
while IFS= read -r line; do
    if [[ "$line" == WorkingDirectory=* ]]; then
        printf 'WorkingDirectory="%s"\n' "$UNIT_WORKDIR"
    else
        printf '%s\n' "$line"
    fi
done < "$UNIT_SRC" > "$UNIT_DST"
chmod 0644 "$UNIT_DST"

echo "==> Перечитываю systemd"
systemctl daemon-reload

echo "==> Включаю автозапуск aemr-bot.service"
systemctl enable aemr-bot.service

echo "==> Запускаю прямо сейчас (если ещё не запущен)"
systemctl start aemr-bot.service

echo ""
echo "Готово. Бот будет автоматически подниматься после reboot."
echo ""
echo "Проверка состояния:"
echo "  systemctl status aemr-bot.service"
echo ""
echo "Логи юнита:"
echo "  journalctl -u aemr-bot.service -n 50"
echo ""
echo "Проверка что контейнеры реально работают:"
echo "  docker compose -f $REPO_DIR/infra/docker-compose.yml ps"
