from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from maxapi import Bot, Dispatcher
from maxapi.exceptions.max import InvalidToken

from aemr_bot import health
from aemr_bot.config import settings
from aemr_bot.db.session import session_scope
from aemr_bot.handlers import register_handlers
from aemr_bot.handlers.appeal import recover_stuck_funnels
from aemr_bot.services import broadcasts as broadcasts_service
from aemr_bot.services import cron as cron_service
from aemr_bot.services import operators as operators_service
from aemr_bot.services import policy as policy_service
from aemr_bot.services import settings_store

log = logging.getLogger("aemr_bot")

bot = Bot(settings.bot_token)
dp = Dispatcher()
register_handlers(dp)


def _install_polling_timeout(bot: Bot, timeout: int) -> None:
    """Зафиксировать таймаут long-poll, который использует Dispatcher.start_polling.

    maxapi вызывает bot.get_updates(marker=...) без таймаута и откатывается
    на серверный по умолчанию. Мы переопределяем метод на этом экземпляре,
    чтобы каждый запрос GetUpdates нёс наш таймаут. Он управляет тем, как
    долго MAX держит запрос при отсутствии событий. Настройка торгует
    частотой пустых обращений против запаса по rate-limit. См.
    settings.polling_timeout_seconds.
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


# Обработчик webhook'а регистрируется при загрузке модуля, чтобы dp.init_serve() его подхватил.
# По Макс.docx раздел 12 (Quick Start Python webhook):
#   from maxapi.methods.types.getted_updates import process_update_webhook
#   @dp.webhook_post('/...') → возвращает 2xx, затем dp.handle(event) обрабатывает событие.
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


async def _register_bot_commands(bot: Bot) -> None:
    """Прописать список команд в /-меню MAX, чтобы они видны при наборе слэша.

    MAX Bot API не поддерживает раздельные scopes (нет аналога
    `BotCommandScopeChat` в Telegram). Set_my_commands записывает один
    список для всех пользователей — и для жителя в личке, и для
    оператора в служебной группе. Поэтому здесь только команды жителя.
    Оператор и так пользуется кнопочной панелью /op_help; полный
    справочник его команд — в самой панели и в RUNBOOK. Вывод операторских
    команд здесь засорил бы подсказки у обычного жителя именами вроде
    /erase, /setting, /reply — это плохая UX и мини-намёк злоумышленнику
    о наличии админских команд.
    """
    from maxapi.types import BotCommand

    commands = [
        BotCommand(name="start", description="Открыть меню"),
        BotCommand(name="menu", description="Открыть меню"),
        BotCommand(name="help", description="Справка"),
        BotCommand(name="policy", description="Политика обработки данных"),
        BotCommand(name="subscribe", description="Подписаться на новости"),
        BotCommand(name="unsubscribe", description="Отписаться от новостей"),
        BotCommand(name="forget", description="Удалить мои данные"),
        BotCommand(name="whoami", description="Показать мой ID"),
    ]
    await bot.set_my_commands(*commands)
    log.info("set_my_commands: зарегистрировано %d команд жителя", len(commands))


async def _preflight_check_token(bot: Bot) -> None:
    """Один лёгкий запрос к MAX до политики и до dispatcher.

    Цель — получить понятную диагностику при битом токене.
    Без preflight первый сетевой вызов делал `policy_service.ensure_uploaded`,
    падал с InvalidToken; внутри maxapi aiohttp-сессия закрывалась, и
    дальнейший `dp.start_polling` уходил в `RuntimeError: Session is closed`.
    Контейнер уходил в restart-loop без внятной первопричины.

    Здесь мы ловим InvalidToken явно и выходим с осмысленным сообщением,
    чтобы оператор видел `❌ BOT_TOKEN неверный` вместо стектрейса
    aiohttp. Сетевые ошибки (MAX временно недоступен) не считаем
    смертельными — пишем warning и продолжаем, dispatcher переподключится.
    """
    try:
        info = await bot.get_me()
    except InvalidToken:
        log.error(
            "❌ BOT_TOKEN неверный или просрочен. "
            "Проверьте значение в infra/.env и токен бота в max.ru/business. "
            "Контейнер выйдет, чтобы избежать restart-loop."
        )
        sys.exit(1)
    except Exception:
        log.warning(
            "preflight: get_me() упал по сети. Продолжаем — dispatcher "
            "сам переподключится. Если бот не оживёт, проверьте сеть и токен.",
            exc_info=True,
        )
        return
    name = getattr(info, "first_name", None) or getattr(info, "name", "?")
    bot_id = getattr(info, "user_id", None) or getattr(info, "id", "?")
    log.info("preflight: токен валидный — бот %s (id=%s)", name, bot_id)


async def main() -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Сначала проверяем токен MAX, до любых других сетевых операций.
    # См. _preflight_check_token: без этого первый сбой (политика, рассылки)
    # ронял aiohttp-сессию и dispatcher падал на «Session is closed».
    await _preflight_check_token(bot)

    # Регистрируем команды в /-меню MAX. Без этого житель и оператор видят
    # пустую подсказку при наборе слэша. set_my_commands перезаписывает
    # список целиком — при удалении команды из словаря ниже она исчезнет
    # из подсказок MAX тоже. Список общий для жителя и оператора:
    # бот сам внутри команд проверяет, в каком чате он.
    try:
        await _register_bot_commands(bot)
    except Exception:
        log.exception("set_my_commands failed; продолжаем без подсказок в /-меню")

    await _seed_settings()

    # На холодном старте создаём первого ИТ-оператора из env, если ни одного ещё нет.
    if settings.bootstrap_it_max_user_id is not None:
        try:
            async with session_scope() as session:
                inserted = await operators_service.bootstrap_it_from_env(
                    session,
                    max_user_id=settings.bootstrap_it_max_user_id,
                    full_name=(
                        settings.bootstrap_it_full_name or "ИТ-специалист"
                    ),
                )
            if inserted:
                log.info(
                    "bootstrapped IT operator from env: max_user_id=%s",
                    settings.bootstrap_it_max_user_id,
                )
        except Exception:
            log.exception("bootstrap_it_from_env failed")

    # Один раз на старте загружаем PDF с политикой приватности; ошибки игнорируем, чтобы бот всё равно поднялся.
    try:
        await policy_service.ensure_uploaded(bot)
    except Exception:
        log.exception("policy upload failed; will fall back to URL consent")

    # Подбираем рассылки, которые предыдущий процесс оставил в SENDING.
    # Без этого они бы навсегда висели в SENDING. См. services/broadcasts.py.
    try:
        async with session_scope() as session:
            reaped = await broadcasts_service.reap_orphaned_sending(session)
        if reaped:
            log.warning(
                "marked %d orphaned broadcast(s) as failed (left in SENDING by previous process)",
                reaped,
            )
    except Exception:
        log.exception("reap_orphaned_sending failed")

    # Восстановление не должно блокировать старт диспетчера — запускаем и забываем.
    async def _recover():
        try:
            await recover_stuck_funnels(bot)
        except Exception:
            log.exception("recover_stuck_funnels failed")

    asyncio.create_task(_recover())

    # /healthz: всегда поднят. В режиме webhook его раздаёт FastAPI, но в
    # режиме polling это единственная точка входа, поэтому пропустить нельзя.
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
