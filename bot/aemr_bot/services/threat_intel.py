"""Local threat-feed reputation checks; never visit resident-supplied URLs.

Refresh at startup and hourly. Valid snapshots are retained per source and
persisted atomically. Failure never prevents receiving an appeal. A match is
host reputation, not a verdict about a particular page or all its subdomains.
"""
from __future__ import annotations

import asyncio
import csv
import io
import ipaddress
import json
import logging
import os
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlparse

import aiohttp

from aemr_bot.network import session_kwargs

log = logging.getLogger(__name__)
_URLHAUS_URL = "https://urlhaus-api.abuse.ch/v2/files/exports/{app_key}/recent.csv"
_THREATFOX_URL = "https://threatfox-api.abuse.ch/v2/files/exports/{app_key}/full.csv.zip"
_PHISHTANK_URL_TEMPLATE = "https://data.phishtank.com/data/{app_key}/online-valid.json"
_OPENPHISH_URL = "https://raw.githubusercontent.com/openphish/public_feed/refs/heads/main/feed.txt"
_FETCH_TIMEOUT_SEC = 60
_STALENESS_BUDGET_SEC = 6 * 3600
_MAX_FEED_BYTES = 32 * 1024 * 1024
_MAX_UNPACKED_BYTES = 64 * 1024 * 1024


def _enabled_sources() -> set[str]:
    # Without an abuse.ch key use the limited public phishing feed.
    names = ({"urlhaus", "threatfox"} if os.environ.get("ABUSECH_AUTH_KEY", "").strip()
             else {"openphish"})
    if os.environ.get("PHISHTANK_APP_KEY", "").strip():
        names.add("phishtank")
    return names


@dataclass
class ThreatIntelStore:
    hosts: set[str] = field(default_factory=set)
    last_refresh_at: float | None = None
    sources: dict[str, int] = field(default_factory=dict)
    source_hosts: dict[str, set[str]] = field(default_factory=dict)
    source_updated_at: dict[str, float] = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)
    outage_notified: bool = False
    refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def is_malicious(self, url: str) -> tuple[bool, str | None]:
        host = _normalize_host(url)
        if not host or host not in self.hosts:
            return False, None
        sources = sorted(name for name, hosts in self.source_hosts.items() if host in hosts)
        return True, ", ".join(sources) if sources else "threat-intel"

    def staleness_age_seconds(self) -> float | None:
        if self.last_refresh_at is None:
            return None
        return max(0, time.monotonic() - self.last_refresh_at)

    def is_stale(self) -> bool:
        if self.source_hosts:
            return any(
                time.time() - self.source_updated_at.get(
                    name, time.time() - (time.monotonic() - self.started_at)
                ) > _STALENESS_BUDGET_SEC
                for name in _enabled_sources()
            )
        origin = self.last_refresh_at if self.last_refresh_at is not None else self.started_at
        return time.monotonic() - origin > _STALENESS_BUDGET_SEC

    def rebuild(self) -> None:
        self.hosts = set().union(*self.source_hosts.values())
        self.sources = {name: len(hosts) for name, hosts in self.source_hosts.items()}


_STORE: ThreatIntelStore | None = None


def _cache_path() -> Path | None:
    value = os.environ.get("THREAT_INTEL_CACHE_PATH", "/var/lib/aemr/threat-intel.json").strip()
    return Path(value) if value else None


