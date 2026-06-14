from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from maxapi import Bot, Dispatcher
from maxapi.client.default import DefaultConnectionProperties
from maxapi.exceptions.max import InvalidToken

from aemr_bot import health, network
from aemr_bot.config import settings
from aemr_bot.db.session import session_scope
from aemr_bot.handlers import register_handlers
from aemr_bot.handlers.appeal import recover_stuck_funnels
from aemr_bot.services import broadcasts as broadcasts_service
from aemr_bot.services import cron as cron_service
from aemr_bot.services import operators as operators_service
from aemr_bot.services import policy as policy_service
from aemr_bot.services import settings_store
# Ре-экспорт: spawn_background_task переехал в utils/background.py
# (батч 4), но исторические вызовы `from aemr_bot.main import
# spawn_background_task` должны продолжать работать — импортированное
# имя становится атрибутом модуля main.
from aemr_bot.utils.background import spawn_background_task

log = logging.getLogger("aemr_bot")

# Sacred event log hook: оборачивает bot.send_message декоратором,
# который синхронизирует menu_tracker[admin_group_id] после каждого
# успешного send в admin chat. Закрывает архитектурный gap «62
# прямых send'a в admin chat без tracker.sync» одной строкой —
# подробное обоснование в `services/admin_bus.install_outgoing_tracker_hook`.
from aemr_bot.services import admin_bus  # noqa: E402


def build_bot() -> Bot:
    """Собрать экземпляр Bot с нашими таймаутами и hook'ами.

    maxapi default = timeout 150s × max_retries 3 (до 10 мин на запрос).
    При sequential polling один тормозящий запрос блокирует обработку
    ВСЕХ следующих событий — видимое «тап → бот завис». Override через
    наш конфиг: timeout 30s + 1 retry → worst case ~60s, не 10 минут.

    Здесь же ставится outgoing-tracker hook (sacred event log) и, для
    polling-режима, фиксированный таймаут long-poll — чтобы поведение
    запуска не зависело от того, через фабрику или модуль создан bot.
    """
    bot = Bot(
        settings.bot_token,
        default_connection=DefaultConnectionProperties(
            timeout=settings.max_api_timeout_seconds,
            max_retries=settings.max_api_retries,
            # firewall mode: при включённом прокси-режиме trust_env=True уходит в
            # ClientSession (maxapi/bot.py ensure_session) → aiohttp читает
            # HTTP(S)_PROXY из окружения. Вне режима dict пустой, поведение прежнее.
            **network.session_kwargs(),
        ),
    )
    admin_bus.install_outgoing_tracker_hook(bot)
    if settings.bot_mode == "polling":
        _install_polling_timeout(bot, settings.polling_timeout_seconds)
        # Клиентский ClientTimeout.total долгого poll'а должен превышать
        # серверный hold (finding c) — иначе холостые циклы рвутся по
        # таймауту и бот переподключается без передышки.
        _apply_polling_client_timeout(bot)
    return bot


def build_dispatcher() -> Dispatcher:
    """Собрать Dispatcher с зарегистрированными роутерами/хендлерами.

    use_create_task=True: handlers — отдельные asyncio.Task, polling loop
    не блокируется одним долгим callback'ом. Per-user state защищён
    asyncio.Lock в appeal_runtime, concurrent dispatch безопасен.

    В polling-режиме оборачиваем `dp.handle` (P2-1): и `on_message`, и
    `on_callback` диспетчеризуются ровно через него (maxapi зовёт
    `dp.handle(event)` на каждый апдейт), поэтому это единственная точка,
    закрывающая оба пути сразу — без правок в каждом handler'е. Обёртка
    делает per-user токен-бакет (бёрст одного жителя гасится тихим ack'ом
    без ответа) и bounded-семафор (потолок одновременных handle()-тасков,
    симметрично webhook-семафору). В webhook-режиме поток уже ограничивает
    _WEBHOOK_SEMAPHORE вокруг dp.handle в _max_webhook, поэтому второй
    семафор там не вешаем (иначе двойное bounded-окно) — но токен-бакет
    одинаково полезен и там; оставляем обёртку и для webhook, без своего
    семафора.
    """
    dp = Dispatcher(use_create_task=True)
    register_handlers(dp)
    _install_dispatch_guards(dp)
    return dp


