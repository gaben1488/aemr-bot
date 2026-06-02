"""Единая шина для отправки сообщений в admin chat.

`admin_bus.send` оборачивает `bot.send_message` + `note_event` атомарно,
чтобы tracker не отставал от физического состояния чата. Шина — только
для новых сообщений; edit идёт через freshness-aware пути
(`admin_card.render`, `send_or_edit_screen`).

Также: `note_incoming_admin_message(mid)` двигает tracker на mid
входящего сообщения оператора (handler'ом, не самой шиной).

Полная мотивация и контракт «что шина делает и не делает»: см.
`docs/_meta/_archive/CODE_DECISIONS_LOG.md §2`.
"""
from __future__ import annotations

import asyncio
import logging
import time

from aemr_bot.config import settings as cfg
from aemr_bot.utils import menu_tracker
from aemr_bot.utils.event import extract_message_id

log = logging.getLogger(__name__)


# ============================================================================
# Глобальный лимитер исходящих (perf-кластер «2 RPS»)
# ============================================================================
# MAX держит исходящий лимит ~2 RPS на токен бота. Единственным троттлом
# раньше был локальный `asyncio.sleep(rate_delay)` внутри ОДНОЙ рассылки
# (handlers/broadcast). Проблема: рассылка (1 RPS) + ответы операторов +
# новые карточки обращений + cron-уведомления (pulse, funnel-watchdog,
# retention-алёрты) шлются НЕЗАВИСИМО и СКЛАДЫВАЮТСЯ. Их сумма пробивает
# 2 RPS → MAX отвечает 429 → часть рассылки (включая оповещение о ЧС)
# теряется, потому что `_send_one` сдаётся после 3 ретраев.
#
# Фикс: один процесс-глобальный token-bucket, через который проходят ВСЕ
# `bot.send_message` / `bot.edit_message` (навешивается в
# `install_outgoing_tracker_hook`, который УЖЕ оборачивает send_message —
# см. ниже). Рассылка и интерактив делят ОБЩИЙ бюджет, а не суммируются.
# funnel_watchdog и recover-рассылки идут через тот же хук автоматически
# (они зовут `bot.send_message`).
#
# Свойства (это ГОРЯЧИЙ путь — через него идёт каждый исходящий):
# - single-loop: один event loop, один процесс. Между refill и списанием
#   токена внутри `_reserve` НЕТ `await` → нет точки переключения,
#   гонок нет, блокировка не нужна.
# - monotonic-часы: невосприимчив к скачкам системного времени.
# - при низкой активности задержка ~0: токены накоплены до capacity,
#   `acquire()` списывает токен и возвращается без сна.
# - НЕ дедлочит: ждём ровно `время до следующего токена`, потом списываем.
# - FAIL-OPEN: если внутри лимитера случится исключение — `acquire()`
#   проглатывает его и возвращается немедленно. Доступность важнее
#   идеального темпа: лучше на мгновение превысить RPS, чем заморозить
#   отправку (например, не отправить оповещение о ЧС).
#
# Темп 1.5 msg/s (запас под 2 RPS): не упираемся в потолок при джиттере
# round-trip, оставляем место под edit_message и ack'и. Burst до capacity
# (3) сглаживает короткие всплески (пара карточек подряд) без задержки.
_OUTGOING_RATE_PER_SEC = 1.5
_OUTGOING_BURST = 3.0


