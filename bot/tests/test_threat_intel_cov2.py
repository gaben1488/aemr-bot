"""Покрытие непокрытых веток services/threat_intel.

Базовый test_threat_intel.py покрывает parser'ы и ThreatIntelStore, но
оставляет без тестов:
- _normalize_host: ветку urlparse-исключения и host, который становится
  пустым после среза www.
- is_malicious: ранний выход при host == "" (строка 84).
- _fetch_text: успешный 200, не-200 статус, исключение сети.
- _parse_*: короткие/битые строки (len(parts) < N), non-dict элементы.
- refresh_all: happy-path с двумя feed'ами, ветку PHISHTANK_APP_KEY,
  ветку «ни один feed не отдался» (counts пуст → store не трогаем),
  ветку успешного обновления (hosts/sources/last_refresh_at).

_fetch_text принимает session параметром — подменяем фейковым async
context-manager'ом. refresh_all создаёт ClientSession внутри, поэтому
для него monkeypatch'им _fetch_text целиком.
"""
from __future__ import annotations

import time

import pytest

from aemr_bot.services import threat_intel as ti


class _FakeResp:
    def __init__(self, status: int, text: str) -> None:
        self._status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def status(self) -> int:
        return self._status

    async def text(self) -> str:
        return self._text


class _FakeSession:
    """session.get(url) → async-context-manager с заранее заданным ответом."""

    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp

    def get(self, url, timeout=None):  # noqa: ARG002
        return self._resp


class _RaisingSession:
    def get(self, url, timeout=None):  # noqa: ARG002
        raise RuntimeError("network down")


class TestNormalizeHostEdges:
    def test_host_that_is_only_www_becomes_empty(self) -> None:
        # "www." → после среза остаётся "" — покрывает return пустой строки.
        assert ti._normalize_host("http://www./path") == ""

    def test_bare_www_prefix_stripped(self) -> None:
        assert ti._normalize_host("www.bad.test") == "bad.test"

    def test_is_malicious_empty_host_short_circuits(self) -> None:
        # URL с непустым хостом в set, но проверяем URL без извлекаемого
        # host'а → ветка `if not host: return False` (строка 84).
        store = ti.ThreatIntelStore(hosts={"bad.test"})
        is_bad, src = store.is_malicious("http:///no-host-here")
        assert is_bad is False
        assert src is None


class TestFetchText:
    @pytest.mark.asyncio
    async def test_returns_body_on_200(self) -> None:
        body = await ti._fetch_text(_FakeSession(_FakeResp(200, "PAYLOAD")), "http://x")
        assert body == "PAYLOAD"

    @pytest.mark.asyncio
    async def test_none_on_non_200(self) -> None:
        assert await ti._fetch_text(_FakeSession(_FakeResp(503, "")), "http://x") is None

    @pytest.mark.asyncio
    async def test_none_on_exception(self) -> None:
        assert await ti._fetch_text(_RaisingSession(), "http://x") is None


class TestParserShortRows:
    def test_urlhaus_skips_rows_with_too_few_columns(self) -> None:
        body = "1,only-two-cols\n2,date,https://kept.test/x,extra\n"
        assert ti._parse_urlhaus_csv(body) == {"kept.test"}

    def test_urlhaus_skips_rows_whose_url_yields_empty_host(self) -> None:
        # 3 колонки есть, но url-колонка пустая → host "" → строка пропущена
        # (ветка `if host:` ложна, продолжаем цикл).
        body = "1,date,,extra\n2,date,https://kept.test/,x\n"
        assert ti._parse_urlhaus_csv(body) == {"kept.test"}

    def test_threatfox_skips_single_token_lines(self) -> None:
        body = "0.0.0.0\n0.0.0.0 kept.test\n"
        assert ti._parse_threatfox_hostfile(body) == {"kept.test"}

    def test_threatfox_skips_token_that_yields_empty_host(self) -> None:
        # 2 токена, но 2-й ("www.") нормализуется в "" → строка пропущена.
        body = "0.0.0.0 www.\n0.0.0.0 kept.test\n"
        assert ti._parse_threatfox_hostfile(body) == {"kept.test"}

    def test_phishtank_skips_non_dict_items(self) -> None:
        body = '["just-a-string", 42, {"url": "https://kept.test/login"}]'
        assert ti._parse_phishtank_json(body) == {"kept.test"}

    def test_phishtank_skips_non_string_url(self) -> None:
        body = '[{"url": 123}, {"url": "https://kept.test/"}]'
        assert ti._parse_phishtank_json(body) == {"kept.test"}


class TestRefreshAll:
    @pytest.fixture(autouse=True)
    def _isolated_store(self, monkeypatch):
        """Свежий singleton на каждый тест, чтобы не зависеть от порядка."""
        monkeypatch.setattr(ti, "_STORE", None)
        # PhishTank по умолчанию выключен — убираем ключ из env.
        monkeypatch.delenv("PHISHTANK_APP_KEY", raising=False)

    @pytest.mark.asyncio
    async def test_two_feeds_populate_store(self, monkeypatch) -> None:
        async def fake_fetch(session, url):  # noqa: ARG001
            if url == ti._URLHAUS_URL:
                return "1,date,https://a.evil/x,extra\n"
            if url == ti._THREATFOX_URL:
                return "0.0.0.0 b.evil\n"
            return None

        monkeypatch.setattr(ti, "_fetch_text", fake_fetch)
        counts = await ti.refresh_all()
        assert counts == {"urlhaus": 1, "threatfox": 1}
        store = ti.get_store()
        assert store.hosts == {"a.evil", "b.evil"}
        assert store.sources == {"urlhaus": 1, "threatfox": 1}
        assert store.last_refresh_at is not None

    @pytest.mark.asyncio
    async def test_phishtank_included_when_key_set(self, monkeypatch) -> None:
        monkeypatch.setenv("PHISHTANK_APP_KEY", "secret-key")
        seen_urls: list[str] = []

        async def fake_fetch(session, url):  # noqa: ARG001
            seen_urls.append(url)
            if url == ti._URLHAUS_URL:
                return "1,date,https://a.evil/x,extra\n"
            if url == ti._THREATFOX_URL:
                return "0.0.0.0 b.evil\n"
            # phishtank URL
            return '[{"url": "https://c.phish/login"}]'

        monkeypatch.setattr(ti, "_fetch_text", fake_fetch)
        counts = await ti.refresh_all()
        assert counts.get("phishtank") == 1
        assert any("secret-key" in u for u in seen_urls)
        assert "c.phish" in ti.get_store().hosts

    @pytest.mark.asyncio
    async def test_no_feed_responds_keeps_old_set(self, monkeypatch) -> None:
        # Предзаполняем store «старым» набором.
        store = ti.get_store()
        store.hosts = {"old.evil"}
        store.last_refresh_at = time.monotonic() - 100

        async def fake_fetch(session, url):  # noqa: ARG001
            return None  # все feed'ы лежат

        monkeypatch.setattr(ti, "_fetch_text", fake_fetch)
        counts = await ti.refresh_all()
        assert counts == {}
        # Старый набор и timestamp не тронуты — fail-open.
        assert ti.get_store().hosts == {"old.evil"}