def _install_dispatch_guards(dp: Dispatcher) -> None:
    """Навесить per-user throttle и bounded-семафор на `dp.handle`.

    Переопределяем метод на ЭКЗЕМПЛЯРЕ (как `_install_polling_timeout`
    для `bot.get_updates`), не трогая maxapi. Семафор берём только в
    polling-режиме: webhook-путь уже обёрнут _WEBHOOK_SEMAPHORE, дублировать
    bound нельзя. Троттлинг применяем в обоих режимах — это анти-флуд на
    уровне жителя, ортогональный транспорту.
    """
    original_handle = dp.handle
    polling = settings.bot_mode == "polling"

    async def guarded_handle(event_object, *args, **kwargs):
        # 1) Per-user токен-бакет — до любой работы. Затроттленное событие
        #    тихо завершаем (callback — гасим спиннер ack'ом, текст роняем).
        if not _throttle_allows_event(event_object):
            await _ack_throttled_callback(event_object)
            return None
        # 2) Bounded-семафор только на polling-пути (webhook ограничен своим).
        if polling:
            async with _get_polling_dispatch_semaphore():
                return await original_handle(event_object, *args, **kwargs)
        return await original_handle(event_object, *args, **kwargs)

    dp.handle = guarded_handle  # type: ignore[method-assign]


def create_app() -> tuple[Bot, Dispatcher]:
    """Фабрика приложения: (Bot, Dispatcher), готовые к запуску.

    Объединяет build_bot + build_dispatcher в одну точку. Вызывается на
    уровне модуля (ниже — `bot, dp = create_app()`), чтобы сохранить
    исторические `from aemr_bot.main import bot/dp` и регистрацию
    webhook-декоратора, замыкающего эти module-level имена. Поведение
    запуска идентично прежней inline-инициализации.
    """
    return build_bot(), build_dispatcher()


# Semaphore-окно для входящих webhook'ов. Без ограничения каждый POST
# в /max/webhook порождает asyncio.create_task(...) — флуд (1000 RPS
# или ботнет) получает unbounded task spawn → OOM при mem_limit=512m
# в docker-compose. С 32 параллельными dispatchers очередь FastAPI
# держит остальные на 200ms+ — клиенты MAX перетягивают, но процесс
# не падает. 32 — компромисс между throughput и memory pressure;
# увеличивать только после реальных нагрузочных замеров.
_WEBHOOK_CONCURRENCY = 32
_WEBHOOK_SEMAPHORE: asyncio.Semaphore | None = None


def _get_webhook_semaphore() -> asyncio.Semaphore:
    """Lazy-init семафора. Создавать на module-level нельзя — нет
    активного event loop при импорте main.py."""
    global _WEBHOOK_SEMAPHORE
    if _WEBHOOK_SEMAPHORE is None:
        _WEBHOOK_SEMAPHORE = asyncio.Semaphore(_WEBHOOK_CONCURRENCY)
    return _WEBHOOK_SEMAPHORE


# Semaphore-окно для polling-dispatch (P2-1). maxapi с use_create_task=True
# спавнит `asyncio.create_task(dp.handle(event))` на КАЖДЫЙ апдейт без
# верхней границы (см. maxapi/dispatcher.py:_dispatch_fetched_events). На
# webhook-пути флуд ограничивает _WEBHOOK_SEMAPHORE, но polling-путь зиял:
# ботнет или один зацикленный клиент мог накопить тысячи одновременных
# handle()-тасков, каждый из которых берёт соединение из пула БД (15) →
# исчерпание пула, рост памяти при mem_limit=512m, деградация для всех.
# Симметрично webhook'у: bounded окно держит число параллельных dispatch'ей
# в узде. 32 — тот же компромис throughput/память, что и у webhook-семафора.
_POLLING_DISPATCH_CONCURRENCY = 32
_POLLING_DISPATCH_SEMAPHORE: asyncio.Semaphore | None = None


def _get_polling_dispatch_semaphore() -> asyncio.Semaphore:
    """Lazy-init polling-dispatch семафора (нет event loop при импорте)."""
    global _POLLING_DISPATCH_SEMAPHORE
    if _POLLING_DISPATCH_SEMAPHORE is None:
        _POLLING_DISPATCH_SEMAPHORE = asyncio.Semaphore(
            _POLLING_DISPATCH_CONCURRENCY
        )
    return _POLLING_DISPATCH_SEMAPHORE