class _GlobalOutgoingLimiter:
    """Процесс-глобальный async token-bucket на monotonic-времени.

    Один на процесс/loop. `acquire()` пропускает burst до `capacity`
    мгновенно, затем троттлит так, чтобы устойчивый темп не превышал
    `rate_per_sec`. Fail-open: любое внутреннее исключение → немедленный
    проход (доступность приоритетнее точности темпа).
    """

    __slots__ = ("_capacity", "_rate", "_tokens", "_last")

    def __init__(self, rate_per_sec: float, capacity: float) -> None:
        self._capacity = capacity
        self._rate = rate_per_sec
        # Стартуем с полным бакетом: первые `capacity` сообщений после
        # старта процесса уходят без задержки (типичный кейс — пара
        # стартовых pulse/карточек), дальше темп выравнивается.
        self._tokens = capacity
        self._last = time.monotonic()

    def _reserve(self) -> float:
        """Зарезервировать один слот отправки и вернуть, сколько ждать (сек).

        Пополняет бакет по прошедшему времени и СПИСЫВАЕТ токен синхронно,
        позволяя балансу уйти в МИНУС: отрицательный остаток — позиция в
        очереди. Резерв атомарен (между пополнением и списанием НЕТ `await`),
        поэтому конкурентные `acquire()` (use_create_task → параллельные
        Task'и, до 32 через dispatch-семафор) получают РАЗНЫЕ значения
        ожидания (1/rate, 2/rate, …) и разносятся во времени, а не
        просыпаются стадом и не пробивают темп. Возвращает 0.0, если токен
        был в наличии (burst до capacity уходит без задержки), иначе секунды
        до своего слота. Синхронный, без `await` — гонок в single-loop нет.
        """
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        # Резерв слота: уводим баланс в минус (очередь). Пополнение выше
        # capped на capacity, поэтому глубина минуса самоограничена реальной
        # нагрузкой и со временем выбирается по rate.
        self._tokens -= 1.0
        if self._tokens >= 0.0:
            return 0.0
        return (-self._tokens) / self._rate if self._rate > 0 else 0.0

    async def acquire(self) -> None:
        """Дождаться разрешения на одну исходящую отправку.

        Fail-open: при любой ошибке внутри — молча возвращаемся, отправка
        всё равно пойдёт. При наличии токена не спим (latency ~0). Слот
        резервируется ДО сна (синхронно), поэтому темп держится и под
        конкурентностью — повторно списывать после сна НЕ нужно.
        """
        try:
            wait = self._reserve()
            if wait > 0:
                await asyncio.sleep(wait)
        except Exception:
            # Никогда не ломаем отправку из-за лимитера.
            log.debug("outgoing-limiter: acquire failed, fail-open", exc_info=False)


_outgoing_limiter: _GlobalOutgoingLimiter | None = None


def _get_outgoing_limiter() -> _GlobalOutgoingLimiter:
    """Lazy-singleton глобального лимитера исходящих.

    Создаём лениво (не на import-time): значения rate/burst фиксированы,
    но единый инстанс на процесс гарантируем здесь, а не модульной
    переменной с side-effect при импорте.
    """
    global _outgoing_limiter
    if _outgoing_limiter is None:
        _outgoing_limiter = _GlobalOutgoingLimiter(
            rate_per_sec=_OUTGOING_RATE_PER_SEC, capacity=_OUTGOING_BURST
        )
    return _outgoing_limiter


async def _acquire_outgoing_slot() -> None:
    """Пройти глобальный лимитер перед фактической отправкой в MAX.

    Тонкая обёртка, чтобы хук и тесты дёргали единую точку. Fail-open
    обеспечивается самим `acquire()`.
    """
    await _get_outgoing_limiter().acquire()


