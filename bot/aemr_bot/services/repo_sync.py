"""Синхронизация настроек бота с GitHub-репозиторием.

Создаёт Pull Request с актуальным `seed/runtime_config.json`, собранным
из БД. PR — отдельная ветка, в main не идёт напрямую. После мержа
вручную через GitHub UI scripts/auto-deploy.sh подтянет изменения на
VPS.

Feature-flag: модуль работает только если задан GITHUB_PAT в окружении
(или в settings.commit_author_*). Если PAT отсутствует — функции
возвращают `SyncResult(ok=False, reason='no_token')`, не падают. Это
позволяет включать репо-синхронизацию одной правкой `.env` без
редеплоя бота.

Архитектурные решения:
- Используем только REST API v3 — без зависимости от PyGithub. Меньше
  библиотек = меньше CVE-поверхности и проще аудит.
- Аутентификация: fine-grained Personal Access Token с правами
  Contents:RW + PullRequests:RW на конкретный репо.
- Ветвление: `bot-config-sync-YYYYMMDD-HHMMSS` от main. Время в UTC
  для предсказуемости имён.
- Один PR на изменение — не накапливаем правки в одной ветке, чтобы
  каждое решение IT-админа было отдельным merge-юнитом для аудита.
- Никаких force-push, никаких modification протобразов кроме целевого
  файла seed/runtime_config.json.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30)
TARGET_PATH = "seed/runtime_config.json"


@dataclass(frozen=True)
class SyncConfig:
    pat: str
    repo: str            # вид "owner/repo", напр. "Gaben1488/aemr-bot"
    base_branch: str     # обычно "main"
    author_name: str
    author_email: str


@dataclass(frozen=True)
class SyncResult:
    ok: bool
    pr_url: str | None = None
    pr_number: int | None = None
    branch: str | None = None
    reason: str = ""     # для ok=False — машинный код причины
    message: str = ""    # для UI — человекочитаемое описание


def load_config_from_env_and_settings(
    pat_env_var: str = "GITHUB_PAT",
    repo_env_var: str = "GITHUB_REPO",
    base_env_var: str = "GITHUB_PR_BASE_BRANCH",
    *,
    author_name: str | None = None,
    author_email: str | None = None,
) -> SyncConfig | None:
    """Собирает SyncConfig из окружения + переданных автора/почты.
    Возвращает None, если хоть один обязательный параметр пуст.

    repo по умолчанию "Gaben1488/aemr-bot" — соответствует фактическому
    адресу проекта. Перекрывается переменной GITHUB_REPO."""
    pat = os.environ.get(pat_env_var, "").strip()
    repo = os.environ.get(repo_env_var, "Gaben1488/aemr-bot").strip()
    base = os.environ.get(base_env_var, "main").strip()
    if not pat or not repo or not base:
        return None
    if not author_name or not author_email:
        return None
    return SyncConfig(
        pat=pat,
        repo=repo,
        base_branch=base,
        author_name=author_name.strip(),
        author_email=author_email.strip(),
    )


def serialize_runtime_config(data: dict[str, Any]) -> str:
    """JSON-сериализация с детерминированным порядком ключей и
    переводом строки в конце — чтобы diff в git был минимальным."""
    return json.dumps(
        data,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _make_branch_name() -> str:
    return "bot-config-sync-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _build_commit_message(dirty_keys: list[str]) -> str:
    """Формат: header (под 50 симв) + пустая строка + bullet list ключей."""
    n = len(dirty_keys)
    if n == 0:
        header = "config: sync settings from bot menu"
    elif n == 1:
        header = f"config: update {dirty_keys[0]}"
    else:
        header = f"config: sync {n} settings from bot menu"
    body_lines = [""] + [f"- {k}" for k in dirty_keys]
    return header + "\n" + "\n".join(body_lines)


def _sanitize_for_pr_body(value: str, max_len: int = 120) -> str:
    """Очистить строку перед вставкой в PR body / commit message.

    H1 (SECURITY_REVIEW_2026-05-26 CVSS 6.1): `operator_name` приходит
    из `op_record.full_name`, который IT-админ задаёт через
    `/add_operators`. Если злонамеренный (или скомпрометированный) IT
    впишет в full_name `\\n## Maintainer note\\nAuto-approve: YES`,
    эта секция всплывёт в PR body как валидный markdown — обманет
    как auto-merge бота, так и человека-reviewer'а.

    Защита: схлопнуть newline/CR в пробелы; обрезать длину до
    разумного предела; экранировать backtick (чтобы не сломать
    inline-code блок). После этого имя остаётся читаемым в PR, но
    инъекция структуры markdown невозможна.
    """
    # Любые переводы строк → пробел (одна строка, не break-out из контекста).
    cleaned = value.replace("\r", " ").replace("\n", " ")
    # Backtick экранируем, чтобы не вылезти из inline-code (на будущее,
    # сейчас имя не в code-блоке, но защита on-by-default).
    cleaned = cleaned.replace("`", "ˋ")
    # Множественные пробелы — сжимаем (косметика, не безопасность).
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1] + "…"
    return cleaned


def _build_pr_body(
    dirty_keys: list[str], operator_name: str, operator_id: int
) -> str:
    safe_name = _sanitize_for_pr_body(operator_name)
    lines = [
        "Автоматический PR из меню «⚙️ Настройки бота».",
        "",
        f"**Инициатор:** {safe_name} (max_user_id={operator_id})",
        f"**Изменено ключей:** {len(dirty_keys)}",
        "",
        "**Затронутые ключи:**",
    ]
    lines.extend(f"- `{k}`" for k in dirty_keys)
    lines.extend(
        [
            "",
            "После мержа scripts/auto-deploy.sh подхватит изменения на VPS в",
            "течение 10 минут и пересоберёт контейнер бота.",
            "",
            "_Этот PR создан ботом aemr-bot. Авторизация через GITHUB_PAT._",
        ]
    )
    return "\n".join(lines)


class _GH:
    """Тонкий клиент GitHub REST с подстановкой заголовков и базы."""

    def __init__(self, cfg: SyncConfig) -> None:
        self.cfg = cfg
        self._headers = {
            "Authorization": f"Bearer {cfg.pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "aemr-bot-repo-sync",
        }

    async def _request(
        self,
        session: aiohttp.ClientSession,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
    ) -> tuple[int, dict | list | None]:
        url = f"{GITHUB_API}{path}"
        async with session.request(
            method, url, headers=self._headers, json=json_body
        ) as resp:
            status = resp.status
            try:
                data = await resp.json()
            except aiohttp.ContentTypeError:
                data = None
            return status, data

    async def get_ref_sha(
        self, session: aiohttp.ClientSession, branch: str
    ) -> str | None:
        status, data = await self._request(
            session,
            "GET",
            f"/repos/{self.cfg.repo}/git/ref/heads/{branch}",
        )
        if status != 200 or not isinstance(data, dict):
            return None
        return data.get("object", {}).get("sha")

    async def create_branch(
        self, session: aiohttp.ClientSession, branch: str, from_sha: str
    ) -> bool:
        status, _ = await self._request(
            session,
            "POST",
            f"/repos/{self.cfg.repo}/git/refs",
            json_body={"ref": f"refs/heads/{branch}", "sha": from_sha},
        )
        return status == 201

    async def get_file_sha(
        self, session: aiohttp.ClientSession, path: str, branch: str
    ) -> str | None:
        status, data = await self._request(
            session,
            "GET",
            f"/repos/{self.cfg.repo}/contents/{path}?ref={branch}",
        )
        if status != 200 or not isinstance(data, dict):
            return None
        return data.get("sha")

    async def put_file(
        self,
        session: aiohttp.ClientSession,
        *,
        path: str,
        branch: str,
        content_str: str,
        message: str,
        sha: str | None,
    ) -> bool:
        body = {
            "message": message,
            "content": base64.b64encode(content_str.encode("utf-8")).decode("ascii"),
            "branch": branch,
            "committer": {
                "name": self.cfg.author_name,
                "email": self.cfg.author_email,
            },
            "author": {
                "name": self.cfg.author_name,
                "email": self.cfg.author_email,
            },
        }
        if sha:
            body["sha"] = sha
        status, _ = await self._request(
            session,
            "PUT",
            f"/repos/{self.cfg.repo}/contents/{path}",
            json_body=body,
        )
        return status in (200, 201)

    async def create_pr(
        self,
        session: aiohttp.ClientSession,
        *,
        title: str,
        head: str,
        base: str,
        body: str,
    ) -> tuple[int | None, str | None]:
        status, data = await self._request(
            session,
            "POST",
            f"/repos/{self.cfg.repo}/pulls",
            json_body={
                "title": title,
                "head": head,
                "base": base,
                "body": body,
                "maintainer_can_modify": True,
            },
        )
        if status != 201 or not isinstance(data, dict):
            return None, None
        return data.get("number"), data.get("html_url")


async def create_settings_pr(
    cfg: SyncConfig,
    *,
    runtime_config: dict[str, Any],
    dirty_keys: list[str],
    operator_name: str,
    operator_id: int,
) -> SyncResult:
    """Полный сценарий: ветка → файл → PR. Все шаги best-effort —
    при любой ошибке возвращаем понятный reason для UI.

    Контракт:
    - В main НЕ пишем напрямую. Всегда отдельная ветка + PR.
    - При исчерпании квоты или сетевой ошибке возвращаем reason
      без падения, чтобы IT-админ увидел понятное сообщение в чате.
    """
    if not dirty_keys:
        return SyncResult(
            ok=False, reason="no_changes",
            message="Нет несинхронизированных изменений.",
        )

    branch = _make_branch_name()
    commit_message = _build_commit_message(dirty_keys)
    pr_title = commit_message.split("\n", 1)[0]
    pr_body = _build_pr_body(dirty_keys, operator_name, operator_id)
    content_str = serialize_runtime_config(runtime_config)
    gh = _GH(cfg)

    async with aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT) as session:
        # 1. base ref
        base_sha = await gh.get_ref_sha(session, cfg.base_branch)
        if base_sha is None:
            return SyncResult(
                ok=False, reason="no_base_branch",
                message=(
                    f"Не нашёл ветку {cfg.base_branch} в {cfg.repo}. "
                    f"Проверьте GITHUB_REPO и GITHUB_PR_BASE_BRANCH."
                ),
            )
        # 2. новая ветка
        created = await gh.create_branch(session, branch, base_sha)
        if not created:
            return SyncResult(
                ok=False, reason="branch_failed",
                message="Не удалось создать ветку. PAT без прав Contents:RW?",
            )
        # 3. файл — определяем, существовал ли уже
        existing_sha = await gh.get_file_sha(session, TARGET_PATH, cfg.base_branch)
        wrote = await gh.put_file(
            session,
            path=TARGET_PATH,
            branch=branch,
            content_str=content_str,
            message=commit_message,
            sha=existing_sha,
        )
        if not wrote:
            return SyncResult(
                ok=False, reason="write_failed",
                message="Файл не записался. Проверьте права PAT.",
            )
        # 4. PR
        pr_number, pr_url = await gh.create_pr(
            session,
            title=pr_title,
            head=branch,
            base=cfg.base_branch,
            body=pr_body,
        )
        if pr_number is None or pr_url is None:
            return SyncResult(
                ok=False, reason="pr_failed",
                message="PR не создался. PAT без прав Pull requests:RW?",
            )
        log.info(
            "settings PR created: branch=%s url=%s keys=%d",
            branch, pr_url, len(dirty_keys),
        )
        return SyncResult(
            ok=True,
            pr_url=pr_url,
            pr_number=pr_number,
            branch=branch,
            message=f"PR #{pr_number} создан",
        )


async def fetch_main_runtime_config(
    cfg: SyncConfig,
) -> tuple[dict[str, Any] | None, str]:
    """Скачать seed/runtime_config.json из base-ветки и распарсить.
    Возвращает (data, reason). data=None если файла нет или ошибка."""
    async with aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT) as session:
        gh = _GH(cfg)
        status, data = await gh._request(
            session,
            "GET",
            f"/repos/{cfg.repo}/contents/{TARGET_PATH}?ref={cfg.base_branch}",
        )
        if status == 404:
            return None, "not_in_repo"
        if status != 200 or not isinstance(data, dict):
            return None, "fetch_failed"
        encoded = data.get("content") or ""
        try:
            raw = base64.b64decode(encoded).decode("utf-8")
            return json.loads(raw), "ok"
        except Exception:
            return None, "parse_failed"