# Per-user токен-бакет (P2-1). Polling-путь не имел per-user rate-limit:
# один max_user_id мог гнать апдейты на скорости сети, и каждый порождал
# отдельный handle()-таск (см. семафор выше) + запросы к БД и MAX API.
# Бизнес-лимиты (3 обращения/час, followup-флуд) бьют ПОЗЖЕ — после того
# как событие уже прошло dispatch, открыло сессию БД и, возможно, сходило
# в MAX. Токен-бакет режет burst на самом входе: легитимный житель тапает
# воронку (~10 нажатий) и НЕ упирается, а машинный флуд (десятки событий в
# секунду от одного user_id) гасится до dispatch'а.
#
# Параметры: capacity 20 (запас на самый длинный человеческий всплеск —
# пройти воронку + поправиться), refill 5 ток/сек (устойчивый человеческий
# темп с большим запасом). Бакет в памяти, monotonic-часы; lazy-GC по TTL
# не даёт словарю расти бесконечно. Админ-группа и события без user_id
# НЕ троттлятся — операторов ограничивать нельзя, безатрибутные lifecycle-
# события пропускаем (fail-open по доступности, бакет — анти-флуд жителя).
_THROTTLE_CAPACITY = 20.0
_THROTTLE_REFILL_PER_SEC = 5.0
# Idle-бакет старше этого срока выбрасываем при следующей чистке, чтобы
# словарь не рос по мере прохождения новых жителей через бота.
_THROTTLE_TTL_SEC = 300.0


class _UserThrottle:
    """In-memory токен-бакет на max_user_id. Один процесс, один event loop.

    `allow(user_id)` → True если есть токен (-1), False если бакет пуст.
    Не блокирует и не спит: при отказе вызывающий тихо роняет событие.
    Потокобезопасность не нужна — asyncio single-thread, между `await`
    нет точки переключения внутри allow().
    """

    __slots__ = ("_capacity", "_refill", "_buckets")

    def __init__(self, capacity: float, refill_per_sec: float) -> None:
        self._capacity = capacity
        self._refill = refill_per_sec
        # user_id -> (tokens, last_monotonic)
        self._buckets: dict[int, tuple[float, float]] = {}

    def allow(self, user_id: int, *, now: float | None = None) -> bool:
        ts = asyncio.get_running_loop().time() if now is None else now
        tokens, last = self._buckets.get(user_id, (self._capacity, ts))
        # Пополняем пропорционально прошедшему времени, но не выше потолка.
        tokens = min(self._capacity, tokens + (ts - last) * self._refill)
        if tokens < 1.0:
            self._buckets[user_id] = (tokens, ts)
            return False
        self._buckets[user_id] = (tokens - 1.0, ts)
        return True

    def gc(self, *, now: float | None = None, ttl: float = _THROTTLE_TTL_SEC) -> None:
        """Выбросить давно неактивные бакеты. O(n) по словарю, зовётся редко
        (раз в N принятых событий) — амортизированно дёшево."""
        ts = asyncio.get_running_loop().time() if now is None else now
        stale = [uid for uid, (_, last) in self._buckets.items() if ts - last > ttl]
        for uid in stale:
            self._buckets.pop(uid, None)


_user_throttle: _UserThrottle | None = None
# Счётчик принятых событий — чтобы запускать gc() не на каждом, а раз в N.
_throttle_events_since_gc = 0
_THROTTLE_GC_EVERY = 500


def _get_user_throttle() -> _UserThrottle:
    global _user_throttle
    if _user_throttle is None:
        _user_throttle = _UserThrottle(
            _THROTTLE_CAPACITY, _THROTTLE_REFILL_PER_SEC
        )
    return _user_throttle


def _throttle_allows_event(event: object) -> bool:
    """Решить, пропускать ли входящее событие per-user токен-бакетом.

    Возвращает True (обрабатывать) если: событие из админ-группы (операторов
    не троттлим), у события нет user_id (lifecycle/безатрибутное — fail-open),
    или в бакете жителя есть токен. False — только когда конкретный житель
    превысил бёрст-лимит; вызывающий обязан тихо завершить событие.
    """
    from aemr_bot.utils.event import get_chat_id, get_user_id

    # Операторов в служебной группе не ограничиваем — они легитимно
    # прокликивают много кнопок (карточки, статистика, настройки).
    if settings.admin_group_id is not None and get_chat_id(event) == settings.admin_group_id:
        return True

    user_id = get_user_id(event)
    if user_id is None:
        return True

    throttle = _get_user_throttle()
    allowed = throttle.allow(user_id)
    if allowed:
        global _throttle_events_since_gc
        _throttle_events_since_gc += 1
        if _throttle_events_since_gc >= _THROTTLE_GC_EVERY:
            _throttle_events_since_gc = 0
            throttle.gc()
    return allowed


