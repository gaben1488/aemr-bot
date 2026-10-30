"""Independent restore/privacy restrictions and configured local-log redaction.

Message ids are retained only to prevent stale edits after restoring an older
working database. The journal never schedules deletion of external copies.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from aemr_bot.config import settings


def _journal_dir() -> Path:
    path = Path(settings.erasure_state_dir)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path



def _key() -> bytes:
    directory = _journal_dir()
    path = directory / "subject.key"
    if not path.exists():
        temporary = directory / f".key.{os.urandom(8).hex()}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(os.urandom(32))
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        finally:
            temporary.unlink()
    key = path.read_bytes()
    if len(key) != 32:
        raise RuntimeError("Invalid independent erasure journal key")
    return key



def subject_digest(max_user_id: int) -> str:
    return hmac.new(_key(), str(max_user_id).encode(), hashlib.sha256).hexdigest()



def _write_instruction(filename: str, entry: dict) -> None:
    directory = _journal_dir()
    temporary = directory / f".write.{os.urandom(8).hex()}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(entry, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, directory / filename)
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)



def _mirror_copy(copy) -> None:
    _write_instruction(f"{copy.external_key}.copy.json", {
        "external_key": copy.external_key, "subjects": list(copy.subjects or []),
        "appeal_id": copy.appeal_id, "kind": copy.kind, "message_id": copy.message_id,
        "state": copy.state, "erase_requested": bool(copy.erase_requested),
        "last_error": copy.last_error, "created_at": copy.created_at.isoformat(),
    })



def _write_tombstone(digest: str, cutoff: datetime) -> None:
    # Atomic replacement makes concurrent readers see a complete instruction.
    directory = _journal_dir()
    destination = directory / f"{digest}.json"
    previous = json.loads(destination.read_text()) if destination.exists() else None
    if previous is None or datetime.fromisoformat(previous["erased_before"]) < cutoff:
        _write_instruction(f"{digest}.backup.json", {
            "subject_digest": digest, "erased_before": cutoff.isoformat(),
            "state": "pending", "error": None,
        })
    temporary = directory / f".{digest}.{os.urandom(8).hex()}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump({"subject_digest": digest, "erased_before": cutoff.isoformat()}, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, destination)
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)



def scrub_local_logs(max_user_id: int, *, first_name: str | None = None, phone: str | None = None) -> None:
    """Redact known identifier tokens in application log files; preserve other lines.

    Host journald and third-party log stores are outside this process's control.
    No identity values are retained in the cleanup journal.
    """
    import logging
    import re
    if not settings.log_dir:
        return
    identity = re.compile(
        rf"((?:max_user_id|max_id|user_id|resident|uid|пользователя|пользователь)"
        rf"[\s=:]+){max_user_id}(?!\d)", re.IGNORECASE,
    )
    name = re.compile(r"(?<!\w)" + re.escape(first_name) + r"(?!\w)") if first_name else None
    phone_pattern = re.compile(re.escape(phone)) if phone and len(phone) >= 3 else None
    handlers = [handler for handler in logging.getLogger().handlers
                if isinstance(handler, logging.FileHandler)]
    for handler in handlers:
        handler.acquire()
    try:
        for path in Path(settings.log_dir).glob("bot.log*"):
            if path.is_file() and not path.is_symlink():
                # In-place rewrite preserves the active FileHandler's inode.
                with path.open("r+", encoding="utf-8", errors="replace") as output:
                    content = output.read()
                    lines = []
                    for line in content.splitlines(keepends=True):
                        if identity.search(line):
                            line = identity.sub(r"\1[erased]", line)
                            if name is not None:
                                line = name.sub("[erased]", line)
                        if phone_pattern is not None:
                            line = phone_pattern.sub("[erased]", line)
                        lines.append(line)
                    content = "".join(lines)
                    output.seek(0)
                    output.write(content)
                    output.truncate()
                    output.flush()
                    os.fsync(output.fileno())
    finally:
        for handler in reversed(handlers):
            handler.release()



def journal_restriction(max_user_id: int, action: str) -> None:
    """Keep withdrawals/unsubscribe effective after restoring an older dump."""
    if action not in {"revoke", "unsubscribe"}:
        raise ValueError("Unknown restriction")
    digest = subject_digest(max_user_id)
    cutoff = datetime.now(UTC)
    _write_instruction(f"{digest}.{action}.restriction.json",
                       {"subject_digest": digest, "action": action, "at": cutoff.isoformat()})
