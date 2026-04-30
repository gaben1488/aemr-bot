from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from maxapi import Bot, Dispatcher

from aemr_bot import health
from aemr_bot.config import settings
from aemr_bot.db.session import session_scope
from aemr_bot.handlers import register_handlers
from aemr_bot.handlers.appeal import recover_stuck_funnels
from aemr_bot.services import cron as cron_service
from aemr_bot.services import policy as policy_service
from aemr_bot.services import settings_store

log = logging.getLogger("aemr_bot")

bot = Bot(settings.bot_token)
dp = Dispatcher()
register_handlers(dp)


def _install_polling_timeout(bot: Bot, timeout: int) -> None:
    """Pin the long-poll timeout used by Dispatcher.start_polling.

    maxapi calls bot.get_updates(marker=...) without timeout, falling back to
    the server default. We override the bound method on this instance so every
    GetUpdates request carries our timeout — which controls how long MAX holds
    the request when there are no events. Tuning this trades empty-round-trip
    rate against rate-limit headroom; see settings.polling_timeout_seconds.
    """
    original = bot.get_updates

    async def get_updates_with_timeout(*args, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return await original(*args, **kwargs)

    bot.get_updates = get_updates_with_timeout  # type: ignore[method-assign]


if settings.bot_mode == "polling":
    _install_polling_timeout(bot, settings.polling_timeout_seconds)


async def _seed_settings():
    async with session_scope() as session:
        await settings_store.seed_if_empty(session)


def _build_admin_senders(bot: Bot):
    from aemr_bot.services import uploads

    async def send_admin_text(text: str):
        if not settings.admin_group_id:
            return
        await bot.send_message(chat_id=settings.admin_group_id, text=text)

    async def send_admin_document(filename: str, content: bytes, caption: str = ""):
        if not settings.admin_group_id:
            return
        token = await uploads.upload_bytes(bot, content, suffix=Path(filename).suffix or ".bin")
        if token is None:
            await send_admin_text(
                f"{caption}\n(файл {filename} — загрузка не удалась, см. логи)"
            )
            return
        await bot.send_message(
            chat_id=settings.admin_group_id,
            text=caption or filename,
            attachments=[uploads.file_attachment(token)],
        )

    return send_admin_text, send_admin_document


# Webhook handler — registered at module load so dp.init_serve() picks it up.
# Per Макс.docx section 12 (Quick Start Python webhook):
#   from maxapi.methods.types.getted_updates import process_update_webhook
#   @dp.webhook_post('/...') → returns 2xx, then dp.handle(event) processes it.
if settings.bot_mode == "webhook":
    from fastapi import Request
    from fastapi.responses import JSONResponse

    try:
        from maxapi.methods.types.getted_updates import process_update_webhook
    except ImportError:
        process_update_webhook = None  # type: ignore[assignment]

    @dp.webhook_post("/max/webhook")
    async def _max_webhook(request: Request):
        if settings.webhook_secret:
            got = request.headers.get("X-Max-Secret") or request.query_params.get("secret")
            if got != settings.webhook_secret:
                return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            event_json = await request.json()
            if process_update_webhook is not None:
                event_object = await process_update_webhook(event_json=event_json, bot=bot)

                async def _handle():
                    try:
                        await dp.handle(event_object)
                    except Exception:
                        log.exception("update handling failed")

                asyncio.create_task(_handle())
        except Exception:
            log.exception("webhook decode failed")
        return JSONResponse({"ok": True})


async def main() -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    await _seed_settings()

    # Upload privacy PDF once on startup; ignore failures so the bot still starts.
    try:
        await policy_service.ensure_uploaded(bot)
    except Exception:
        log.exception("policy upload failed; will fall back to URL consent")

    # Recovery shouldn't block dispatcher startup — fire-and-forget.
    async def _recover():
        try:
            await recover_stuck_funnels(bot)
        except Exception:
            log.exception("recover_stuck_funnels failed")

    asyncio.create_task(_recover())

    # /healthz: always on. Webhook mode also serves it from FastAPI, but in
    # polling mode this is the only endpoint, so we can't skip it.
    health_runner = None
    if settings.bot_mode == "polling":
        health_runner = await health.start(
            host=settings.webhook_host, port=settings.webhook_port
        )
        asyncio.create_task(health.heartbeat_pulse())

    send_admin_text, send_admin_document = _build_admin_senders(bot)
    scheduler = cron_service.build_scheduler(send_admin_document, send_admin_text)
    scheduler.start()

    try:
        if settings.bot_mode == "webhook":
            log.info("Starting in webhook mode at %s", settings.webhook_url)
            await dp.init_serve(bot, log_level=settings.log_level.lower())
        else:
            log.info("Starting in long polling mode")
            await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        if health_runner is not None:
            await health_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
