#!/usr/bin/env bash
# Безопасный аудит VPS для aemr-bot.
# Скрипт собирает технический отчёт без вывода секретов из .env.

set -u

PROJECT_DIR="${PROJECT_DIR:-/home/aemr/aemr-bot}"
OUT_DIR="${OUT_DIR:-/tmp}"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="$OUT_DIR/aemr_audit_${STAMP}.log"
ARCHIVE="$OUT_DIR/aemr_audit_${STAMP}.tar.gz"

mask() {
  sed -E \
    -e 's/(BOT_TOKEN=).*/\1***HIDDEN***/' \
    -e 's/(DATABASE_URL=).*/\1***HIDDEN***/' \
    -e 's/(POSTGRES_PASSWORD=).*/\1***HIDDEN***/' \
    -e 's/(POSTGRES_USER=).*/\1***HIDDEN***/' \
    -e 's/(BACKUP_GPG_PASSPHRASE=).*/\1***HIDDEN***/' \
    -e 's/(BACKUP_S3_ACCESS_KEY=).*/\1***HIDDEN***/' \
    -e 's/(BACKUP_S3_SECRET_KEY=).*/\1***HIDDEN***/' \
    -e 's/(WEBHOOK_SECRET=).*/\1***HIDDEN***/' \
    -e 's/(Authorization: )[A-Za-z0-9._~+\/-]+/\1***HIDDEN***/g' \
    -e 's/(access_token=)[A-Za-z0-9._~+\/-]+/\1***HIDDEN***/g'
}

section() {
  echo
  echo "===== $1 ====="
}

run() {
  echo "+ $*"
  "$@" 2>&1 || true
}

{
section "AUDIT INFO"
echo "Generated at: $(date)"
echo "Project dir: $PROJECT_DIR"
echo "Output log: $OUT"

section "TIME"
run date
run timedatectl

section "SYSTEM"
run hostnamectl
run uptime
run free -h
run df -h
run df -i

section "OS UPDATES"
apt list --upgradable 2>/dev/null | head -120 || true

section "PROJECT GIT"
if [ -d "$PROJECT_DIR/.git" ]; then
  cd "$PROJECT_DIR" || exit 1
  run pwd
  run git branch --show-current
  run git log -10 --oneline
  echo "+ git status --short"
  git status --short || true
else
  echo "Project git directory not found: $PROJECT_DIR"
fi

section "FILE TREE"
if [ -d "$PROJECT_DIR" ]; then
  cd "$PROJECT_DIR" || exit 1
  find . -maxdepth 4 -type f \
    | sort \
    | grep -vE '/(\.git|__pycache__|\.pytest_cache|\.venv|\.mypy_cache|\.ruff_cache)/' \
    | head -500
fi

section "ENV KEYS MASKED"
if [ -f "$PROJECT_DIR/infra/.env" ]; then
  grep -E '^[A-Z0-9_]+=' "$PROJECT_DIR/infra/.env" | mask
else
  echo "infra/.env not found"
fi

section "DOCKER VERSION"
run docker --version
run docker compose version

section "COMPOSE CONFIG SERVICES"
if [ -d "$PROJECT_DIR/infra" ]; then
  cd "$PROJECT_DIR/infra" || exit 1
  docker compose config --services 2>/dev/null || true
else
  echo "infra directory not found"
fi

section "COMPOSE PS"
if [ -d "$PROJECT_DIR/infra" ]; then
  cd "$PROJECT_DIR/infra" || exit 1
  docker compose ps || true
fi

section "BOT INSPECT"
docker inspect aemr-bot-bot-1 \
  --format 'Name={{.Name}} Status={{.State.Status}} Health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} RestartCount={{.RestartCount}} StartedAt={{.State.StartedAt}} RestartPolicy={{.HostConfig.RestartPolicy.Name}} Image={{.Image}}' \
  2>/dev/null || true

section "DB INSPECT"
docker inspect aemr-bot-db-1 \
  --format 'Name={{.Name}} Status={{.State.Status}} Health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} RestartCount={{.RestartCount}} StartedAt={{.State.StartedAt}} RestartPolicy={{.HostConfig.RestartPolicy.Name}} Image={{.Image}}' \
  2>/dev/null || true

section "HEALTHZ"
curl -fsS -m 5 http://127.0.0.1:8080/healthz || true
echo

section "BOT LOGS IMPORTANT 24H"
if [ -d "$PROJECT_DIR/infra" ]; then
  cd "$PROJECT_DIR/infra" || exit 1
  docker compose logs --since "24h" bot 2>/dev/null \
    | grep -Ei "error|exception|traceback|failed|timeout|429|unhealthy|startup-pulse|pulse|health-selfcheck|misfire|missed|Scheduler|restart|preflight|Starting" \
    | tail -500 \
    | mask || true
fi

section "BOT LOGS LAST 300"
if [ -d "$PROJECT_DIR/infra" ]; then
  cd "$PROJECT_DIR/infra" || exit 1
  docker compose logs --tail=300 bot 2>/dev/null | mask || true
fi

section "DB LOGS LAST 150"
if [ -d "$PROJECT_DIR/infra" ]; then
  cd "$PROJECT_DIR/infra" || exit 1
  docker compose logs --tail=150 db 2>/dev/null | mask || true
fi

section "DOCKER DISK"
run docker system df

section "BACKUPS"
if [ -d "$PROJECT_DIR/infra" ]; then
  cd "$PROJECT_DIR/infra" || exit 1
  docker compose exec -T bot sh -lc 'ls -lah /backups 2>/dev/null || true' || true
fi

section "PYTHON AND TEST CONFIG"
if [ -d "$PROJECT_DIR/bot" ]; then
  cd "$PROJECT_DIR/bot" || exit 1
  run python3 --version
  if [ -f pyproject.toml ]; then
    sed -n '1,220p' pyproject.toml
  fi
fi

section "RECENT GITHUB-RELATED FILES"
if [ -d "$PROJECT_DIR/.github" ]; then
  find "$PROJECT_DIR/.github" -maxdepth 3 -type f -print -exec sed -n '1,220p' {} \;
fi

section "DONE"
echo "Log: $OUT"
echo "Archive: $ARCHIVE"
} > "$OUT" 2>&1

# Создаём архив с одним log-файлом. Это удобнее скачивать из панели/VNC.
tar -czf "$ARCHIVE" -C "$(dirname "$OUT")" "$(basename "$OUT")" 2>/dev/null || true

printf '\nГотово. Отчёт сохранён здесь:\n%s\n\nАрхив для скачивания:\n%s\n\nПоказать отчёт в терминале:\ncat %s\n\n' "$OUT" "$ARCHIVE" "$OUT"
