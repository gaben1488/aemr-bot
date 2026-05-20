"""Тесты `services/repo_sync.py` — клиент GitHub REST для синхронизации настроек.

Модуль ходит в GitHub API без PyGithub (меньше CVE-surface). Тесты
покрывают:
- Pure helpers (`serialize_runtime_config`, `_make_branch_name`,
  `_build_commit_message`, `_build_pr_body`) — без сети.
- `load_config_from_env_and_settings` — happy path + None при пустых.
- `_GH._request` + методы (`get_ref_sha`, `create_branch`,
  `get_file_sha`, `put_file`, `create_pr`) — моки aiohttp.
- `create_settings_pr` — orchestrator: успех + каждая точка отказа
  (no_changes, no_base_branch, branch_failed, write_failed, pr_failed).
- `fetch_main_runtime_config` — 200/404/fetch_failed/parse_failed.

Дизайн: aiohttp.ClientSession мокается на уровне модуля (`session_cm`),
`_GH` — на уровне класса в orchestrator-тестах; для прямых тестов
методов `_GH` патчим `_request` через `patch.object`. Это разделяет
unit-тесты HTTP-обёртки и тесты бизнес-логики.
"""
from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aemr_bot.services import repo_sync


# ---- pure helpers -----------------------------------------------------------


class TestSerializeRuntimeConfig:
    def test_deterministic_key_order(self) -> None:
        d = {"z": 1, "a": 2, "m": 3}
        out = repo_sync.serialize_runtime_config(d)
        # ключи отсортированы — диф в git минимальный
        assert out.find('"a"') < out.find('"m"') < out.find('"z"')

    def test_ends_with_newline(self) -> None:
        # Без trailing \n git показывает «no newline at end of file» —
        # лишний шум в каждом PR. Контракт: всегда финальная пустая строка.
        out = repo_sync.serialize_runtime_config({"k": "v"})
        assert out.endswith("\n")

    def test_unicode_preserved(self) -> None:
        # ensure_ascii=False → кириллица не уродуется в \uXXXX
        out = repo_sync.serialize_runtime_config({"welcome": "Привет"})
        assert "Привет" in out

    def test_indent_2(self) -> None:
        out = repo_sync.serialize_runtime_config({"k": "v"})
        assert '\n  "k"' in out


class TestMakeBranchName:
    def test_pattern_and_length(self) -> None:
        n = repo_sync._make_branch_name()
        assert n.startswith("bot-config-sync-")
        # YYYYMMDD-HHMMSS = 15 символов после префикса
        assert len(n) == len("bot-config-sync-") + 15

    def test_uses_utc(self) -> None:
        # Тест что utcnow используется, а не localtime: проверяем что
        # имя — это UTC timestamp на момент вызова, не локальный.
        from datetime import datetime, timezone
        n = repo_sync._make_branch_name()
        # Парсим обратно
        ts_str = n.removeprefix("bot-config-sync-")
        parsed = datetime.strptime(ts_str, "%Y%m%d-%H%M%S")
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        # В пределах 5 секунд от now-UTC
        delta = abs((now_utc - parsed).total_seconds())
        assert delta < 5


class TestBuildCommitMessage:
    def test_no_keys_generic_header(self) -> None:
        msg = repo_sync._build_commit_message([])
        assert "config: sync settings from bot menu" in msg

    def test_single_key_named(self) -> None:
        msg = repo_sync._build_commit_message(["welcome_text"])
        assert "config: update welcome_text" in msg
        assert "- welcome_text" in msg

    def test_multi_key_count_and_bullets(self) -> None:
        msg = repo_sync._build_commit_message(["a", "b", "c"])
        assert "config: sync 3 settings" in msg
        for k in ("a", "b", "c"):
            assert f"- {k}" in msg


class TestBuildPrBody:
    def test_includes_operator_meta(self) -> None:
        body = repo_sync._build_pr_body(["welcome_text"], "Иван", 42)
        assert "Иван" in body
        # operator_id фиксируется в body для audit
        assert "max_user_id=42" in body
        assert "welcome_text" in body

    def test_mentions_auto_deploy(self) -> None:
        body = repo_sync._build_pr_body(["k"], "X", 1)
        assert "auto-deploy" in body


