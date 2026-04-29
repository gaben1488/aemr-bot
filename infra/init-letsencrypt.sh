#!/usr/bin/env bash
# Bootstrap Let's Encrypt cert for the bot's webhook domain.
# Run once on the server, AFTER docker compose up has nginx running on :80.
# Usage:  DOMAIN=feedback.elizovomr.ru EMAIL=admin@elizovomr.ru ./init-letsencrypt.sh
set -euo pipefail

DOMAIN="${DOMAIN:?DOMAIN env var is required}"
EMAIL="${EMAIL:?EMAIL env var is required}"

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
