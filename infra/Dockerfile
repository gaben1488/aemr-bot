FROM python:3.12-slim@sha256:4386a385d81dba9f72ed72a6fe4237755d7f5440c84b417650f38336bbc43117

# uv нужен только на этапе сборки — для `uv export` из bot/uv.lock в
# requirements.txt с SHA256-хешами. Берём пиннутый бинарь из официального
# образа astral-sh/uv (digest, не мутабельный тег) вместо
# `curl astral.sh/uv/install.sh | sh`: компромет домена иначе исполнялся бы
# в build-контексте. uv в финальный рантайм НЕ попадает (см. /usr/local/bin).
COPY --from=ghcr.io/astral-sh/uv:0.11.12@sha256:3a59a3cdd5f7c217faa36e32dbc7fddbb0412889c2a0a5229f6d790e5a019dd7 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kamchatka

RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client gnupg ca-certificates curl tzdata unzip rclone \
    && rm -rf /var/lib/apt/lists/*
# rclone берём из debian apt вместо `curl rclone.org/install.sh | bash`:
# любой будущий компромет домена rclone.org исполнялся бы как root в
# образе. Версия из apt чуть старше (1.60 на bookworm), но достаточна
# для S3-аплоада бэкапов; функциональность не урезана.

# Non-root user. Fixed UID/GID so the named volume `backups` keeps the
# same ownership across rebuilds and on hosts where bind-mounts are used.
RUN groupadd --system --gid 1000 botuser \
    && useradd --system --uid 1000 --gid 1000 --create-home --home-dir /home/botuser botuser

WORKDIR /app

# Hash-enforced зависимости. bot/uv.lock — источник истины (фиксированные
# версии + SHA256 каждого wheel'а). Раньше `pip install -e /app` ставил по
# диапазонам из pyproject БЕЗ проверки хешей: подмена пакета на зеркале или
# MITM прошли бы незаметно. Теперь:
#   1) uv export разворачивает lock в requirements.txt с --hash на каждый пакет;
#   2) pip --require-hashes отказывается ставить что-либо без совпадающего хеша
#      (fail-closed: один неверный хеш — build падает).
# --no-emit-project выкидывает сам aemr-bot (editable, ставится отдельно ниже).
# uv.lock НЕ редактируется здесь — только читается.
COPY bot/pyproject.toml /app/pyproject.toml
COPY bot/uv.lock /app/uv.lock
RUN pip install --upgrade pip \
    && uv export --frozen --no-emit-project --no-dev \
        --format requirements-txt -o /tmp/requirements.txt \
    && pip install --require-hashes --no-deps -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt /usr/local/bin/uv

COPY bot/aemr_bot /app/aemr_bot
# Сам пакет ставим editable БЕЗ зависимостей: весь дерево deps уже
# установлено хеш-верифицированным шагом выше, повторно тянуть с PyPI нечего.
RUN pip install -e /app --no-deps
COPY bot/alembic.ini /app/alembic.ini
COPY seed /app/seed
COPY docs/PRIVACY.pdf /app/seed/PRIVACY.pdf

# /backups is the named volume mount target. Pre-create it so chown runs
# at build time; compose will mount over it but ownership of the mount
# point stays for the volume.
RUN mkdir -p /backups && chown -R botuser:botuser /app /backups

ENV SEED_DIR=/app/seed

USER botuser

# exec: python становится PID 1 и получает SIGTERM от `compose stop|restart`.
# Без exec PID 1 = sh, SIGTERM до python не доходит → advisory-lock снимается
# только по reap соединения Postgres, и новый процесс не может стартовать
# (рестарт-цикл). ponytail: exec закрывает graceful-stop; жёсткий краш без
# FIN всё ещё держит лок до keepalive Postgres — добавить TCP-keepalive на
# lock-соединение, если станет проблемой.
CMD ["sh", "-c", "alembic upgrade head && exec python -m aemr_bot.main"]
