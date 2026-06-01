"""URL threat-intelligence для входящих сообщений жителей.

См. `docs/_meta/URL_THREAT_INTEL_2026-05-26.md` для полной архитектуры.

Цель: оператор в admin-карточке видит предупреждение «⛔ Подозрительные
ссылки», если житель прислал в обращении URL из известных feed'ов
malware/phishing. Не блокирует сообщение жителя — у него может быть
легитимный кейс «мне это прислали мошенники, помогите» — но даёт
оператору сигнал к осторожности.

**Поведение**:
- В памяти держим `set[str]` нормализованных host'ов (lowercase, без
  www, без trailing slash). Lookup O(1).
- Обновление раз в час из трёх free feed'ов: URLhaus, ThreatFox,
  опционально PhishTank (если задан PHISHTANK_APP_KEY).
- Staleness budget 6 часов: если за 6 часов ни одно обновление не
  прошло — alert в admin chat, но бот продолжает работать со стейл-set'ом.
- Fail-open: любая ошибка fetch (network, parse, format-change) → log
  + использовать предыдущий set, не падать.

**Что НЕ делает**:
- Не выполняет live-lookup на API на hot-path (точка отказа).
- Не блокирует сообщение жителя — только warning оператору.
- Не персистит set на диск — restart бота = новая загрузка через cron.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import aiohttp

log = logging.getLogger(__name__)

# Источники feed'ов. URL'ы фиксированы в коде — это не пользовательский
# input, не нужно валидации; если abuse.ch поменяет endpoint, переписываем.
_URLHAUS_URL = "https://urlhaus.abuse.ch/downloads/csv_online/"
_THREATFOX_URL = "https://threatfox.abuse.ch/downloads/hostfile/"
_PHISHTANK_URL_TEMPLATE = (
    "https://data.phishtank.com/data/{app_key}/online-valid.json"
)

# Timeout запроса. Не критично долгий — cron job в фоне, не блокирует
# пользователя, но 60 сек достаточно для медленного CDN.
_FETCH_TIMEOUT_SEC = 60
# Staleness budget: после стольких секунд без успешного refresh — alert.
_STALENESS_BUDGET_SEC = 6 * 3600  # 6 часов


@dataclass
class ThreatIntelStore:
    """In-memory хранилище host'ов из threat-intel feed'ов.

    Singleton-инстанс получается через `get_store()` (lazy).
    """

    hosts: set[str] = field(default_factory=set)
    """Множество lowercase-host'ов известных malware/phishing-сайтов."""

    last_refresh_at: float | None = None
    """Monotonic-timestamp последнего успешного refresh'а (любого feed'а)."""

    sources: dict[str, int] = field(default_factory=dict)
    """Сколько host'ов вкладывает каждый feed (для observability)."""

    def is_malicious(self, url: str) -> tuple[bool, str | None]:
        """Проверить URL по локальному set'у.

        Возвращает (True, источник) если host URL в feed'е, иначе
        (False, None). Источник — для отчёта оператору в admin card.

        Sanity: пустой set (бот только что стартовал, cron ещё не
        прошёл) → всегда False, не fail-positive.
        """
        if not self.hosts or not url:
            return False, None
        host = _normalize_host(url)
        if not host:
            return False, None
        if host in self.hosts:
            # Мы знаем что host в set'е, но не знаем точно из какого
            # feed'а — set объединённый. Возвращаем generic-метку.
            return True, "threat-intel"
        return False, None

    def staleness_age_seconds(self) -> float | None:
        """Сколько секунд прошло с последнего успешного refresh'а.

        None — если ни разу не обновлялись (бот только что стартовал
        и cron ещё не прошёл).
        """
        if self.last_refresh_at is None:
            return None
        return time.monotonic() - self.last_refresh_at

    def is_stale(self) -> bool:
        """True если данные старше staleness budget'а."""
        age = self.staleness_age_seconds()
        return age is not None and age > _STALENESS_BUDGET_SEC


# Singleton — простой module-level dict. Не Class-method, чтобы было
# легко тестировать через monkeypatch.
_STORE: ThreatIntelStore | None = None


def get_store() -> ThreatIntelStore:
    """Lazy-singleton доступ к глобальному store."""
    global _STORE
    if _STORE is None:
        _STORE = ThreatIntelStore()
    return _STORE