def save_cache(store: ThreatIntelStore) -> None:
    """No keys, resident data or URLs in cache; atomic replace on a persistent volume."""
    path = _cache_path()
    if path is None:
        return
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            # Version 2 preserves www subdomains as distinct hosts. Version 1
            # collapsed them into the apex and cannot be safely reconstructed.
            "version": 2, "outage_notified": store.outage_notified,
            "started_at": time.time() - max(0, time.monotonic() - store.started_at),
            "sources": {name: {"updated_at": store.source_updated_at[name], "hosts": sorted(hosts)}
                        for name, hosts in store.source_hosts.items()},
        }
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
            tmp = f.name
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        # Do not log exception strings: paths might contain credentials supplied by an operator.
        log.warning("threat_intel: cache write failed; memory snapshot retained")
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def get_store() -> ThreatIntelStore:
    global _STORE
    if _STORE is None:
        store = ThreatIntelStore()
        path = _cache_path()
        if path and path.is_file():
            try:
                if path.stat().st_size > _MAX_UNPACKED_BYTES:
                    raise ValueError("cache too large")
                data = json.loads(path.read_text())
                if data["version"] != 2:
                    raise ValueError("cache version")
                for name, item in data["sources"].items():
                    if name not in _enabled_sources():
                        continue
                    hosts = item["hosts"]
                    stamp = float(item["updated_at"])
                    if not isinstance(hosts, list) or not all(
                        isinstance(h, str) and h and _normalize_host(h) == h for h in hosts
                    ) or not 0 < stamp <= time.time():
                        raise ValueError("invalid cache")
                    store.source_hosts[name] = set(hosts)
                    store.source_updated_at[name] = stamp
                store.rebuild()
                if store.source_updated_at:
                    age = time.time() - max(store.source_updated_at.values())
                    store.last_refresh_at = time.monotonic() - max(0, age)
                store.started_at -= max(0, time.time() - float(data.get("started_at", time.time())))
                store.outage_notified = data.get("outage_notified") is True
            except Exception:
                store = ThreatIntelStore()
                log.warning("threat_intel: invalid cache ignored")
        _STORE = store
    return _STORE


def _normalize_host(url_or_host: str) -> str:
    try:
        value = url_or_host.strip()
        # A bare IPv6 address from our own cache is not a URL authority.
        try:
            return ipaddress.ip_address(value).compressed
        except ValueError:
            pass
        parsed = urlparse(value if "://" in value else f"http://{value}")
        host = (parsed.hostname or "").lower().rstrip(".")
        # URL authorities may spell the same IPv6 address in expanded form;
        # use the same representation as bare indicators and cache validation.
        try:
            return ipaddress.ip_address(host).compressed
        except ValueError:
            pass
        return host.encode("idna").decode("ascii")
    except (ValueError, AttributeError, UnicodeError):
        return ""


async def _fetch_text(session: aiohttp.ClientSession, url: str) -> str | None:
    """Bound compressed AND uncompressed sizes; never log URLs or exception messages."""
    label = urlparse(url).hostname
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_SEC),
            allow_redirects=False,
        ) as resp:
            if resp.status != 200:
                log.warning("threat_intel: %s HTTP %d", label, resp.status)
                return None
            body = bytearray()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                body.extend(chunk)
                if len(body) > _MAX_FEED_BYTES:
                    raise ValueError("feed too large")
            if url.endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(body)) as archive:
                    files = [f for f in archive.infolist() if not f.is_dir()]
                    if len(files) != 1 or files[0].file_size > _MAX_UNPACKED_BYTES:
                        raise ValueError("unexpected archive")
                    # Read at most the cap even if ZIP metadata lies; never extract paths.
                    with archive.open(files[0]) as stream:
                        body = bytearray(stream.read(_MAX_UNPACKED_BYTES + 1))
                    if len(body) > _MAX_UNPACKED_BYTES:
                        raise ValueError("archive too large")
            return body.decode("utf-8-sig")
    except Exception as exc:
        log.warning("threat_intel: %s fetch failed (%s)", label, type(exc).__name__)
        return None


def _rows(body: str):
    return csv.reader(io.StringIO("\n".join(
        line for line in body.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )), skipinitialspace=True)


def _feed_host(value: str) -> str:
    host = _normalize_host(value)
    if not host or any(c.isspace() for c in host) or any(c in host for c in "<>@/\\"):
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        return host if "." in host and all(
            part and len(part) <= 63 and all(c.isalnum() or c == "-" for c in part)
            for part in host.split(".")
        ) else ""


