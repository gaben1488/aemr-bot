"""Наблюдаемость aemr-bot: Sentry + PII-фильтр.

Лёгкая обвязка вокруг sentry-sdk. Цель — поймать тихие ошибки в
производстве (как было с `OP_HELP_FULL` overflow до жалобы владельца)
без тяжёлого observability-стека (Prometheus / Grafana / ELK / Loki —
out-of-scope MLP для гос-бота на одном VPS).

Включается через `SENTRY_DSN` env-переменную. Без неё `init_sentry()`
становится no-op; никакого вызова в Sentry нет.
"""

from aemr_bot.observability.sentry import init_sentry

__all__ = ["init_sentry"]
