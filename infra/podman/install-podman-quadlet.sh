#!/usr/bin/env bash
# Установка aemr-bot как systemd-службы под Podman через Quadlet (Podman 4.4+).
# Запуск (rootful):  sudo bash infra/podman/install-podman-quadlet.sh
#
# Что делает: проверяет версию podman, собирает образ бота из Dockerfile, кладёт
# Quadlet-юниты (.network + .container) в /etc/containers/systemd/ с подстановкой
# реального пути репозитория, перегенерирует systemd-юниты и поднимает стек.
# Служба переживает отключение терминала (системный юнит) и стартует при загрузке.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
QUADLET_DIR="/etc/containers/systemd"
ENV_FILE="$REPO/infra/.env"

echo "== Репозиторий: $REPO =="

# 1. podman + поддержка Quadlet (>= 4.4)
if ! command -v podman >/dev/null 2>&1; then
  echo "ОШИБКА: podman не установлен."; exit 1
fi
PVER="$(podman --version | awk '{print $3}')"
PMAJ="${PVER%%.*}"; PREST="${PVER#*.}"; PMIN="${PREST%%.*}"
echo "Podman: $PVER"
if [ "$PMAJ" -lt 4 ] || { [ "$PMAJ" -eq 4 ] && [ "$PMIN" -lt 4 ]; }; then
  echo "ВНИМАНИЕ: Podman < 4.4 — Quadlet НЕ поддерживается (типично для Debian 12 bookworm)."
  echo "Варианты:"
  echo "  (1) fallback без Quadlet (тот же docker-compose.yml через podman compose):"
  echo "      sudo cp '$HERE/aemr-bot-compose.service' /etc/systemd/system/"
  echo "      sudo systemctl daemon-reload && sudo systemctl enable --now aemr-bot-compose.service"
  echo "  (2) поставить Podman 5 (OBS-репо kubic) и запустить этот скрипт снова."
  exit 1
fi

# 2. .env и DATABASE_URL (Quadlet, в отличие от compose, не выводит его из POSTGRES_*)
if [ ! -f "$ENV_FILE" ]; then
  echo "ОШИБКА: нет $ENV_FILE — скопируйте infra/.env.example в infra/.env и заполните."; exit 1
fi
if ! grep -q '^DATABASE_URL=' "$ENV_FILE"; then
  echo "ВНИМАНИЕ: в $ENV_FILE нет DATABASE_URL (compose выводил его сам, Quadlet — нет)."
  echo "Добавьте строку, подставив пароль из POSTGRES_PASSWORD:"
  echo "  DATABASE_URL=postgresql+asyncpg://aemr:ПАРОЛЬ@aemr-bot-db:5432/aemr"
  exit 1
fi

# 2b. Каталог персистентных логов на диске хоста (bot.log с ротацией).
# UID 1000 — пользователь botuser в образе; без chown read_only-контейнер
# не сможет писать. Логи переживают любой rm/пересборку контейнера.
LOG_DIR="/var/log/aemr-bot"
echo "== Каталог логов $LOG_DIR (владелец UID 1000) =="
mkdir -p "$LOG_DIR"
chown 1000:1000 "$LOG_DIR" || echo "ВНИМАНИЕ: chown $LOG_DIR не удался — проверьте права."

# 2c. Персистентный journald: контейнерный stdout (LogDriver=journald) должен
# писаться на диск и переживать перезагрузку. На Debian Storage=auto хранит
# журнал только если есть /var/log/journal — создаём, если нет.
if [ ! -d /var/log/journal ]; then
  echo "== Включаю персистентный journald (/var/log/journal) =="
  mkdir -p /var/log/journal
  systemctl restart systemd-journald || echo "ВНИМАНИЕ: перезапустите systemd-journald вручную."
fi

# 3. Сборка образа бота из Dockerfile
echo "== Сборка образа localhost/aemr-bot:latest =="
podman build -t localhost/aemr-bot:latest -f "$REPO/infra/Dockerfile" "$REPO"

# 4. Установка Quadlet-юнитов (с подстановкой реального пути репозитория)
echo "== Установка Quadlet-юнитов в $QUADLET_DIR =="
mkdir -p "$QUADLET_DIR"
for unit in aemr-bot.network aemr-bot-db.container aemr-bot.container; do
  sed "s#/home/aemr/aemr-bot#$REPO#g" "$HERE/$unit" > "$QUADLET_DIR/$unit"
  echo "  установлен: $unit"
done
echo "  (cntlm — только при прокси с NTLM: вручную скопируйте aemr-bot-cntlm.container)"

# 5. Регенерация юнитов из Quadlet + запуск (Requires поднимет и БД)
systemctl daemon-reload
echo "== Запуск aemr-bot.service =="
systemctl start aemr-bot.service

# 6. Проверка
sleep 6
systemctl --no-pager --plain status aemr-bot-db.service aemr-bot.service 2>&1 | head -24 || true
echo
echo "Готово. Полезные команды:"
echo "  systemctl status aemr-bot.service        # состояние"
echo "  podman ps                                 # контейнеры"
echo "  curl -s http://127.0.0.1:8080/readyz      # здоровье (ждём {\"ok\": true})"
echo "  journalctl -u aemr-bot.service -f         # логи бота (host-level, переживают rm)"
echo "  tail -f /var/log/aemr-bot/bot.log         # читаемый файл-лог приложения (с ротацией)"
echo "  systemctl restart aemr-bot.service        # перезапуск после пересборки образа"
