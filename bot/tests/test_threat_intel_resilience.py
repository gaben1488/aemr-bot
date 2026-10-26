"""Regression tests for threat-feed failures and notification episodes."""
import time
from unittest.mock import AsyncMock

import pytest

from aemr_bot.services import cron
from aemr_bot.services import threat_intel as ti


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(ti, "_STORE", None)
    monkeypatch.setenv("THREAT_INTEL_CACHE_PATH", str(tmp_path / "feeds.json"))
    monkeypatch.setenv("ABUSECH_AUTH_KEY", "test-key")
    monkeypatch.delenv("PHISHTANK_APP_KEY", raising=False)


async def test_partial_failure_preserves_source_and_provenance(monkeypatch):
    bodies = {"urlhaus": '1,date,https://a.evil/x,online\n',
              "threatfox": '1,date,b.evil,domain,botnet_cc\n'}

    async def fetch(session, url):
        return bodies["urlhaus" if "urlhaus" in url else "threatfox"]

    monkeypatch.setattr(ti, "_fetch_text", fetch)
    await ti.refresh_all()
    bodies["urlhaus"] = '2,date,https://new.evil/x,online\n'
    bodies["threatfox"] = None
    await ti.refresh_all()
    store = ti.get_store()
    assert store.is_malicious("https://b.evil/") == (True, "threatfox")
    assert store.is_malicious("https://new.evil/") == (True, "urlhaus")
    assert not store.is_malicious("https://a.evil/")[0]


async def test_html_does_not_replace_valid_snapshot(monkeypatch):
    async def fetch(session, url):
        return '1,date,https://a.evil/x,online\n' if 'urlhaus' in url else None

    monkeypatch.setattr(ti, "_fetch_text", fetch)
    await ti.refresh_all()
    stamp = ti.get_store().last_refresh_at
    monkeypatch.setattr(ti, "_fetch_text", AsyncMock(return_value="<html>Login required</html>"))
    assert await ti.refresh_all() == {}
    assert ti.get_store().hosts == {"a.evil"}
    assert ti.get_store().last_refresh_at == stamp


async def test_cache_restores_snapshot_and_age(monkeypatch):
    monkeypatch.setattr(ti, "_fetch_text", AsyncMock(
        side_effect=['1,date,https://a.evil/x,online\n', None]))
    await ti.refresh_all()
    monkeypatch.setattr(ti, "_STORE", None)
    assert ti.get_store().is_malicious("https://a.evil/") == (True, "urlhaus")
    assert ti.get_store().staleness_age_seconds() < 10


def test_never_successful_becomes_stale(monkeypatch):
    store = ti.get_store()
    monkeypatch.setattr(ti.time, "monotonic", lambda: store.started_at + 7 * 3600)
    assert store.is_stale()


def test_normalization_idn_trailing_dot_and_bad_ipv6():
    assert ti._normalize_host("https://ПРИМЕР.РФ./a") == "xn--e1afmkfd.xn--p1ai"
    assert ti._normalize_host("https://[broken/a") == ""


async def test_outage_alert_once_until_recovered(monkeypatch):
    store = ti.get_store()
    store.last_refresh_at = time.monotonic() - 7 * 3600
    monkeypatch.setattr(ti, "refresh_all", AsyncMock(return_value={}))
    send = AsyncMock()
    await cron._job_threat_intel_refresh(send)
    await cron._job_threat_intel_refresh(send)
    send.assert_awaited_once()
    store.last_refresh_at = time.monotonic()
    await cron._job_threat_intel_refresh(send)
    store.last_refresh_at -= 7 * 3600
    await cron._job_threat_intel_refresh(send)
    assert send.await_count == 2


async def test_backup_empty_path_is_quiet(monkeypatch):
    from aemr_bot.config import settings
    monkeypatch.setattr(settings, "backup_local_dir", None)
    send = AsyncMock()
    await cron._job_backup_with_alert(send)
    send.assert_not_awaited()


def test_warning_does_not_reintroduce_clickable_url(monkeypatch):
    from aemr_bot.services.card_format import _maybe_url_warning
    store = ti.get_store()
    store.hosts = {"evil.com"}
    warning = _maybe_url_warning("https://evil.com/login")
    assert "⛔" in warning
    assert "https://evil.com/login" not in warning
    assert "evil.com" not in warning