def _normalize_host(url_or_host: str) -> str:
    """Извлечь и нормализовать hostname из URL или host'а напрямую.

    `https://www.Attacker.com/path` → `attacker.com`.
    `evil.example` → `evil.example`.
    `https://[fe80::1]/x` → `fe80::1` (IPv6 без скобок).

    Безопасно к парсе-ошибкам: всё, что не парсится — пустая строка.
    """
    try:
        # Если уже host без схемы — urlparse не вернёт hostname,
        # дописываем схему искусственно.
        if "://" not in url_or_host:
            parsed = urlparse(f"http://{url_or_host}")
        else:
            parsed = urlparse(url_or_host)
    except (ValueError, AttributeError):
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


async def _fetch_text(
    session: aiohttp.ClientSession, url: str
) -> str | None:
    """Скачать URL как text. None при любой ошибке (log на WARNING)."""
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_SEC)
        ) as resp:
            if resp.status != 200:
                log.warning(
                    "threat_intel: %s returned HTTP %d", url, resp.status
                )
                return None
            return await resp.text()
    except Exception as exc:
        log.warning("threat_intel: fetch %s failed: %s", url, exc)
        return None


def _parse_urlhaus_csv(body: str) -> set[str]:
    """CSV URLhaus: первые 7 строк — комментарий, потом
    `id,dateadded,url,...`. Берём `url` (поле 3, 0-indexed 2)."""
    hosts: set[str] = set()
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        # Простой parse — split по `,` с учётом возможных кавычек.
        # Полный CSV-parser избыточен для нашего use case (мы берём
        # один столбец, без многострочных значений).
        parts = line.split(",")
        if len(parts) < 3:
            continue
        url = parts[2].strip().strip('"')
        host = _normalize_host(url)
        if host:
            hosts.add(host)
    return hosts


def _parse_threatfox_hostfile(body: str) -> set[str]:
    """ThreatFox host-file: формат `0.0.0.0 evil.example` per line."""
    hosts: set[str] = set()
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        host = _normalize_host(parts[1])
        if host:
            hosts.add(host)
    return hosts


def _parse_phishtank_json(body: str) -> set[str]:
    """PhishTank online-valid.json: список объектов с полем `url`."""
    try:
        items = json.loads(body)
    except json.JSONDecodeError:
        return set()
    if not isinstance(items, list):
        return set()
    hosts: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        url = it.get("url", "")
        host = _normalize_host(url) if isinstance(url, str) else ""
        if host:
            hosts.add(host)
    return hosts


async def refresh_all() -> dict[str, int]:
    """Перетянуть все feed'ы и обновить singleton store.

    Возвращает dict `{feed_name: count}` для логирования. Если ни один
    feed не отдался — store не обновляется (сохраняется предыдущий
    set), `last_refresh_at` тоже не двигается → staleness растёт.

    Вызывается из cron `threat-intel-refresh` (раз в час).
    """
    store = get_store()
    counts: dict[str, int] = {}
    new_hosts: set[str] = set()
    async with aiohttp.ClientSession() as session:
        # Все три feed'а тянем параллельно — это IO-bound.
        urlhaus_task = _fetch_text(session, _URLHAUS_URL)
        threatfox_task = _fetch_text(session, _THREATFOX_URL)

        phishtank_key = os.environ.get("PHISHTANK_APP_KEY", "").strip()
        phishtank_task: asyncio.Task | None = None
        if phishtank_key:
            phishtank_task = asyncio.create_task(
                _fetch_text(
                    session,
                    _PHISHTANK_URL_TEMPLATE.format(app_key=phishtank_key),
                )
            )

        urlhaus_body, threatfox_body = await asyncio.gather(
            urlhaus_task, threatfox_task
        )
        phishtank_body = await phishtank_task if phishtank_task else None

    if urlhaus_body:
        hosts = _parse_urlhaus_csv(urlhaus_body)
        new_hosts.update(hosts)
        counts["urlhaus"] = len(hosts)
    if threatfox_body:
        hosts = _parse_threatfox_hostfile(threatfox_body)
        new_hosts.update(hosts)
        counts["threatfox"] = len(hosts)
    if phishtank_body:
        hosts = _parse_phishtank_json(phishtank_body)
        new_hosts.update(hosts)
        counts["phishtank"] = len(hosts)

    if not counts:
        log.warning(
            "threat_intel: ни один feed не отдался — set не обновлён, "
            "продолжаем со старым (age=%.0f сек)",
            store.staleness_age_seconds() or 0,
        )
        return {}

    store.hosts = new_hosts
    store.sources = counts
    store.last_refresh_at = time.monotonic()
    log.info(
        "threat_intel: refresh ok — %d host'ов из %d feed'ов: %s",
        len(new_hosts), len(counts), counts,
    )
    return counts
