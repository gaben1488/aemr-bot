FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kamchatka

RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client gnupg ca-certificates curl tzdata unzip \
    && curl https://rclone.org/install.sh | bash \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY bot/pyproject.toml /app/pyproject.toml
RUN pip install --upgrade pip && pip install -e /app

COPY bot/aemr_bot /app/aemr_bot
COPY bot/alembic.ini /app/alembic.ini
COPY seed /app/seed
COPY docs/PRIVACY.pdf /app/seed/PRIVACY.pdf

ENV SEED_DIR=/app/seed

CMD ["sh", "-c", "alembic upgrade head && python -m aemr_bot.main"]
