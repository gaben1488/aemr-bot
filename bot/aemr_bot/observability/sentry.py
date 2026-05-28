"""Sentry-обвязка с PII-фильтром для гос-бота aemr-bot.

## Зачем

`OP_HELP_FULL` overflow в проде не был замечен мониторингом — только
после жалобы владельца. `docker logs` достаточно для разовой
диагностики, но плохо ловит редкие исключения и тихие fail'ы. Sentry
агрегирует похожие исключения, показывает частоту, traceback и
контекст. Это **наблюдаемость без тяжёлого стека** — ELK/Loki/Grafana
out-of-scope MLP для одного VPS на 5-7 операторов.

## 152-ФЗ соблюдение

Сообщения и stacktrace'ы могут случайно содержать ПДн — номер
телефона жителя в логах при ошибке handler'а, max_user_id в
exception'е. `before_send` хук маскирует:

- `+7XXXXXXXXXX` → `+7***NNNN`
- `max_user_id=NNN` → `max_user_id=***`
- `phone=+7NNN` → `phone=***`

Это второй слой защиты поверх `audit_log` retention 365 дней и
GPG-encrypted backup. До Sentry такие подстроки попадали в `docker
logs` без маскирования — теперь стираются перед отправкой.

## Активация

Env: `SENTRY_DSN=https://<key>@sentry.io/<project>` (или self-host
DSN). Без env — `init_sentry()` no-op, никаких сетевых вызовов.

## Self-host для гос-сервиса

Sentry Server можно поднять отдельным docker-compose на VPS — все
данные остаются в РФ. Альтернатива — sentry.io free tier (5K events
в месяц, достаточно для бота на 200-2000 событий в день).
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)


# Регулярные выражения для PII-фильтра. Применяются по приоритету:
# specific patterns с context (phone=, max_user_id=) ловятся раньше,
# чтобы при последующей подстановке `+7\d{10}` не попало в уже
# замаскированный контекст.
_PII_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # `phone=+7XXX` / `phone: +7XXX` — наиболее частый pattern в exc
    # messages из handler-кода («failed to deliver phone=+7...»).
    (
        re.compile(r"(?i)(phone\s*[=:]\s*)\+?\d{6,}"),
        r"\1+7***",
    ),
    # `max_user_id=NNN` — pattern из admin_card / admin_events
    # debug-сообщений.
    (
        re.compile(r"(?i)(max_user_id\s*[=:]\s*)\d{4,}"),
        r"\1***",
    ),
    # `user_id=NNN` (без max_ префикса).
    (
        re.compile(r"(?i)(user_id\s*[=:]\s*)\d{4,}"),
        r"\1***",
    ),
    # `appeal_id=NNN` — не строго PII, но дополнительный context который
    # Sentry-fingerprinting может использовать; маскируем для
    # consistency.
    (
        re.compile(r"(?i)(appeal_id\s*[=:]\s*)\d+"),
        r"\1***",
    ),
    # «голый» телефон в любом месте сообщения: `+7XXXXXXXXXX` или
    # `89XXXXXXXXX`. Ставим в конец, чтобы более специфичные
    # phone= / max_user_id= шаблоны сработали раньше.
    (
        re.compile(r"\+7\d{10}\b"),
        "+7***NNNN",
    ),
    (
        re.compile(r"\b8\d{10}\b"),
        "8***NNNN",
    ),
)


def _mask_pii(text: str) -> str:
    """Прогнать строку через все PII-паттерны.

    Идемпотентна: повторный вызов не меняет уже замаскированную строку
    (паттерны не совпадают с уже подставленными `***`).
    """
    if not text:
        return text
    result = text
    for pattern, replacement in _PII_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def _scrub_event(event: dict[str, Any]) -> dict[str, Any]:
    """Маскировать PII во всех текстовых полях Sentry event.

    Sentry event — вложенный dict с exception messages, breadcrumbs,
    request data, extra context. Проходим только текстовые поля
    верхнего уровня + exception messages + breadcrumb messages, чтобы
    не сломать структуру (numeric IDs Sentry'а, timestamps).
    """
    # Top-level message
    if isinstance(event.get("message"), str):
        event["message"] = _mask_pii(event["message"])

    # Exception chain (главный traceback)
    if isinstance(event.get("exception"), dict):
        values = event["exception"].get("values") or []
        for exc in values:
            if isinstance(exc.get("value"), str):
                exc["value"] = _mask_pii(exc["value"])

    # Breadcrumbs (история до exception'а)
    if isinstance(event.get("breadcrumbs"), dict):
        values = event["breadcrumbs"].get("values") or []
        for crumb in values:
            if isinstance(crumb.get("message"), str):
                crumb["message"] = _mask_pii(crumb["message"])
            # data dict в breadcrumb'е может содержать произвольные ключи
            data = crumb.get("data")
            if isinstance(data, dict):
                for k, v in list(data.items()):
                    if isinstance(v, str):
                        data[k] = _mask_pii(v)

    # Extra context (заданный в коде через sentry_sdk.set_context)
    if isinstance(event.get("extra"), dict):
        for k, v in list(event["extra"].items()):
            if isinstance(v, str):
                event["extra"][k] = _mask_pii(v)

    return event


def _before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Хук sentry_sdk, вызывается перед отправкой event'а на сервер.

    Маскирует PII во всех текстовых полях. Возврат `None` отменяет
    отправку (используется для дедупликации шумных категорий, если
    появится необходимость).
    """
    try:
        return _scrub_event(event)
    except Exception:
        # Любая ошибка в фильтре не должна ронять бот. Sentry получит
        # пустое уведомление о сбое — но лучше так, чем silent crash
        # в production.
        log.exception("sentry _before_send PII scrubber failed")
        return event


def init_sentry(dsn: str | None, environment: str = "production") -> bool:
    """Инициализировать Sentry если задан DSN.

    Возвращает True, если инициализация состоялась. False — если DSN
    пустой / отсутствует sentry-sdk, либо init упал. В любом случае
    основное приложение продолжает работать (Sentry — optional слой
    наблюдаемости, не критический путь).
    """
    if not dsn:
        log.info("sentry: SENTRY_DSN не задан, наблюдаемость отключена")
        return False

    try:
        import sentry_sdk
    except ImportError:
        log.warning(
            "sentry: SENTRY_DSN задан, но sentry-sdk не установлен — "
            "наблюдаемость отключена. `pip install sentry-sdk`."
        )
        return False

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            # send_default_pii=False (по умолчанию) — sentry-sdk сам
            # не отправляет IP/cookies/user.email. Наш PII-фильтр
            # дополняет: чистит ПДн из exception messages.
            send_default_pii=False,
            before_send=_before_send,
            # Trace-sampling: 0.0 чтобы не слать ни одной транзакции —
            # это performance monitoring, нам не нужно (overhead+quota).
            # Только exception'ы.
            traces_sample_rate=0.0,
            # release не задаём — Sentry автоматически берёт из git
            # commit SHA если CI / env GIT_COMMIT задан. Иначе
            # помечает релиз как "unknown".
        )
        log.info(
            "sentry: инициализирован для environment=%s, "
            "PII-фильтр активирован",
            environment,
        )
        return True
    except Exception:
        log.exception("sentry: init упал, наблюдаемость отключена")
        return False
