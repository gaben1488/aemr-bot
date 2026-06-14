"""Firewall/proxy mode: единый рычаг для работы за ЛЮБЫМ межсетевым экраном.

Закрывает риск «бот за корп-фаерволом молча не выходит наружу» (вика → «Эксплуатация»):
по умолчанию maxapi/aiohttp игнорируют
HTTP(S)_PROXY (создают ClientSession без trust_env), а корпоративный CA не попадает
в trust store — за корп-фаерволом (UserGate и любым другим) бот молча не выходит на
platform-api.max.ru. Модель «врубил мод — добавил по месту — го»:

  BOT_FIREWALL_MODE=1                      # врубить (aiohttp начнёт читать прокси из окружения)
  BOT_OUTBOUND_PROXY=http://proxy:3128     # по месту: адрес корпоративного прокси
  BOT_EXTRA_CA_CERT=/certs/corp-ca.pem     # по месту: корневой сертификат, если SSL-инспекция

Два механизма:
- `apply_firewall_env()` — вызывается ОДИН раз на старте, ДО любых HTTP-клиентов:
  (1) пробрасывает BOT_OUTBOUND_PROXY в стандартные HTTP(S)_PROXY env — их читает
  aiohttp при trust_env=True, а также дочерние процессы (curl в healthwatch, rclone,
  git); (2) собирает объединённый CA-бандл (системные CA + корпоративный) и указывает
  на него SSL_CERT_FILE/REQUESTS_CA_BUNDLE — это видит stdlib ssl, aiohttp и requests.
- `session_kwargs()` — kwargs для aiohttp.ClientSession (`trust_env=True`), чтобы прокси
  из окружения реально применился. Уходят в DefaultConnectionProperties(**kwargs) →
  ClientSession (см. maxapi/bot.py ensure_session) и в наши прямые ClientSession.

Библиотека maxapi НЕ патчится — только инъекция kwargs/env.
"""

from __future__ import annotations

import os
import ssl
import tempfile
from pathlib import Path
from typing import Any

from aemr_bot.config import Settings, settings

_PROXY_ENV = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
_CA_BUNDLE_NAME = "aemr-ca-bundle.pem"


def _mask(url: str) -> str:
    """Скрыть креды в proxy-URL для лога: http://user:pass@host → http://***@host."""
    if "@" not in url:
        return url
    scheme, sep, rest = url.partition("://")
    host = rest.rpartition("@")[2]
    return f"{scheme}://***@{host}" if sep else f"***@{host}"


def _proxy_enabled(s: Settings) -> bool:
    """Прокси-режим включён: явный флаг, наш proxy (env или файл-секрет), или прокси уже в окружении."""
    return bool(
        s.firewall_mode
        or s.outbound_proxy
        or s.outbound_proxy_file
        or any(os.environ.get(v) for v in _PROXY_ENV)
    )


def _read_proxy(s: Settings) -> str | None:
    """Адрес прокси: из BOT_OUTBOUND_PROXY либо из файла-секрета BOT_OUTBOUND_PROXY_FILE.

    Файл предпочтительнее для гос-контура: пароль в env виден в `docker inspect`/`/proc`,
    а docker-secret (mode 0400, /run/secrets/...) — нет. Fail-closed: путь задан, а файла
    нет → явная ошибка на старте, а не тихий выход в интернет напрямую."""
    if s.outbound_proxy:
        return s.outbound_proxy
    if s.outbound_proxy_file:
        pf = Path(s.outbound_proxy_file).expanduser()
        if not pf.is_file():
            raise FileNotFoundError(f"BOT_OUTBOUND_PROXY_FILE не найден: {s.outbound_proxy_file}")
        return pf.read_text(encoding="utf-8").strip() or None
    return None


