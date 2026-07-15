"""Кэшировать файловый токен политики конфиденциальности в settings,
чтобы загрузка выполнялась один раз."""

from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
from pathlib import Path

from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import settings_store, uploads

log = logging.getLogger(__name__)

POLICY_PDF_REL = "PRIVACY.pdf"
# Это ключ записи в таблице settings, а не пароль.
POLICY_TOKEN_KEY = "policy_pdf_token"  # nosec B105
# SHA-256 загруженного PDF. Кэшируем рядом с токеном, чтобы перезаливать файл
# ТОЛЬКО при его смене: без этого после обновления seed/PRIVACY.pdf в БД остаётся
# старый токен, и житель получает предыдущую версию политики. Тоже ключ, не секрет.
POLICY_HASH_KEY = "policy_pdf_sha256"  # nosec B105

# Отображаемое жителю имя файла в чате MAX. На диске и в Dockerfile файл
# хранится латиницей (Docker buildkit не справляется с unicode в COPY,
# падает на CI). Чтобы житель получал документ под человеческим именем,
# при загрузке делаем временную копию с этим именем и загружаем её.
POLICY_PDF_DISPLAY_NAME = "Политика обработки персональных данных.pdf"


def _resolve_pdf_path() -> Path:
    return cfg.seed_dir / POLICY_PDF_REL


def _pdf_sha256(path: Path) -> str:
    """SHA-256 файла политики — маркер «этот PDF уже загружен» для кэша токена."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


async def ensure_uploaded(bot, *, force: bool = False) -> str | None:
    path = _resolve_pdf_path()
    if not path.exists():
        log.warning("policy PDF not found at %s — skipping upload", path)
        async with session_scope() as session:
            # PDF нет, но токен мог быть закэширован раньше — отдаём его,
            # старый файл лучше, чем «политика недоступна».
            return await settings_store.get(session, POLICY_TOKEN_KEY)

    current_hash = _pdf_sha256(path)
    async with session_scope() as session:
        cached_token = await settings_store.get(session, POLICY_TOKEN_KEY)
        stored_hash = await settings_store.get(session, POLICY_HASH_KEY)

    # Кэш валиден, только если PDF не менялся с прошлой загрузки. Обновили
    # seed/PRIVACY.pdf → хэш другой → перезаливаем и обновляем токен, иначе
    # житель получал бы старую версию политики по устаревшему токену.
    if cached_token and stored_hash == current_hash and not force:
        return cached_token

    # Копируем во временный файл под русским именем и загружаем его.
    # MAX берёт имя из basename загружаемого файла, поэтому житель в
    # чате увидит «Политика обработки персональных данных.pdf», а не
    # «PRIVACY.pdf». Пишем в /tmp (он смонтирован как tmpfs внутри
    # контейнера, см. infra/docker-compose.yml).
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
        return cached_token  # загрузка не удалась — оставляем прежний токен, если был

    async with session_scope() as session:
        await settings_store.set_value(session, POLICY_TOKEN_KEY, token)
        await settings_store.set_value(session, POLICY_HASH_KEY, current_hash)
    log.info("policy PDF uploaded; token cached (sha256=%s…)", current_hash[:12])
    return token


def build_file_attachment(token: str):
    return uploads.file_attachment(token)
