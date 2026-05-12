"""Кэшировать файловый токен политики конфиденциальности в settings,
чтобы загрузка выполнялась один раз."""

from __future__ import annotations

import logging
from pathlib import Path

from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import settings_store, uploads

log = logging.getLogger(__name__)

POLICY_PDF_REL = "PRIVACY.pdf"
# Это ключ записи в таблице settings, а не пароль.
POLICY_TOKEN_KEY = "policy_pdf_token"  # nosec B105

# Отображаемое жителю имя файла в чате MAX. На диске и в Dockerfile файл
# хранится латиницей (Docker buildkit не справляется с unicode в COPY,
# падает на CI). Чтобы житель получал документ под человеческим именем,
# при загрузке делаем временную копию с этим именем и загружаем её.
POLICY_PDF_DISPLAY_NAME = "Политика обработки персональных данных.pdf"


def _resolve_pdf_path() -> Path:
    return cfg.seed_dir / POLICY_PDF_REL


async def ensure_uploaded(bot, *, force: bool = False) -> str | None:
    async with session_scope() as session:
        token = await settings_store.get(session, POLICY_TOKEN_KEY)
    if token and not force:
        return token

    path = _resolve_pdf_path()
    if not path.exists():
        log.warning("policy PDF not found at %s — skipping upload", path)
        return None

    # Копируем во временный файл под русским именем и загружаем его.
    # MAX берёт имя из basename загружаемого файла, поэтому житель в
    # чате увидит «Политика обработки персональных данных.pdf», а не
    # «PRIVACY.pdf». Пишем в /tmp (он смонтирован как tmpfs внутри
    # контейнера, см. infra/docker-compose.yml).
    import shutil
    import tempfile

    tmp_dir = Path(tempfile.mkdtemp(prefix="policy_"))
    display_path = tmp_dir / POLICY_PDF_DISPLAY_NAME
    try:
        shutil.copyfile(path, display_path)
        token = await uploads.upload_path(bot, display_path)
    finally:
        try:
            display_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except Exception:
            log.debug("не удалось удалить временный файл политики", exc_info=True)

    if token is None:
        return None

    async with session_scope() as session:
        await settings_store.set_value(session, POLICY_TOKEN_KEY, token)
    log.info("policy PDF uploaded; token cached")
    return token


def build_file_attachment(token: str):
    return uploads.file_attachment(token)