async def _ack_throttled_callback(event: object) -> None:
    """Тихо погасить спиннер на кнопке у затроттленного callback'а.

    Для MessageCallback без ack кнопка крутится у жителя. Тихий ack без
    notification — событие проигнорировано, но UI не зависает. Для не-callback
    (обычный текст) делать нечего: сообщение просто не обрабатывается.
    """
    from aemr_bot.utils.event import ack_callback

    if getattr(event, "callback", None) is not None:
        try:
            await ack_callback(event)
        except Exception:
            log.debug("throttled callback ack failed", exc_info=True)




def _install_polling_timeout(bot: Bot, timeout: int) -> None:
    """Зафиксировать таймаут long-poll, который использует Dispatcher.start_polling.

    maxapi вызывает bot.get_updates(marker=...) без таймаута и откатывается
    на серверный по умолчанию. Мы переопределяем метод на этом экземпляре,
    чтобы каждый запрос GetUpdates нёс наш таймаут. Он управляет тем, как
    долго MAX держит запрос при отсутствии событий. Настройка торгует
    частотой пустых обращений против запаса по rate-limit. См.
    settings.polling_timeout_seconds.

    Здесь же — единственная точка, через которую проходит каждая итерация
    polling-цикла (start_polling зовёт get_updates ровно раз за оборот),
    поэтому отмечаем здесь health.poll_watch на КАЖДОМ успешном возврате.
    Это даёт /livez второй, независимый от heartbeat сигнал живости:
    зависший handler при use_create_task=True не гасит heartbeat-таск, но
    если встанет сам poll-цикл (мёртвая aiohttp-сессия, голодание
    event-loop), last_poll протухнет и /livez покраснеет (finding b).
    Отмечаем только при УСПЕХЕ: на исключении (timeout/сетевой сбой) метку
    не двигаем — иначе мёртвый цикл, безостановочно бросающий ошибки,
    выглядел бы живым.
    """
    original = bot.get_updates

    async def get_updates_with_timeout(*args, **kwargs):
        kwargs.setdefault("timeout", timeout)
        result = await original(*args, **kwargs)
        health.poll_watch.mark()
        return result

    bot.get_updates = get_updates_with_timeout  # type: ignore[method-assign]


def _apply_polling_client_timeout(bot: Bot) -> None:
    """Развести серверный long-poll hold и клиентский ClientTimeout.total.

    maxapi держит ОДНУ aiohttp-сессию с total = default_connection.timeout
    (= max_api_timeout_seconds) на все запросы, включая long-poll
    get_updates. Серверный hold long-poll (polling_timeout_seconds) и
    клиентский total не должны совпадать: когда оба ~30с, каждый холостой
    цикл клиент рвёт AsyncioTimeoutError ровно тогда, когда сервер
    собирался ответить пустым [] — переподключение 24/7, бесполезная
    нагрузка и шум в логах (finding c).

    Поднимаем потолок сессии до max(текущий, polling_timeout + буфер), не
    опуская его: send_message/edit по-прежнему получают как минимум свой
    max_api_timeout_seconds. При дефолтах (polling 20 + буфер 10 = 30 ==
    max_api 30) потолок не меняется — инвариант «клиент ждёт дольше
    сервера» уже держится за счёт меньшего polling_timeout. Если оператор
    поднимет polling_timeout выше, потолок подтянется автоматически.
    """
    from aiohttp import ClientTimeout

    conn = bot.default_connection
    needed = (
        settings.polling_timeout_seconds
        + settings.polling_client_timeout_buffer_seconds
    )
    current = conn.timeout
    current_total = getattr(current, "total", None)
    if current_total is not None and needed <= current_total:
        return
    # ClientTimeout — frozen attrs-класс; пересобираем явно с новым total,
    # сохраняя прочие поля сессии (sock_connect/connect/sock_read/
    # ceil_threshold), чтобы не потерять их при подмене.
    conn.timeout = ClientTimeout(
        total=needed,
        connect=getattr(current, "connect", None),
        sock_connect=getattr(current, "sock_connect", None),
        sock_read=getattr(current, "sock_read", None),
        ceil_threshold=getattr(current, "ceil_threshold", 5),
    )


