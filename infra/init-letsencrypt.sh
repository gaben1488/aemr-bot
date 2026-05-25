#!/usr/bin/env bash
# Bootstrap Let's Encrypt cert for the bot's webhook domain.
# Run once on the server, AFTER docker compose up has nginx running on :80.
# Usage:  DOMAIN=feedback.elizovomr.ru EMAIL=admin@elizovomr.ru ./init-letsencrypt.sh
set -euo pipefail

DOMAIN="${DOMAIN:?DOMAIN env var is required}"
EMAIL="${EMAIL:?EMAIL env var is required}"

# SECURITY_REVIEW M7 (CVSS 5.3): защита от shell-injection через env.
# Скрипт ручной, но если оператор по ошибке вставит `DOMAIN="x.ru; rm -rf /etc"`
# или похожее — `--entrypoint "..."` выполнит как shell-команду внутри
# certbot контейнера. Валидируем строго: домен — letters/digits/dots/dashes;
# email — RFC-простой паттерн.
if ! [[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
    echo "DOMAIN has unexpected characters; aborting (security guard)." >&2
    exit 2
fi
if ! [[ "$EMAIL" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
    echo "EMAIL has unexpected format; aborting (security guard)." >&2
    exit 2
fi

cd "$(dirname "$0")"
mkdir -p certbot/conf certbot/www

echo "Requesting cert for $DOMAIN ..."
docker compose run --rm --entrypoint "\
  certbot certonly --webroot -w /var/www/certbot \
    --email $EMAIL --agree-tos --no-eff-email \
    --cert-name feedback \
    -d $DOMAIN" certbot

echo "Reloading nginx ..."
docker compose exec nginx nginx -s reload

echo "Done. Cert at certbot/conf/live/feedback/fullchain.pem"