async def send(
    bot,
    *,
    text: str,
    attachments: list | None = None,
    link=None,
    critical: bool = False,
) -> str | None:
    """Отправить сообщение в admin chat + сдвинуть tracker.

    Возвращает mid отправленного сообщения, либо None если ADMIN_GROUP_ID
    не настроен / send упал / тихий режим подавил отправку.

    Args:
        bot: maxapi Bot.
        text: текст сообщения.
        attachments: опциональный список вложений (клавиатуры, image, etc).
        link: опциональный NewMessageLink (для reply-цитирования).
        critical: если True — игнорировать quiet hours и отправлять всегда.
            Используется для алёртов о реальных инцидентах: фейл бэкапа,
            сбой ретеншена, ответы оператору в реальном времени. Default
            False — рутинные уведомления подавляются ночью если включён
            тихий режим (см. `services/quiet_hours.py`).
    """
    if not cfg.admin_group_id:
        log.warning("admin_bus.send: ADMIN_GROUP_ID не задан, пропускаем")
        return None
    if not critical:
        # quiet hours: sync-проверка in-memory cache, без открытия
        # новой DB-сессии (cache обновляется cron'ом и при set_value
        # в settings_store, см. `services/quiet_hours`).
        from aemr_bot.services.quiet_hours import is_quiet_hours_now

        if is_quiet_hours_now():
            log.info(
                "admin_bus.send: quiet hours active, suppressed "
                "(text prefix=%r)",
                text[:40],
            )
            return None
    kwargs: dict = {"chat_id": cfg.admin_group_id, "text": text}
    if attachments is not None:
        kwargs["attachments"] = attachments
    if link is not None:
        kwargs["link"] = link
    try:
        sent = await bot.send_message(**kwargs)
    except Exception:
        log.exception(
            "admin_bus.send: send_message failed для admin_group_id=%s",
            cfg.admin_group_id,
        )
        return None
    mid = extract_message_id(sent)
    if mid:
        # 2026-05-27 dual-tracker: admin_bus.send используется для
        # **historic events** (pulse, audit-уведомления, retention,
        # operator confirmations, audit-notice). Это НЕ редактируемые
        # меню — клик кнопки на них не должен превращать их в меню.
        # Поэтому двигаем только `physical_mid` через `note_event`,
        # `editable_mid` остаётся на mid предыдущего меню (если было).
        # Следующий тап оператора по кнопке на этом events callback_mid
        # != editable_mid → can_edit=False → send_new. Event остаётся
        # в чате как историчная запись.
        menu_tracker.note_event(cfg.admin_group_id, mid)
    return mid


def note_incoming_admin_message(mid: str | None) -> None:
    """Зарегистрировать факт входящего сообщения в admin chat.

    Вызывается из dispatch hook на каждый MessageCreated в admin chat
    (operator-text, sticker, voice, поговорил в чате, ответ свайпом).
    После этого callback на карточки ВЫШЕ этого сообщения будут идти
    в send_new (freshness увидит mismatch).

    Если mid не извлёкся (None) — no-op, не падаем. Худшее, что может
    случиться при пропуске одного incoming-сообщения — следующий
    operator-callback edit'нет одну карточку на месте, что
    самокорректируется на следующем outgoing-сообщении бота.
    """
    if not cfg.admin_group_id or not mid:
        return
    # 2026-05-27 dual-tracker: incoming op-message — двигает только
    # physical_mid. Editable_mid остаётся на mid предыдущего меню;
    # клик оператора по нему всё ещё может редактировать его, но
    # callback_mid != physical_mid (потому что op написал текст ниже)
    # → freshness откажет → send_new.
    menu_tracker.note_incoming(cfg.admin_group_id, mid)


# Маркер: к одному и тому же bot.send_message hook ставим только один
# раз. Без этого повторный install (например, в тестах) обернул бы send
# рекурсивно — каждое сообщение прошло бы tracker.set N раз. Маркер
# хранится на самом bot-объекте как атрибут.
_HOOK_INSTALLED_ATTR = "_aemr_admin_outgoing_tracker_installed"