# Module-level bot/dp через фабрику. Сохраняем исторические
# `from aemr_bot.main import bot/dp` и регистрацию webhook-декоратора
# ниже (он замыкает эти имена). build_bot уже ставит polling-таймаут и
# tracker-hook, build_dispatcher регистрирует роутеры — порядок и
# побочные эффекты идентичны прежней inline-инициализации.
bot, dp = create_app()


async def _seed_settings():
    async with session_scope() as session:
        await settings_store.seed_if_empty(session)


def _warm_geo_indexes_sync() -> None:
    """Синхронный прогрев geo-индексов (вызывать через asyncio.to_thread).

    geo.py читает ~2.6 МБ GeoJSON (localities/streets/buildings) лениво при
    первом обращении и строит поверх STRtree-индексы. Холодная загрузка
    O(сотни мс) блокировала бы event-loop, если бы случилась внутри
    handler'а первого жителя, нажавшего «Поделиться геолокацией» — а это
    в нашем одно-loop'овом процессе морозит ВСЕХ. Прогреваем заранее в
    отдельном потоке: дергаем три @lru_cache-загрузчика (наполняют кеш) и
    один реальный find_address по центру ЕМО (Елизово), чтобы прошёл весь
    путь building→street индексов. Дальше первый запрос жителя бьёт по
    тёплому кешу.

    Полностью best-effort: при любой ошибке (нет seed-файлов, битый JSON)
    просто логируем debug и выходим — geo.* сам деградирует в None-режим,
    бот остаётся доступен (fail-open).
    """
    from aemr_bot.services import geo

    geo._load_localities()
    geo._load_buildings_index()
    geo._load_streets_index()
    # Центр Елизово (Wikidata P625) — заведомо внутри ЕМО, прогоняет
    # полный каскад locality→building→street по тёплым индексам.
    geo.find_address(53.184, 158.385)


async def _warm_geo_indexes() -> None:
    """Фоновая обёртка: гоняет прогрев в потоке, глушит любые ошибки."""
    try:
        await asyncio.to_thread(_warm_geo_indexes_sync)
        log.info("geo indexes warmed up (localities/buildings/streets)")
    except Exception:
        log.debug(
            "geo warmup failed — индексы прогреются лениво при первом "
            "запросе жителя, безопасно",
            exc_info=False,
        )


def _build_admin_senders(bot: Bot):
    from aemr_bot.services import uploads

    async def send_admin_text(text: str, *, critical: bool = False):
        # Идёт через admin_bus, чтобы tracker сдвигался автоматически
        # после каждого pulse / cron-уведомления / алерта. Иначе
        # freshness-rule в admin_card.render / send_or_edit_screen
        # отставала бы от реального состояния чата.
        #
        # `critical=True` — обязательно для cron-алёртов (фейл бэкапа,
        # ошибки retention, stale-operators, funnel-watchdog). Это
        # пробивает quiet режим: ночные алёрты должны быть видны утром,
        # иначе 152-ФЗ retention или потеря бэкапа окажется
        # незамеченной до понедельника. Pulse-heartbeat'ы остаются
        # `critical=False` — они и должны затихать ночью.
        # См. SECURITY_REVIEW_2026-05-28 §A1.
        from aemr_bot.services import admin_bus

        if not settings.admin_group_id:
            return
        await admin_bus.send(bot, text=text, critical=critical)

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


# Обработчик webhook'а регистрируется при загрузке модуля. В polling-режиме
# (default по проекту) этот блок не активируется. webhook-режим оставлен
# как dead-but-not-removed для возможного будущего возврата к FastAPI-стеку.
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
            # Сравнение через hmac.compare_digest — защита от timing-oracle
            # на проверке секрета. И только заголовок X-Max-Secret: query-
            # параметр откладывается в логи nginx, в Referer и в браузерную
            # историю — это утечка секрета в эфемерные логи.
            import hmac

            got = request.headers.get("X-Max-Secret") or ""
            if not hmac.compare_digest(got, settings.webhook_secret):
                return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            event_json = await request.json()
            if process_update_webhook is not None:
                event_object = await process_update_webhook(event_json=event_json, bot=bot)

                async def _handle():
                    sem = _get_webhook_semaphore()
                    async with sem:  # bounded concurrency, защита от флуда
                        try:
                            await dp.handle(event_object)
                        except Exception:
                            log.exception("update handling failed")

                spawn_background_task(_handle(), name="webhook_dispatch")
        except Exception:
            log.exception("webhook decode failed")
        return JSONResponse({"ok": True})