def _parse_urlhaus_csv(body: str) -> set[str]:
    hosts = set()
    for row in _rows(body):
        if len(row) >= 3 and row[0].strip().isdigit() and row[2].startswith(("http://", "https://")):
            # recent.csv contains offline entries as well as online ones.
            if len(row) > 3 and row[3] == "offline":
                continue
            if host := _feed_host(row[2]):
                hosts.add(host)
    return hosts


def _parse_threatfox_hostfile(body: str) -> set[str]:
    """Legacy parser retained for older fixtures; live source uses CSV exports."""
    hosts = set()
    for line in body.splitlines():
        parts = line.split()
        # These are hosts-file data markers; this parser never binds a socket.
        if (len(parts) >= 2 and parts[0] in {"0.0.0.0", "127.0.0.1"}  # nosec B104
                and (host := _feed_host(parts[1]))):
            hosts.add(host)
    return hosts


def _parse_threatfox_csv(body: str) -> set[str]:
    # Official CSV schema: id,first_seen_utc,ioc_value,ioc_type,threat_type,...
    hosts = set()
    for row in _rows(body):
        if len(row) < 4 or not row[0].strip().isdigit():
            continue
        if row[3] not in {"domain", "url", "ip:port"}:
            continue  # hashes are not network indicators
        if host := _feed_host(row[2]):
            hosts.add(host)
    return hosts


def _parse_phishtank_json(body: str) -> set[str]:
    try:
        items = json.loads(body)
    except json.JSONDecodeError:
        return set()
    if not isinstance(items, list):
        return set()
    return {host for item in items if isinstance(item, dict)
            and isinstance(item.get("url"), str)
            and item.get("online", "yes") == "yes" and item.get("verified", "yes") == "yes"
            if (host := _feed_host(item["url"]))}


def _parse_url_list(body: str) -> set[str]:
    return {host for line in body.splitlines()
            if line.startswith(("https://", "http://")) and (host := _feed_host(line))}


async def refresh_all() -> dict[str, int]:
    store = get_store()
    async with store.refresh_lock:
        return await _refresh(store)


async def _refresh(store: ThreatIntelStore) -> dict[str, int]:
    key = os.environ.get("ABUSECH_AUTH_KEY", "").strip()
    feeds = []
    if key:
        feeds.extend([
            ("urlhaus", _URLHAUS_URL.format(app_key=quote(key, safe="")), _parse_urlhaus_csv),
            ("threatfox", _THREATFOX_URL.format(app_key=quote(key, safe="")), _parse_threatfox_csv),
        ])
    else:
        log.info("threat_intel: ABUSECH_AUTH_KEY missing; using limited OpenPhish community feed")
        feeds.append(("openphish", _OPENPHISH_URL, _parse_url_list))
    phishtank_key = os.environ.get("PHISHTANK_APP_KEY", "").strip()
    if phishtank_key:
        feeds.append(("phishtank", _PHISHTANK_URL_TEMPLATE.format(
            app_key=quote(phishtank_key, safe="")), _parse_phishtank_json))
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        bodies = await asyncio.gather(*(_fetch_text(session, url) for _, url, _ in feeds))
    counts = {}
    for (name, _, parse), body in zip(feeds, bodies, strict=True):
        if not body:
            continue
        try:
            hosts = await asyncio.to_thread(parse, body)
        except Exception as exc:
            log.warning("threat_intel: %s parse failed (%s)", name, type(exc).__name__)
            continue
        if not hosts:
            log.warning("threat_intel: %s empty/invalid feed; previous snapshot retained", name)
            continue
        store.source_hosts[name] = hosts
        store.source_updated_at[name] = time.time()
        counts[name] = len(hosts)
    if counts:
        store.rebuild()
        store.last_refresh_at = time.monotonic()
        log.info("threat_intel: refresh ok: %s; retained coverage: %s", counts, store.sources)
    if not store.is_stale():
        store.outage_notified = False
    await asyncio.to_thread(save_cache, store)
    return counts
