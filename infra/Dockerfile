FROM python:3.12-slim@sha256:4386a385d81dba9f72ed72a6fe4237755d7f5440c84b417650f38336bbc43117

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kamchatka

RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client gnupg ca-certificates curl tzdata unzip \
    && curl https://rclone.org/install.sh | bash \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. Fixed UID/GID so the named volume `backups` keeps the
# same ownership across rebuilds and on hosts where bind-mounts are used.
RUN groupadd --system --gid 1000 botuser \
    && useradd --system --uid 1000 --gid 1000 --create-home --home-dir /home/botuser botuser

WORKDIR /app

COPY bot/pyproject.toml /app/pyproject.toml
RUN pip install --upgrade pip && pip install -e /app

COPY bot/aemr_bot /app/aemr_bot
COPY bot/alembic.ini /app/alembic.ini
COPY seed /app/seed
COPY docs/PRIVACY.pdf /app/seed/PRIVACY.pdf

# /backups is the named volume mount target. Pre-create it so chown runs
# at build time; compose will mount over it but ownership of the mount
# point stays for the volume.
RUN mkdir -p /backups && chown -R botuser:botuser /app /backups

ENV SEED_DIR=/app/seed

USER botuser

CMD ["sh", "-c", "alembic upgrade head && python -m aemr_bot.main"]