async def _register_bot_commands(bot: Bot) -> None:
    """Опубликовать /-меню MAX: универсальные /start и /menu (PATCH /me).

    MAX Bot API не поддерживает per-scope команды (нет
    `BotCommandScopeChat` как в Telegram) — одно /-меню на ВСЕ чаты:
    и личку жителя, и служебную группу. Поэтому публикуем только
    универсальные команды, осмысленные в любом чате: /start и /menu
    (в личке открывают меню жителя, в админ-группе — памятку оператора).
    Специфичные жительские команды (/forget, /export, /policy, …) НЕ
    публикуем — в служебной группе они путали операторов («почему
    /forget виден в админ-чате?»); их обработчики остаются рабочими как
    запасной путь, но в подсказке /-меню их нет.

    Шлём `commands` прямым aiohttp-PATCH /me, а не через
    `bot.set_my_commands()`: у того в `ChangeInfo.fetch()` стоит
    `if self.commands:` (предохранитель против случайной очистки), и
    нам проще держать явный предсказуемый список здесь.
    """
    import aiohttp

    url = f"{bot.api_url}/me"
    # API MAX перешёл на Authorization-header; access_token в query
    # теперь возвращает 401. Префикс «Bearer» НЕ нужен: maxapi внутри
    # тоже передаёт токен напрямую (см. bot.py:153 — `self.headers =
    # {"Authorization": self.__token}`). Подкладываем то же самое.
    headers = {"Authorization": settings.bot_token}
    payload: dict[str, list] = {
        "commands": [
            {"name": "start", "description": "Запустить бота и открыть меню"},
            {"name": "menu", "description": "Открыть главное меню"},
        ]
    }
    try:
        async with aiohttp.ClientSession(**network.session_kwargs()) as session:
            async with session.patch(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    log.info("set_my_commands: /-меню опубликовано (/start, /menu) через PATCH /me")
                else:
                    body = await resp.text()
                    log.warning("set_my_commands PATCH вернул %s: %s", resp.status, body[:200])
    except Exception:
        log.exception("set_my_commands: PATCH /me failed (некритично, /-меню могут остаться у клиентов)")


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

    # Firewall mode ДО любых HTTP-клиентов: пробросить прокси в окружение и вшить
    # корпоративный CA (SSL_CERT_FILE), если заданы. Без настроек — тихий no-op.
    _firewall = network.apply_firewall_env()
    if _firewall:
        log.info("firewall mode: %s", ", ".join(_firewall))

    # Сначала проверяем токен MAX, до любых других сетевых операций.
    # См. _preflight_check_token: без этого первый сбой (политика, рассылки)
    # ронял aiohttp-сессию и dispatcher падал на «Session is closed».
    await _preflight_check_token(bot)

    # Публикуем /-меню MAX (/start, /menu) для всех чатов. У MAX нет
    # раздельного списка команд для лички и служебной группы, поэтому
    # держим только универсальные команды: в личке они открывают меню
    # жителя, в служебной группе — памятку оператора. Специфичные
    # жительские команды (/forget, /export…) в подсказку не выносим;
    # их обработчики работают как запасной путь.
    try:
        await _register_bot_commands(bot)
    except Exception:
        log.exception("set_my_commands failed; продолжаем без подсказок в /-меню")

    await _seed_settings()

    # Прогрев in-memory cache для quiet режима — до запуска polling и
    # cron'ов, чтобы первая же отправка через admin_bus.send уже видела
    # актуальный enabled/start/end из БД. Без этого первые ~5 секунд
    # (до первого pulse-cron'а) cache держит default disabled и
    # non-critical сообщения могут проскочить в quiet окне.
    # См. SECURITY_REVIEW_2026-05-28 §A2.
    try:
        from aemr_bot.services import quiet_hours

        async with session_scope() as session:
            await quiet_hours.refresh_cache_from_db(session)
    except Exception:
        log.debug(
            "quiet_hours.refresh_cache_from_db boot warmup failed — "
            "cache останется в default disabled, безопасно",
            exc_info=False,
        )

    # Прогрев geo-индексов (~2.6 МБ GeoJSON) — СТРОГО в фоне, не блокируя
    # старт polling. Без него первый житель, нажавший «Поделиться
    # геолокацией», оплатил бы холодную загрузку прямо в handler'е, что в
    # одно-loop'овом процессе подморозило бы всех. spawn_background_task с
    # asyncio.to_thread держит чтение/парсинг вне event-loop. См.
    # _warm_geo_indexes (fail-open: ошибка прогрева не валит старт).
    spawn_background_task(_warm_geo_indexes(), name="warm_geo_indexes")

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

    # Hydrate wizard state из БД (миграция 0011) — закрывает «оператор
    # потерял регистрацию сотрудника при docker compose up --build».
    # GC просроченных записей делает hydrate сам. На chicken-and-egg
    # проблем нет: in-memory dict'ы уже инициализированы пустыми;
    # hydrate просто наполняет.
    try:
        from aemr_bot.services import wizard_persist
        async with session_scope() as session:
            op_n, _ = await wizard_persist.hydrate_into_registry(session)
        # op-wizards в admin_operators._op_wizards — отдельный dict от
        # wizard_registry. Копируем туда же, чтобы handlers (которые
        # читают свой собственный dict) увидели восстановленное.
        if op_n:
            from aemr_bot.handlers import admin_operators
            from aemr_bot.services import wizard_registry as _wr
            for op_id, state in _wr._op_wizards.items():  # noqa: SLF001
                # Восстанавливаем expires_at в monotonic-форму:
                # реальный TTL уже отсчитан в БД, оставшийся остаток
                # неизвестен — даём свежий полный TTL. Хуже не будет:
                # оператор увидит свой шаг и продолжит.
                local = dict(state)
                local["expires_at"] = (
                    admin_operators._time_op.monotonic()
                    + admin_operators._OP_WIZARD_TTL_SEC
                )
                admin_operators._op_wizards[op_id] = local  # noqa: SLF001
    except Exception:
        log.exception("wizard hydrate failed; работаем без восстановленных wizards")

    # Восстановление не должно блокировать старт диспетчера — запускаем и забываем.
    async def _recover():
        try:
            await recover_stuck_funnels(bot)
        except Exception:
            log.exception("recover_stuck_funnels failed")

    spawn_background_task(_recover(), name="recover_stuck_funnels")

    # /healthz: всегда поднят. В режиме webhook его раздаёт FastAPI, но в
    # режиме polling это единственная точка входа, поэтому пропустить нельзя.
    health_runner = None
    if settings.bot_mode == "polling":
        health_runner = await health.start(
            host=settings.webhook_host, port=settings.webhook_port
        )
        spawn_background_task(health.heartbeat_pulse(), name="heartbeat_pulse")

    send_admin_text, send_admin_document = _build_admin_senders(bot)
    # bot отдаём в build_scheduler, чтобы сервисы не импортировали `main`
    # лазево (P0-2). Цикл services → main был хрупкий: любой рефакторинг
    # main.py мог сломать cron-job.
    scheduler = cron_service.build_scheduler(
        bot, send_admin_document, send_admin_text
    )
    scheduler.start()

    try:
        if settings.bot_mode == "webhook":
            log.info("Starting in webhook mode at %s", settings.webhook_url)
            # maxapi 1.1 пометил init_serve как deprecated и оставил
            # тонкую обёртку над handle_webhook (см. dispatcher.py:1476).
            # Зовём целевой метод напрямую — без DeprecationWarning в
            # логах и в готовности к будущему удалению init_serve.
            # `log_level` уходил в AppRunner kwargs и тихо игнорировался,
            # поэтому не пробрасываем — логгер настраивается через
            # logging.basicConfig выше.
            await dp.handle_webhook(
                bot,
                host=settings.webhook_host,
                port=settings.webhook_port,
                path="/max/webhook",
                secret=settings.webhook_secret or None,
            )
        else:
            log.info("Starting in long polling mode")
            await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        if health_runner is not None:
            await health_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