def test_bare_subdomain_checked_and_defanged():
    from aemr_bot.services.card_format import _maybe_url_warning
    from aemr_bot.utils.url_defang import defang_for_admin
    ti.get_store().hosts = {"login.evil.com"}
    assert "⛔" in _maybe_url_warning("проверьте login.evil.com")
    assert "login.evil.com" not in defang_for_admin("проверьте login.evil.com")


async def test_fetch_zip_limits_redirects_and_secret_redaction(monkeypatch, caplog):
    import io
    import zipfile

    from aiohttp import web

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("full.csv", '1,date,b.evil,domain,botnet_cc\n')
    body = archive.getvalue()
    app = web.Application()
    async def serve_csv(request):
        return web.Response(body=body)

    async def redirect(request):
        raise web.HTTPFound('/secret/full.csv.zip')

    async def forbidden(request):
        return web.Response(status=403)

    app.router.add_get('/secret/full.csv.zip', serve_csv)
    app.router.add_get('/redirect', redirect)
    app.router.add_get('/secret/error', forbidden)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    root = f'http://127.0.0.1:{port}'
    try:
        async with ti.aiohttp.ClientSession() as session:
            csv_text = await ti._fetch_text(session, root + '/secret/full.csv.zip')
            assert ti._parse_threatfox_csv(csv_text) == {'b.evil'}
            assert await ti._fetch_text(session, root + '/redirect') is None
            assert await ti._fetch_text(session, root + '/secret/error') is None
            assert '/secret/' not in caplog.text
            monkeypatch.setattr(ti, '_MAX_FEED_BYTES', 1)
            assert await ti._fetch_text(session, root + '/secret/full.csv.zip') is None
    finally:
        await runner.cleanup()


async def test_notification_survives_restart_and_send_failure_retries(monkeypatch):
    store = ti.get_store()
    store.started_at -= 7 * 3600
    monkeypatch.setattr(ti, 'refresh_all', AsyncMock(return_value={}))
    monkeypatch.setattr(cron.asyncio, 'sleep', AsyncMock())
    failing = AsyncMock(side_effect=RuntimeError('MAX unavailable'))
    await cron._job_threat_intel_refresh(failing)
    assert not store.outage_notified
    working = AsyncMock()
    await cron._job_threat_intel_refresh(working)
    assert store.outage_notified
    monkeypatch.setattr(ti, '_STORE', None)
    assert ti.get_store().is_stale()
    await cron._job_threat_intel_refresh(working)
    working.assert_awaited_once()


def test_corrupt_cache_is_ignored(monkeypatch, tmp_path):
    path = tmp_path / 'corrupt.json'
    path.write_text('{broken')
    monkeypatch.setenv('THREAT_INTEL_CACHE_PATH', str(path))
    assert ti.get_store().hosts == set()


def test_partial_feed_staleness_cannot_be_hidden_by_other_refresh(monkeypatch):
    store = ti.get_store()
    store.source_hosts = {'urlhaus': {'evil.com'}, 'threatfox': {'other.com'}}
    store.source_updated_at = {'urlhaus': time.time(), 'threatfox': time.time() - 7 * 3600}
    store.last_refresh_at = time.monotonic()
    assert store.is_stale()


def test_startup_refresh_is_scheduled_immediately():
    scheduler = cron.build_scheduler(AsyncMock(), AsyncMock(), AsyncMock())
    job = scheduler.get_job('threat-intel-refresh')
    from datetime import datetime
    assert abs((job.next_run_time - datetime.now(job.next_run_time.tzinfo)).total_seconds()) < 5


def test_backup_unset_is_disabled(monkeypatch):
    from aemr_bot.config import Settings
    monkeypatch.delenv('BACKUP_LOCAL_DIR', raising=False)
    assert Settings(_env_file=None).backup_local_dir is None


async def test_without_abuse_key_uses_public_phishing_feed(monkeypatch):
    monkeypatch.delenv('ABUSECH_AUTH_KEY')
    async def fetch(session, url):
        assert url == 'https://raw.githubusercontent.com/openphish/public_feed/refs/heads/main/feed.txt'
        return 'https://known-phish.example/login\nhttps://second-phish.example/a\n'
    monkeypatch.setattr(ti, '_fetch_text', fetch)
    assert await ti.refresh_all() == {'openphish': 2}
    assert ti.get_store().is_malicious('https://known-phish.example/a') == (True, 'openphish')
    assert not ti.get_store().is_stale()