def install_outgoing_tracker_hook(bot) -> None:
    """Monkey-patch `bot.send_message` / `bot.edit_message`: глобальный
    лимитер исходящих ПЕРЕД отправкой + после успешного send в admin chat
    двигает `menu_tracker.note_event(admin_group_id, mid)`.

    Закрывает 62 прямых `bot.send_message(...)` сайта одним hook'ом
    вместо миграции всех через `admin_bus.send` (большая правка, риск
    регрессий). Идемпотентно — повторный install на тот же bot no-op
    (маркер `_aemr_admin_outgoing_tracker_installed`).

    Perf-кластер «2 RPS»: тот же hook — единственная точка, через которую
    физически проходит КАЖДЫЙ исходящий вызов к MAX (`send_message` ad-hoc
    из 62 сайтов, рассылка `_send_one`, cron `funnel_watchdog`/pulse,
    `edit_message` прогресс-карточек и меню). Поэтому здесь же навешен
    `await _acquire_outgoing_slot()` — рассылка и интерактив делят ОДИН
    бюджет ~1.5 msg/s вместо того чтобы суммироваться и пробивать 429.
    Лимитер fail-open: если он сам бросит — отправка всё равно идёт.

    Полная мотивация (жалоба владельца, рассуждения «hook vs миграция»),
    идемпотентность guard, отношения с `admin_card.render` и
    `admin_bus.send`: см. `docs/_meta/_archive/CODE_DECISIONS_LOG.md §3`.

    Args:
        bot: maxapi Bot, на котором будет установлен hook.
    """
    if not cfg.admin_group_id:
        log.info(
            "install_outgoing_tracker_hook: ADMIN_GROUP_ID не задан, "
            "hook не устанавливаем (для citizen-only-окружения OK)."
        )
        return
    if getattr(bot, _HOOK_INSTALLED_ATTR, False):
        log.debug(
            "install_outgoing_tracker_hook: hook уже установлен на этом "
            "bot, повторный вызов проигнорирован."
        )
        return

    original_send = bot.send_message

    async def _wrapped_send(*args, **kwargs):
        # Hook должен быть совместим с любой формой вызова — позиционной,
        # keyword, mixed. Реальные использования в коде — keyword only
        # (chat_id=..., text=..., attachments=...), но защита от
        # внезапных позиционных аргументов нужна, чтобы hook не отвалился
        # на нестандартном usage и не сломал send.
        #
        # Perf «2 RPS»: глобальный лимитер ПЕРЕД фактической отправкой.
        # Fail-open (acquire сам глотает ошибки), поэтому исключение
        # самой отправки по-прежнему всплывает наружу как раньше —
        # tracker-sync ниже не выполнится (result не получен).
        await _acquire_outgoing_slot()
        result = await original_send(*args, **kwargs)
        try:
            target_chat_id = kwargs.get("chat_id")
            if (
                target_chat_id == cfg.admin_group_id
                and target_chat_id is not None
            ):
                mid = extract_message_id(result)
                if mid:
                    # 2026-05-27 dual-tracker: hook ловит ВСЕ исходящие
                    # в admin chat. Большинство — historic events (pulse,
                    # audit, broadcast progress, прямые sends в коде).
                    # Двигаем только physical_mid. Editable_mid должен
                    # обновляться явно через send_or_edit_screen или
                    # _send_or_edit_menu (которые сами знают, что они
                    # шлют редактируемый экран).
                    menu_tracker.note_event(cfg.admin_group_id, mid)
                    log.debug(
                        "outgoing-tracker-hook: admin chat send → "
                        "physical_mid = %s", mid,
                    )
        except Exception:
            # tracker-sync best-effort, никогда не ломает caller.
            log.debug(
                "install_outgoing_tracker_hook: post-send sync failed",
                exc_info=False,
            )
        return result

    bot.send_message = _wrapped_send  # type: ignore[assignment]

    # Perf «2 RPS»: edit_message тоже считается MAX'ом в общий лимит
    # исходящих (прогресс-карточки рассылки, edit меню/экранов). Прогоняем
    # его через ТОТ ЖЕ лимитер, чтобы общий темп держался. Tracker здесь
    # НЕ двигаем — edit не создаёт новое событие, это правка существующего
    # mid. Оборачиваем только если у bot есть edit_message (тестовые
    # заглушки бывают без него) — отсутствие не должно ломать install.
    original_edit = getattr(bot, "edit_message", None)
    if original_edit is not None and callable(original_edit):

        async def _wrapped_edit(*args, **kwargs):
            # Лимитер fail-open, исключение самой правки всплывает как раньше.
            await _acquire_outgoing_slot()
            return await original_edit(*args, **kwargs)

        bot.edit_message = _wrapped_edit  # type: ignore[assignment]

    setattr(bot, _HOOK_INSTALLED_ATTR, True)
    log.info(
        "install_outgoing_tracker_hook: установлен hook на bot.send_message "
        "(+edit_message) для admin_group_id=%s — каждый исходящий в admin "
        "chat двигает menu_tracker, и КАЖДЫЙ исходящий вызов к MAX проходит "
        "глобальный лимитер ~%.1f msg/s.",
        cfg.admin_group_id, _OUTGOING_RATE_PER_SEC,
    )
