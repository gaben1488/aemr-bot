"""Тесты на services/threat_intel.py — URL threat-intel set + parse."""
from __future__ import annotations

from aemr_bot.services.threat_intel import (
    ThreatIntelStore,
    _normalize_host,
    _parse_phishtank_json,
    _parse_threatfox_hostfile,
    _parse_urlhaus_csv,
)


class TestNormalizeHost:
    def test_full_url(self) -> None:
        assert _normalize_host("https://www.Attacker.com/path") == "attacker.com"

    def test_no_scheme(self) -> None:
        assert _normalize_host("evil.example") == "evil.example"

    def test_with_port(self) -> None:
        assert _normalize_host("http://evil.example:8080/x") == "evil.example"

    def test_uppercase_lowered(self) -> None:
        assert _normalize_host("https://EVIL.COM/") == "evil.com"

    def test_empty(self) -> None:
        assert _normalize_host("") == ""

    def test_garbage(self) -> None:
        # Не должно падать на любом мусоре
        assert _normalize_host("not-a-url") == "not-a-url"


class TestParseUrlhausCSV:
    def test_skip_comments(self) -> None:
        body = (
            "# Comment line\n"
            "1,2024-01-01,https://malware.test/payload.exe,foo,bar\n"
        )
        hosts = _parse_urlhaus_csv(body)
        assert "malware.test" in hosts

    def test_multiple_lines(self) -> None:
        body = (
            "1,date,https://a.evil/,extra\n"
            "2,date,http://b.evil/,extra\n"
        )
        hosts = _parse_urlhaus_csv(body)
        assert hosts == {"a.evil", "b.evil"}

    def test_empty_body(self) -> None:
        assert _parse_urlhaus_csv("") == set()


class TestParseThreatfoxHostfile:
    def test_zero_zero_format(self) -> None:
        body = "0.0.0.0 evil.example\n0.0.0.0 phish.test\n"
        hosts = _parse_threatfox_hostfile(body)
        assert hosts == {"evil.example", "phish.test"}

    def test_skip_comments_and_empty(self) -> None:
        body = "# comment\n\n0.0.0.0 real.bad\n"
        hosts = _parse_threatfox_hostfile(body)
        assert hosts == {"real.bad"}


class TestParsePhishtankJson:
    def test_parses_url_field(self) -> None:
        body = '[{"url": "https://phisher.test/login"}, {"url": "http://b.evil/"}]'
        hosts = _parse_phishtank_json(body)
        assert hosts == {"phisher.test", "b.evil"}

    def test_invalid_json_returns_empty(self) -> None:
        assert _parse_phishtank_json("not json {") == set()

    def test_non_list_returns_empty(self) -> None:
        assert _parse_phishtank_json('{"object": true}') == set()


class TestThreatIntelStore:
    def test_empty_store_returns_false(self) -> None:
        store = ThreatIntelStore()
        is_bad, source = store.is_malicious("https://anywhere.com")
        assert is_bad is False
        assert source is None

    def test_malicious_host_caught(self) -> None:
        store = ThreatIntelStore(hosts={"evil.example"})
        is_bad, source = store.is_malicious("https://www.Evil.example/path")
        assert is_bad is True
        assert source == "threat-intel"

    def test_clean_host_passes(self) -> None:
        store = ThreatIntelStore(hosts={"evil.example"})
        is_bad, _ = store.is_malicious("https://elizovomr.ru/page")
        assert is_bad is False

    def test_staleness_age_none_before_refresh(self) -> None:
        store = ThreatIntelStore()
        assert store.staleness_age_seconds() is None
        assert store.is_stale() is False  # None ≠ stale, скорее «не начали»

    def test_is_stale_after_budget(self) -> None:
        import time
        store = ThreatIntelStore()
        # 7 часов назад
        store.last_refresh_at = time.monotonic() - 7 * 3600
        assert store.is_stale() is True