def session_kwargs(s: Settings | None = None) -> dict[str, Any]:
    """kwargs для aiohttp.ClientSession (распаковываются как `ClientSession(**kwargs)`).

    `trust_env=True` заставляет aiohttp читать HTTP(S)_PROXY / NO_PROXY / .netrc из
    окружения. Вне прокси-режима возвращает пусто — поведение бота не меняется.
    Тип значения — Any: ключи уходят keyword-аргументами в перегруженный ClientSession.
    """
    s = s or settings
    return {"trust_env": True} if _proxy_enabled(s) else {}


def _build_ca_bundle(extra_ca: str) -> str:
    """Объединить системные CA с корпоративным в один файл, вернуть путь к нему.

    Объединение (а не замена) сохраняет доверие к публичным CA — это нужно при
    ВЫБОРОЧНОЙ SSL-инспекции, когда подменяется только часть трафика. Пишем в tmp:
    в контейнере /tmp — tmpfs, доступна на запись даже при read-only rootfs.

    Fail-closed: BOT_EXTRA_CA_CERT обязан быть валидным PEM-СЕРТИФИКАТОМ (не приватным
    ключом, не мусором). Иначе бандл собрался бы, но stdlib ssl молча проигнорировал бы
    битый блок → бот упал бы на TLS уже в проде непонятной ошибкой, а не на старте.
    """
    extra = Path(extra_ca).expanduser()
    if not extra.is_file():
        raise FileNotFoundError(f"BOT_EXTRA_CA_CERT не найден: {extra_ca}")
    try:
        extra_text = extra.read_text(encoding="utf-8")
        # cadata требует ASCII-PEM; non-ASCII (мусор/не-сертификат) даёт TypeError, битый
        # сертификат — ssl.SSLError. Любой из них = невалидный CA → падаем на старте.
        ssl.create_default_context().load_verify_locations(cadata=extra_text)
    except (ssl.SSLError, UnicodeDecodeError, ValueError, TypeError) as e:
        raise ValueError(
            f"BOT_EXTRA_CA_CERT не является валидным PEM-сертификатом ({extra_ca}): {e}"
        ) from e
    parts: list[str] = []
    sys_ca = ssl.get_default_verify_paths().cafile
    if sys_ca and Path(sys_ca).is_file():
        parts.append(Path(sys_ca).read_text(encoding="utf-8", errors="ignore"))
    parts.append(extra_text)
    out = Path(tempfile.gettempdir()) / _CA_BUNDLE_NAME
    out.write_text("\n".join(parts) + "\n", encoding="utf-8")
    os.chmod(out, 0o600)  # defense-in-depth: бандл не перетереть/не прочитать чужим (CA публичен, но для аудита)
    return str(out)


def apply_firewall_env(s: Settings | None = None) -> list[str]:
    """Применить настройки межсетевика ДО создания HTTP-клиентов. Идемпотентно.

    Возвращает список применённых мер (для лога). Явно заданные оператором env НЕ
    перетираем (`setdefault`) — ручная настройка всегда главнее нашего конфига;
    CA-бандл собираем заново при каждом старте (детерминированно), это не секрет.
    """
    s = s or settings
    applied: list[str] = []
    proxy = _read_proxy(s)
    if proxy:
        for var in _PROXY_ENV:
            os.environ.setdefault(var, proxy)
        applied.append(f"proxy={_mask(proxy)}")
    if s.no_proxy:
        os.environ.setdefault("NO_PROXY", s.no_proxy)
        os.environ.setdefault("no_proxy", s.no_proxy)
        applied.append(f"no_proxy={s.no_proxy}")
    if s.extra_ca_cert:
        bundle = _build_ca_bundle(s.extra_ca_cert)
        os.environ["SSL_CERT_FILE"] = bundle
        os.environ["REQUESTS_CA_BUNDLE"] = bundle
        applied.append(f"extra_ca={s.extra_ca_cert}")
    if _proxy_enabled(s) and not any(a.startswith("proxy=") for a in applied):
        applied.append("trust_env=True (HTTP(S)_PROXY из окружения)")
    return applied
