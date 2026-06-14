"""Тесты firewall/proxy mode (aemr_bot.network): прокси из окружения + кастомный CA.

Проверяем логику, а не сеть: session_kwargs (включается верно), apply_firewall_env
(пробрасывает прокси в env, не перетирая ручное; собирает CA-бандл), маскирование кредов.
"""

import os
from pathlib import Path

import pytest

from aemr_bot import network
from aemr_bot.config import Settings

_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")
_CA_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
_VALID_CA = Path(__file__).parent / "fixtures" / "test_ca.pem"  # self-signed PEM-сертификат


def _clean_env(monkeypatch):
    for v in (*_PROXY_VARS, *_CA_VARS):
        monkeypatch.delenv(v, raising=False)


def _settings(monkeypatch, **env) -> Settings:
    base = {"BOT_TOKEN": "t", "DATABASE_URL": "sqlite+aiosqlite:///:memory:"}
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, str(v))
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_session_kwargs_off_by_default(monkeypatch):
    _clean_env(monkeypatch)
    s = _settings(monkeypatch)
    assert network.session_kwargs(s) == {}


def test_session_kwargs_on_with_firewall_mode(monkeypatch):
    _clean_env(monkeypatch)
    s = _settings(monkeypatch, BOT_FIREWALL_MODE="1")
    assert network.session_kwargs(s) == {"trust_env": True}


def test_session_kwargs_on_with_explicit_proxy(monkeypatch):
    _clean_env(monkeypatch)
    s = _settings(monkeypatch, BOT_OUTBOUND_PROXY="http://proxy:3128")
    assert network.session_kwargs(s) == {"trust_env": True}


def test_session_kwargs_on_when_proxy_already_in_env(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://preset:8080")
    s = _settings(monkeypatch)
    assert network.session_kwargs(s) == {"trust_env": True}


def test_apply_proxy_sets_env_and_masks_creds(monkeypatch):
    _clean_env(monkeypatch)
    s = _settings(monkeypatch, BOT_OUTBOUND_PROXY="http://user:pass@proxy:3128", BOT_NO_PROXY="localhost")
    applied = network.apply_firewall_env(s)
    assert os.environ["HTTPS_PROXY"] == "http://user:pass@proxy:3128"
    assert os.environ["HTTP_PROXY"] == "http://user:pass@proxy:3128"
    assert os.environ["NO_PROXY"] == "localhost"
    # креды НЕ светятся в логе-списке
    joined = " ".join(applied)
    assert "***@proxy:3128" in joined
    assert "user:pass" not in joined


def test_apply_proxy_does_not_override_manual_env(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://preset:8080")
    s = _settings(monkeypatch, BOT_OUTBOUND_PROXY="http://ours:3128")
    network.apply_firewall_env(s)
    # setdefault: ручная настройка оператора главнее нашего конфига
    assert os.environ["HTTPS_PROXY"] == "http://preset:8080"


def test_apply_extra_ca_builds_combined_bundle(monkeypatch):
    _clean_env(monkeypatch)
    s = _settings(monkeypatch, BOT_EXTRA_CA_CERT=str(_VALID_CA))
    applied = network.apply_firewall_env(s)
    bundle = os.environ["SSL_CERT_FILE"]
    assert os.environ["REQUESTS_CA_BUNDLE"] == bundle
    assert Path(bundle).is_file()
    corp = _VALID_CA.read_text(encoding="utf-8").strip()
    assert corp in Path(bundle).read_text(encoding="utf-8")  # корп-CA попал в объединённый бандл
    assert any("extra_ca" in a for a in applied)
    if os.name == "posix":  # chmod 0600 — defense-in-depth (на Windows mode-биты не те)
        assert os.stat(bundle).st_mode & 0o077 == 0


def test_apply_extra_ca_invalid_fails_closed(monkeypatch, tmp_path):
    # битый CA (не сертификат) обязан падать на СТАРТЕ, а не позже на TLS в проде
    _clean_env(monkeypatch)
    bad = tmp_path / "not-a-cert.pem"
    bad.write_text("-----BEGIN CERTIFICATE-----\nCORPCAMARKER-не-base64\n-----END CERTIFICATE-----\n", encoding="utf-8")
    s = _settings(monkeypatch, BOT_EXTRA_CA_CERT=str(bad))
    with pytest.raises(ValueError, match="не является валидным PEM"):
        network.apply_firewall_env(s)


def test_build_ca_bundle_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        network._build_ca_bundle(str(tmp_path / "does-not-exist.pem"))


def test_proxy_from_file_secret(monkeypatch, tmp_path):
    # прокси из файла-секрета (docker secret), а не из env — пароль не виден в docker inspect
    _clean_env(monkeypatch)
    secret = tmp_path / "proxy_url"
    secret.write_text("http://user:pass@proxy:3128\n", encoding="utf-8")
    s = _settings(monkeypatch, BOT_OUTBOUND_PROXY_FILE=str(secret))
    applied = network.apply_firewall_env(s)
    assert os.environ["HTTPS_PROXY"] == "http://user:pass@proxy:3128"
    assert network.session_kwargs(s) == {"trust_env": True}
    assert "***@proxy:3128" in " ".join(applied)


def test_proxy_file_missing_fails_closed(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    s = _settings(monkeypatch, BOT_OUTBOUND_PROXY_FILE=str(tmp_path / "nope"))
    with pytest.raises(FileNotFoundError):
        network.apply_firewall_env(s)


def test_apply_noop_without_settings(monkeypatch):
    _clean_env(monkeypatch)
    s = _settings(monkeypatch)
    assert network.apply_firewall_env(s) == []
    assert "SSL_CERT_FILE" not in os.environ


def test_mask_hides_credentials():
    assert network._mask("http://u:p@host:3128") == "http://***@host:3128"
    assert network._mask("http://host:3128") == "http://host:3128"