# ---- load_config_from_env_and_settings -------------------------------------


class TestLoadConfigFromEnv:
    def test_happy(self, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_PAT", "ghp_xxxx")
        monkeypatch.setenv("GITHUB_REPO", "owner/repo")
        monkeypatch.setenv("GITHUB_PR_BASE_BRANCH", "main")
        cfg = repo_sync.load_config_from_env_and_settings(
            author_name="Bot", author_email="bot@example.com"
        )
        assert cfg is not None
        assert cfg.pat == "ghp_xxxx"
        assert cfg.repo == "owner/repo"
        assert cfg.base_branch == "main"
        assert cfg.author_name == "Bot"
        assert cfg.author_email == "bot@example.com"

    def test_repo_default(self, monkeypatch) -> None:
        # GITHUB_REPO не задан → дефолт Gaben1488/aemr-bot
        monkeypatch.setenv("GITHUB_PAT", "ghp_xxxx")
        monkeypatch.delenv("GITHUB_REPO", raising=False)
        cfg = repo_sync.load_config_from_env_and_settings(
            author_name="Bot", author_email="bot@example.com"
        )
        assert cfg is not None
        assert cfg.repo == "Gaben1488/aemr-bot"

    def test_no_pat_returns_none(self, monkeypatch) -> None:
        # Feature-flag: без PAT модуль выключен, бот не падает.
        monkeypatch.delenv("GITHUB_PAT", raising=False)
        cfg = repo_sync.load_config_from_env_and_settings(
            author_name="Bot", author_email="bot@example.com"
        )
        assert cfg is None

    def test_no_author_returns_none(self, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_PAT", "ghp_xxxx")
        cfg = repo_sync.load_config_from_env_and_settings(
            author_name=None, author_email="bot@example.com"
        )
        assert cfg is None

    def test_strips_whitespace(self, monkeypatch) -> None:
        # Хвостовой \n или пробел в .env не должен ломать конфиг.
        monkeypatch.setenv("GITHUB_PAT", "  ghp_xxxx \n")
        monkeypatch.setenv("GITHUB_REPO", "owner/repo")
        cfg = repo_sync.load_config_from_env_and_settings(
            author_name="  Bot  ", author_email="  bot@example.com  "
        )
        assert cfg is not None
        assert cfg.pat == "ghp_xxxx"
        assert cfg.author_name == "Bot"


# ---- _GH wrapper (HTTP) ----------------------------------------------------


def _cfg() -> repo_sync.SyncConfig:
    return repo_sync.SyncConfig(
        pat="ghp_test",
        repo="owner/repo",
        base_branch="main",
        author_name="Bot",
        author_email="bot@example.com",
    )


class _MockResponse:
    """Async-context-manager имитирующий aiohttp response."""

    def __init__(self, status: int, data=None, raise_on_json: bool = False) -> None:
        self.status = status
        self._data = data
        self._raise = raise_on_json

    async def json(self):
        if self._raise:
            import aiohttp
            raise aiohttp.ContentTypeError(MagicMock(), MagicMock())
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


def _session_with(response: _MockResponse) -> MagicMock:
    session = MagicMock()
    session.request = MagicMock(return_value=response)
    return session


class TestGHRequest:
    @pytest.mark.asyncio
    async def test_request_ok(self) -> None:
        gh = repo_sync._GH(_cfg())
        session = _session_with(_MockResponse(200, {"key": "value"}))
        status, data = await gh._request(session, "GET", "/path")
        assert status == 200
        assert data == {"key": "value"}

    @pytest.mark.asyncio
    async def test_authorization_header_uses_pat(self) -> None:
        # PAT попадает в Bearer header, и только туда.
        gh = repo_sync._GH(_cfg())
        assert gh._headers["Authorization"] == "Bearer ghp_test"
        # PAT не утекает в User-Agent
        assert "ghp_test" not in gh._headers["User-Agent"]

    @pytest.mark.asyncio
    async def test_non_json_response_returns_none_data(self) -> None:
        # Сервер вернул не-JSON (например, HTML 502) — не падаем,
        # возвращаем (status, None).
        gh = repo_sync._GH(_cfg())
        session = _session_with(_MockResponse(502, raise_on_json=True))
        status, data = await gh._request(session, "GET", "/path")
        assert status == 502
        assert data is None


class TestGHMethods:
    @pytest.mark.asyncio
    async def test_get_ref_sha_ok(self) -> None:
        gh = repo_sync._GH(_cfg())
        with patch.object(
            gh, "_request",
            AsyncMock(return_value=(200, {"object": {"sha": "abc123"}})),
        ):
            sha = await gh.get_ref_sha(MagicMock(), "main")
        assert sha == "abc123"

    @pytest.mark.asyncio
    async def test_get_ref_sha_404_returns_none(self) -> None:
        gh = repo_sync._GH(_cfg())
        with patch.object(
            gh, "_request", AsyncMock(return_value=(404, None)),
        ):
            sha = await gh.get_ref_sha(MagicMock(), "nonexistent")
        assert sha is None

    @pytest.mark.asyncio
    async def test_get_ref_sha_malformed_returns_none(self) -> None:
        # 200, но в payload нет object.sha → не падаем, None
        gh = repo_sync._GH(_cfg())
        with patch.object(
            gh, "_request", AsyncMock(return_value=(200, {"weird": True})),
        ):
            sha = await gh.get_ref_sha(MagicMock(), "main")
        assert sha is None

    @pytest.mark.asyncio
    async def test_create_branch_ok(self) -> None:
        gh = repo_sync._GH(_cfg())
        with patch.object(gh, "_request", AsyncMock(return_value=(201, {}))):
            ok = await gh.create_branch(MagicMock(), "br", "sha")
        assert ok is True

    @pytest.mark.asyncio
    async def test_create_branch_already_exists(self) -> None:
        # 422 — ветка уже существует (второй клик в ту же секунду).
        # Контракт: возвращаем False, выше слой даст понятную ошибку.
        gh = repo_sync._GH(_cfg())
        with patch.object(gh, "_request", AsyncMock(return_value=(422, {}))):
            ok = await gh.create_branch(MagicMock(), "br", "sha")
        assert ok is False

    @pytest.mark.asyncio
    async def test_get_file_sha_ok(self) -> None:
        gh = repo_sync._GH(_cfg())
        with patch.object(
            gh, "_request",
            AsyncMock(return_value=(200, {"sha": "blob_sha"})),
        ):
            sha = await gh.get_file_sha(MagicMock(), "path", "main")
        assert sha == "blob_sha"

    @pytest.mark.asyncio
    async def test_get_file_sha_404(self) -> None:
        # Файла нет в репо (первый раз создаём) — None.
        gh = repo_sync._GH(_cfg())
        with patch.object(gh, "_request", AsyncMock(return_value=(404, None))):
            sha = await gh.get_file_sha(MagicMock(), "path", "main")
        assert sha is None

    @pytest.mark.asyncio
    async def test_put_file_creates_new(self) -> None:
        # sha=None → 201 Created (новый файл)
        gh = repo_sync._GH(_cfg())
        with patch.object(gh, "_request", AsyncMock(return_value=(201, {}))):
            ok = await gh.put_file(
                MagicMock(), path="p", branch="b",
                content_str="x", message="m", sha=None,
            )
        assert ok is True

    @pytest.mark.asyncio
    async def test_put_file_updates_existing_with_sha(self) -> None:
        # sha задан → 200 OK (обновление), sha попадает в body
        gh = repo_sync._GH(_cfg())
        req = AsyncMock(return_value=(200, {}))
        with patch.object(gh, "_request", req):
            await gh.put_file(
                MagicMock(), path="p", branch="b",
                content_str="x", message="m", sha="old_sha",
            )
        body = req.await_args.kwargs["json_body"]
        assert body["sha"] == "old_sha"
        # content base64-encoded
        assert body["content"] == base64.b64encode(b"x").decode("ascii")
        # committer и author — оба из cfg
        assert body["committer"]["name"] == "Bot"
        assert body["author"]["email"] == "bot@example.com"

    @pytest.mark.asyncio
    async def test_put_file_fail(self) -> None:
        gh = repo_sync._GH(_cfg())
        with patch.object(gh, "_request", AsyncMock(return_value=(403, {}))):
            ok = await gh.put_file(
                MagicMock(), path="p", branch="b",
                content_str="x", message="m", sha=None,
            )
        assert ok is False

    @pytest.mark.asyncio
    async def test_create_pr_ok(self) -> None:
        gh = repo_sync._GH(_cfg())
        with patch.object(
            gh, "_request",
            AsyncMock(return_value=(201, {
                "number": 42, "html_url": "https://github.com/owner/repo/pull/42",
            })),
        ):
            n, u = await gh.create_pr(
                MagicMock(), title="t", head="br", base="main", body="b",
            )
        assert n == 42
        assert u == "https://github.com/owner/repo/pull/42"

    @pytest.mark.asyncio
    async def test_create_pr_fail(self) -> None:
        gh = repo_sync._GH(_cfg())
        with patch.object(gh, "_request", AsyncMock(return_value=(422, {}))):
            n, u = await gh.create_pr(
                MagicMock(), title="t", head="br", base="main", body="b",
            )
        assert n is None
        assert u is None


# ---- create_settings_pr orchestration --------------------------------------


def _aiohttp_session_patch():
    """Контекст-менеджер `aiohttp.ClientSession(...) as session` —
    наш orchestrator его открывает. Возвращаем фейк-сессию, через
    которую methods _GH-инстанса не должны ходить (они уже мокаются)."""
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return cm


class TestCreateSettingsPr:
    @pytest.mark.asyncio
    async def test_no_dirty_keys_returns_no_changes(self) -> None:
        # Контракт: пустой список dirty_keys → не лезем в API,
        # сразу возвращаем reason='no_changes'.
        result = await repo_sync.create_settings_pr(
            _cfg(),
            runtime_config={"x": 1},
            dirty_keys=[],
            operator_name="X",
            operator_id=1,
        )
        assert result.ok is False
        assert result.reason == "no_changes"

    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        with patch.object(
            repo_sync.aiohttp, "ClientSession", _aiohttp_session_patch(),
        ), patch.object(repo_sync, "_GH") as gh_cls:
            gh = gh_cls.return_value
            gh.get_ref_sha = AsyncMock(return_value="base_sha")
            gh.create_branch = AsyncMock(return_value=True)
            gh.get_file_sha = AsyncMock(return_value="file_sha")
            gh.put_file = AsyncMock(return_value=True)
            gh.create_pr = AsyncMock(
                return_value=(42, "https://github.com/owner/repo/pull/42"),
            )
            result = await repo_sync.create_settings_pr(
                _cfg(),
                runtime_config={"k": "v"},
                dirty_keys=["k"],
                operator_name="Ivan",
                operator_id=42,
            )
        assert result.ok is True
        assert result.pr_number == 42
        assert "pull/42" in (result.pr_url or "")
        # branch_name содержит префикс
        assert (result.branch or "").startswith("bot-config-sync-")

    @pytest.mark.asyncio
    async def test_no_base_branch(self) -> None:
        with patch.object(
            repo_sync.aiohttp, "ClientSession", _aiohttp_session_patch(),
        ), patch.object(repo_sync, "_GH") as gh_cls:
            gh = gh_cls.return_value
            gh.get_ref_sha = AsyncMock(return_value=None)
            result = await repo_sync.create_settings_pr(
                _cfg(), runtime_config={"k": "v"},
                dirty_keys=["k"], operator_name="X", operator_id=1,
            )
        assert result.ok is False
        assert result.reason == "no_base_branch"
        assert "main" in result.message

    @pytest.mark.asyncio
    async def test_branch_failed(self) -> None:
        with patch.object(
            repo_sync.aiohttp, "ClientSession", _aiohttp_session_patch(),
        ), patch.object(repo_sync, "_GH") as gh_cls:
            gh = gh_cls.return_value
            gh.get_ref_sha = AsyncMock(return_value="base_sha")
            gh.create_branch = AsyncMock(return_value=False)
            result = await repo_sync.create_settings_pr(
                _cfg(), runtime_config={"k": "v"},
                dirty_keys=["k"], operator_name="X", operator_id=1,
            )
        assert result.ok is False
        assert result.reason == "branch_failed"

    @pytest.mark.asyncio
    async def test_write_failed(self) -> None:
        with patch.object(
            repo_sync.aiohttp, "ClientSession", _aiohttp_session_patch(),
        ), patch.object(repo_sync, "_GH") as gh_cls:
            gh = gh_cls.return_value
            gh.get_ref_sha = AsyncMock(return_value="base_sha")
            gh.create_branch = AsyncMock(return_value=True)
            gh.get_file_sha = AsyncMock(return_value="file_sha")
            gh.put_file = AsyncMock(return_value=False)
            result = await repo_sync.create_settings_pr(
                _cfg(), runtime_config={"k": "v"},
                dirty_keys=["k"], operator_name="X", operator_id=1,
            )
        assert result.ok is False
        assert result.reason == "write_failed"

    @pytest.mark.asyncio
    async def test_pr_failed(self) -> None:
        with patch.object(
            repo_sync.aiohttp, "ClientSession", _aiohttp_session_patch(),
        ), patch.object(repo_sync, "_GH") as gh_cls:
            gh = gh_cls.return_value
            gh.get_ref_sha = AsyncMock(return_value="base_sha")
            gh.create_branch = AsyncMock(return_value=True)
            gh.get_file_sha = AsyncMock(return_value="file_sha")
            gh.put_file = AsyncMock(return_value=True)
            gh.create_pr = AsyncMock(return_value=(None, None))
            result = await repo_sync.create_settings_pr(
                _cfg(), runtime_config={"k": "v"},
                dirty_keys=["k"], operator_name="X", operator_id=1,
            )
        assert result.ok is False
        assert result.reason == "pr_failed"


# ---- fetch_main_runtime_config ---------------------------------------------


class TestFetchMainRuntimeConfig:
    @pytest.mark.asyncio
    async def test_404_returns_not_in_repo(self) -> None:
        # Файл не существует в репо → понятный reason для UI «впервые
        # синхронизируем, файла ещё нет».
        with patch.object(
            repo_sync.aiohttp, "ClientSession", _aiohttp_session_patch(),
        ), patch.object(repo_sync, "_GH") as gh_cls:
            gh = gh_cls.return_value
            gh._request = AsyncMock(return_value=(404, None))
            data, reason = await repo_sync.fetch_main_runtime_config(_cfg())
        assert data is None
        assert reason == "not_in_repo"

    @pytest.mark.asyncio
    async def test_happy(self) -> None:
        content = base64.b64encode(
            json.dumps({"k": "v", "u": "Привет"}).encode("utf-8"),
        ).decode("ascii")
        with patch.object(
            repo_sync.aiohttp, "ClientSession", _aiohttp_session_patch(),
        ), patch.object(repo_sync, "_GH") as gh_cls:
            gh = gh_cls.return_value
            gh._request = AsyncMock(return_value=(200, {"content": content}))
            data, reason = await repo_sync.fetch_main_runtime_config(_cfg())
        assert data == {"k": "v", "u": "Привет"}
        assert reason == "ok"

    @pytest.mark.asyncio
    async def test_500_returns_fetch_failed(self) -> None:
        with patch.object(
            repo_sync.aiohttp, "ClientSession", _aiohttp_session_patch(),
        ), patch.object(repo_sync, "_GH") as gh_cls:
            gh = gh_cls.return_value
            gh._request = AsyncMock(return_value=(500, None))
            data, reason = await repo_sync.fetch_main_runtime_config(_cfg())
        assert data is None
        assert reason == "fetch_failed"

    @pytest.mark.asyncio
    async def test_invalid_json_returns_parse_failed(self) -> None:
        # base64-декодируется, но это не валидный JSON.
        bad_content = base64.b64encode(b"{not valid json").decode("ascii")
        with patch.object(
            repo_sync.aiohttp, "ClientSession", _aiohttp_session_patch(),
        ), patch.object(repo_sync, "_GH") as gh_cls:
            gh = gh_cls.return_value
            gh._request = AsyncMock(return_value=(200, {"content": bad_content}))
            data, reason = await repo_sync.fetch_main_runtime_config(_cfg())
        assert data is None
        assert reason == "parse_failed"
