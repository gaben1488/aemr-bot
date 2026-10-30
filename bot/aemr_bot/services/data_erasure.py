"""Inventory MAX copies, perform one-shot deletion and prevent restore resurrection.

No payloads or raw MAX user identifiers are written to the external journal.
Unknown MAX outcomes require reconciliation, never an automatic mutation retry.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aemr_bot.config import settings
from aemr_bot.db.models import Appeal, ErasureTombstone, Message, ResidentDataCopy, User
from aemr_bot.db.session import session_scope
from aemr_bot.utils.event import extract_message_id

_outgoing_depth: ContextVar[int] = ContextVar("erasure_outgoing_depth", default=0)

_restore_cutoff: ContextVar[datetime | None] = ContextVar("erasure_restore_cutoff", default=None)

class PersonalDataErasedError(RuntimeError):
    """A protected read/send no longer has an eligible resident source."""


@dataclass
class PersonalView:
    session: AsyncSession
    appeal_id: int | None = None
    aggregate: bool = False
    max_user_ids: list[int] = field(default_factory=list)
    active: bool = True

    async def include(self, *, max_user_ids: list[int] | None = None, appeal_id: int | None = None) -> None:
        if not self.active:
            raise PersonalDataErasedError("Personal view is no longer active")
        supplied_ids = list(max_user_ids or [])
        if appeal_id is not None:
            source = await self.session.get(Appeal, appeal_id, populate_existing=True)
            owner = await self.session.get(User, source.user_id) if source is not None else None
            if source is None or source.content_erased_at is not None or owner is None or owner.max_user_id <= 0:
                raise PersonalDataErasedError("Appeal source is no longer available")
            if supplied_ids and set(supplied_ids) != {owner.max_user_id}:
                raise PersonalDataErasedError("Appeal does not belong to the selected resident")
            if self.appeal_id is not None and self.appeal_id != appeal_id:
                raise ValueError("Use a user-scoped or aggregate view for multiple appeals")
            self.appeal_id = appeal_id
            supplied_ids = [owner.max_user_id]
        ids = {uid for uid in supplied_ids if isinstance(uid, int) and uid > 0}
        if not ids:
            return
        users = list((await self.session.scalars(select(User).where(User.max_user_id.in_(ids)))).all())
        if {user.max_user_id for user in users} != ids:
            raise PersonalDataErasedError("Resident source no longer exists")
        if self.appeal_id is not None:
            source = await self.session.get(Appeal, self.appeal_id, populate_existing=True)
            owner = await self.session.get(User, source.user_id) if source is not None else None
            if (source is None or source.content_erased_at is not None or owner is None
                    or owner.max_user_id <= 0 or ids != {owner.max_user_id}):
                raise PersonalDataErasedError("Appeal source no longer belongs to this resident")
        self.max_user_ids = sorted(set(self.max_user_ids) | ids)


_personal_view: ContextVar[PersonalView | None] = ContextVar("erasure_personal_view", default=None)


@asynccontextmanager
async def personal_view(*, appeal_id: int | None = None, max_user_ids: list[int] | None = None,
                        aggregate: bool = False):
    """Hold BEFORE reading PD through rendering/upload/send/edit.

    Aggregate is reserved for actual multi-resident lists whose ids are unknown
    before the query. Exact dynamic lists may call ``await view.include(...)``.
    The outgoing hook consumes this context for both private and admin targets.
    """
    from aemr_bot.services import privacy_epoch
    async with session_scope() as session:
        await privacy_epoch.hold_delivery(session)
        view = PersonalView(session=session, appeal_id=appeal_id, aggregate=aggregate)
        ids = list(max_user_ids or [])
        if appeal_id is not None:
            source = await session.get(Appeal, appeal_id)
            owner = await session.get(User, source.user_id) if source is not None else None
            if (source is None or source.content_erased_at is not None or owner is None
                    or owner.max_user_id <= 0):
                raise PersonalDataErasedError("Appeal source is no longer available")
            if ids and set(ids) != {owner.max_user_id}:
                raise PersonalDataErasedError("Appeal does not belong to the selected resident")
            ids = [owner.max_user_id]
        await view.include(max_user_ids=ids)
        token = _personal_view.set(view)
        try:
            yield view
        finally:
            view.active = False
            _personal_view.reset(token)



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


async def _mirror_matching(session, criterion) -> None:
    await session.flush()
    for copy in (await session.scalars(select(ResidentDataCopy).where(criterion))).all():
        _mirror_copy(copy)


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


async def reserve_copy(session, *, kind: str, appeal_id: int | None = None,
                       max_user_ids: list[int] | None = None, allow_erased_recipient: bool = False) -> int:
    """Commit this reservation before calling MAX; complete even if erasure races."""
    ids = list(max_user_ids or [])
    if appeal_id is not None:
        owner = await session.scalar(select(User.max_user_id).join(Appeal, Appeal.user_id == User.id)
                                     .where(Appeal.id == appeal_id))
        if owner is None or owner <= 0:
            raise RuntimeError("Resident source already erased; outbound copy blocked")
        source = await session.get(Appeal, appeal_id)
        if source is not None and source.content_erased_at is not None:
            raise RuntimeError("Appeal content already cleared; outbound copy blocked")
        retention_digest = hmac.new(_key(), f"appeal:{appeal_id}".encode(), hashlib.sha256).hexdigest()
        if await session.get(ErasureTombstone, retention_digest) is not None:
            raise RuntimeError("Appeal source already cleared by retention")
        if any(uid != owner for uid in ids):
            raise RuntimeError("Outbound recipient differs from authoritative appeal owner")
        ids = [owner]
    subjects = sorted({subject_digest(uid) for uid in ids if uid > 0})
    if not allow_erased_recipient:
        for uid in ids:
            instruction = await session.get(ErasureTombstone, subject_digest(uid))
            if instruction is not None and not await session.scalar(select(User.id).where(User.max_user_id == uid)):
                raise RuntimeError("Resident already erased; only an explicit public confirmation is permitted")
    copy = ResidentDataCopy(kind=kind, appeal_id=appeal_id, subjects=subjects, state="reserved")
    session.add(copy)
    await session.flush()
    _mirror_copy(copy)
    return copy.id


async def finish_copy(copy_id: int, sent=None, *, error: str | None = None) -> None:
    async with session_scope() as session:
        copy = await session.get(ResidentDataCopy, copy_id, with_for_update=True)
        if copy is None:
            return
        mid = extract_message_id(sent)
        copy.message_id = mid
        copy.state = ("pending" if copy.erase_requested else "retained") if mid else "uncertain"
        copy.last_error = error if not mid else None
        _mirror_copy(copy)


async def tracked_send(bot, *, kind: str, appeal_id: int | None = None,
                       max_user_ids: list[int] | None = None, allow_erased_recipient: bool = False,
                       send_args: tuple = (), **kwargs):
    """Inventory before sending; callers retain their existing send error handling."""
    from aemr_bot.services import privacy_epoch
    async with session_scope() as barrier:
        await privacy_epoch.hold_delivery(barrier)
        async with session_scope() as session:
            copy_id = await reserve_copy(session, kind=kind, appeal_id=appeal_id, max_user_ids=max_user_ids,
                                         allow_erased_recipient=allow_erased_recipient)
        token = _outgoing_depth.set(_outgoing_depth.get() + 1)
        try:
            try:
                sent = await bot.send_message(*send_args, **kwargs)
            except BaseException as exc:
                await finish_copy(copy_id, error=type(exc).__name__)
                raise
            await finish_copy(copy_id, sent)
            return sent
        finally:
            _outgoing_depth.reset(token)


async def request_erasure(session, max_user_id: int, user_id: int) -> None:
    """Called under lifecycle/epoch locks before source rows are anonymized.

    The external instruction precedes DB commit deliberately: an authorized
    request survives a crash/rollback and is replayed before restored data opens.
    """
    digest = subject_digest(max_user_id)
    cutoff = _restore_cutoff.get() or datetime.now(UTC)
    _write_tombstone(digest, cutoff)
    identity = await session.get(User, user_id)
    if identity is not None:
        scrub_local_logs(max_user_id, first_name=identity.first_name, phone=identity.phone)
    await session.execute(insert(ErasureTombstone).values(
        subject_digest=digest, erased_before=cutoff, backups_state="pending",
    ).on_conflict_do_update(index_elements=[ErasureTombstone.subject_digest], set_={
        "erased_before": cutoff, "backups_state": "pending", "last_error": None,
    }))
    # Seed all identifiers retained by older releases before redaction removes them.
    appeals = (await session.scalars(select(Appeal).where(Appeal.user_id == user_id))).all()
    mids = {mid for item in appeals for mid in (item.admin_message_id, item.last_admin_card_mid) if mid}
    appeal_ids = [item.id for item in appeals]
    if appeal_ids:
        await request_appeal_cleanup(session, appeal_ids)
        mids.update(mid for mid in (await session.scalars(select(Message.max_message_id)
                    .where(Message.appeal_id.in_(appeal_ids)))).all() if mid)
    existing = set((await session.scalars(select(ResidentDataCopy.message_id)
                                          .where(ResidentDataCopy.message_id.in_(mids)))).all()) if mids else set()
    for mid in mids - existing:
        session.add(ResidentDataCopy(subjects=[digest], kind="legacy_known", message_id=mid,
                                    state="pending", erase_requested=True))
    await session.execute(update(ResidentDataCopy).where(((ResidentDataCopy.subjects.contains([digest])) | (ResidentDataCopy.kind.in_(["bulk_export", "bulk_view"]))),
        ResidentDataCopy.state != "deleted").values(erase_requested=True))
    await session.execute(update(ResidentDataCopy).where(((ResidentDataCopy.subjects.contains([digest])) | (ResidentDataCopy.kind.in_(["bulk_export", "bulk_view"]))),
        ResidentDataCopy.state == "retained").values(state="pending"))
    await _mirror_matching(session, (ResidentDataCopy.subjects.contains([digest])) | (ResidentDataCopy.kind.in_(["bulk_export", "bulk_view"])))


async def recover_deletions(bot, *, limit: int = 100) -> int:
    """Claim before MAX. Crash/timeout never replays a started deletion."""
    async with session_scope() as session:
        # A previous worker may have died after sending; keep that visible.
        # Do not reset 'deleting': concurrent workers may still be processing it.
        ids = list((await session.scalars(select(ResidentDataCopy.id)
            .where(ResidentDataCopy.state == "pending", ResidentDataCopy.erase_requested.is_(True))
            .order_by(ResidentDataCopy.id).limit(limit))).all())
    count = 0
    for copy_id in ids:
        async with session_scope() as session:
            claimed = await session.execute(update(ResidentDataCopy).where(
                ResidentDataCopy.id == copy_id, ResidentDataCopy.state == "pending")
                .values(state="deleting").returning(ResidentDataCopy.message_id))
            mid = claimed.scalar_one_or_none()
            if mid:
                copy = await session.get(ResidentDataCopy, copy_id)
                _mirror_copy(copy)
        if not mid:
            continue
        try:
            result = await bot.delete_message(message_id=mid)
            confirmed = getattr(result, "success", None) is True
            state, error = ("deleted", None) if confirmed else ("uncertain", "unconfirmed_delete")
        except Exception as exc:
            state, error = "uncertain", type(exc).__name__
        async with session_scope() as session:
            await session.execute(update(ResidentDataCopy).where(ResidentDataCopy.id == copy_id)
                .values(state=state, last_error=error))
            copy = await session.get(ResidentDataCopy, copy_id)
            _mirror_copy(copy)
        count += state == "deleted"
    return count


async def replay_restore_tombstones(session) -> int:
    """Must run offline after restore and before polling/exports/backups resume."""
    from aemr_bot.services import users
    directory = _journal_dir()
    if not (directory / "subject.key").exists():
        raise RuntimeError("Restore requires the independent erasure journal and subject.key")
    # The independent inventory includes copies newer than the restored dump.
    # Replay it even when the erased user did not yet exist in that dump.
    for path in directory.glob("*.copy.json"):
        entry = json.loads(path.read_text())
        entry["created_at"] = datetime.fromisoformat(entry["created_at"])
        if entry.get("appeal_id") is not None:
            source = await session.get(Appeal, entry["appeal_id"])
            if source is not None:
                created = source.created_at if source.created_at.tzinfo else source.created_at.replace(tzinfo=UTC)
                if created > entry["created_at"]:
                    # A restored sequence reused the numeric id for a new appeal.
                    entry["appeal_id"] = None
        await session.execute(insert(ResidentDataCopy).values(**entry).on_conflict_do_update(
            index_elements=[ResidentDataCopy.external_key], set_={
                key: value for key, value in entry.items() if key != "external_key"
            }))
    tombstones = {item["subject_digest"]: datetime.fromisoformat(item["erased_before"])
                  for path in directory.glob("*.json") if not path.name.endswith((".retention.json", ".restriction.json", ".copy.json", ".backup.json")) for item in [json.loads(path.read_text())]}
    for digest, cutoff in tombstones.items():
        await session.execute(insert(ErasureTombstone).values(subject_digest=digest,
            erased_before=cutoff, backups_state="pending").on_conflict_do_nothing())
        criterion = ((ResidentDataCopy.subjects.contains([digest])) | (ResidentDataCopy.kind.in_(["bulk_export", "bulk_view"]))) & (ResidentDataCopy.created_at <= cutoff)
        await session.execute(update(ResidentDataCopy).where(criterion, ResidentDataCopy.state != "deleted")
                              .values(erase_requested=True))
        await session.execute(update(ResidentDataCopy).where(criterion, ResidentDataCopy.state == "retained")
                              .values(state="pending"))
        await _mirror_matching(session, criterion)
    for path in directory.glob("*.backup.json"):
        entry = json.loads(path.read_text())
        if entry.get("erased_before"):
            await session.execute(update(ErasureTombstone)
                .where(ErasureTombstone.subject_digest == entry["subject_digest"],
                       ErasureTombstone.erased_before == datetime.fromisoformat(entry["erased_before"]))
                .values(backups_state=entry["state"], last_error=entry.get("error")))
    from sqlalchemy import delete

    from aemr_bot.db.models import OperatorReplyAttempt, OperatorReplyDraft
    for path in directory.glob("*.retention.json"):
        instruction = json.loads(path.read_text())
        appeal_id = instruction["appeal_id"]
        cutoff = datetime.fromisoformat(instruction["erased_before"])
        source = await session.get(Appeal, appeal_id)
        if source is None:
            continue
        created = source.created_at if source.created_at.tzinfo else source.created_at.replace(tzinfo=UTC)
        if created > cutoff:
            continue
        has_payload = bool(source.summary or source.address or source.attachments)
        has_payload = has_payload or bool(await session.scalar(select(Message.id).where(
            Message.appeal_id == appeal_id,
            (Message.text.is_not(None)) | (Message.attachments != []),
        ).limit(1)))
        has_payload = has_payload or bool(await session.scalar(select(OperatorReplyDraft.operator_id)
            .where(OperatorReplyDraft.appeal_id == appeal_id).limit(1)))
        has_payload = has_payload or bool(await session.scalar(select(OperatorReplyAttempt.id)
            .where(OperatorReplyAttempt.appeal_id == appeal_id).limit(1)))
        if not has_payload:
            continue
        await request_appeal_cleanup(session, [appeal_id])
        await session.execute(delete(OperatorReplyAttempt).where(OperatorReplyAttempt.appeal_id == appeal_id))
        await session.execute(delete(OperatorReplyDraft).where(OperatorReplyDraft.appeal_id == appeal_id))
        await session.execute(update(Appeal).where(Appeal.id == appeal_id)
                              .values(summary=None, address=None, attachments=[], related_appeal_id=None))
        await session.execute(update(Message).where(Message.appeal_id == appeal_id)
                              .values(text=None, attachments=[], admin_notice_state=None))
    # Restored PostgreSQL sequences must never recycle an id still referenced
    # by the independent copy/retention journal.
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy import text
        maximum = max([0] + [int(json.loads(path.read_text()).get("appeal_id") or 0)
                             for pattern in ("*.copy.json", "*.retention.json")
                             for path in directory.glob(pattern)])
        if maximum:
            await session.execute(text("""SELECT setval(pg_get_serial_sequence('appeals', 'id'),
                GREATEST((SELECT COALESCE(MAX(id), 0) FROM appeals), :maximum,
                COALESCE(pg_sequence_last_value(pg_get_serial_sequence('appeals', 'id')::regclass), 1)), true)"""),
                {"maximum": maximum})
    residents = (await session.scalars(select(User).where(User.max_user_id > 0))).all()
    restrictions = [json.loads(path.read_text()) for path in directory.glob("*.restriction.json")]
    for user in residents:
        digest = subject_digest(user.max_user_id)
        for entry in restrictions:
            at = datetime.fromisoformat(entry["at"])
            if entry["subject_digest"] != digest:
                continue
            consent = user.consent_pdn_at
            broadcast = user.consent_broadcast_at
            if consent is not None and consent.tzinfo is None:
                consent = consent.replace(tzinfo=UTC)
            if broadcast is not None and broadcast.tzinfo is None:
                broadcast = broadcast.replace(tzinfo=UTC)
            if entry["action"] == "revoke" and (consent is None or consent <= at):
                user.consent_pdn_at = None
                user.consent_revoked_at = at
                user.subscribed_broadcast = False
                user.consent_broadcast_at = None
                user.dialog_state = "idle"
                user.dialog_data = {}
                user.appeal_draft = None
            if entry["action"] == "unsubscribe" and (broadcast is None or broadcast <= at):
                user.subscribed_broadcast = False
                user.consent_broadcast_at = None
    erased = 0
    for user in residents:
        resident_cutoff = tombstones.get(subject_digest(user.max_user_id))
        created = user.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if resident_cutoff is not None and created <= resident_cutoff:
            token = _restore_cutoff.set(resident_cutoff)
            try:
                await users.erase_pdn_detailed(session, user.max_user_id)
            finally:
                _restore_cutoff.reset(token)
            erased += 1
    return erased


async def diagnostics() -> dict[str, int]:
    from sqlalchemy import func
    async with session_scope() as session:
        rows = (await session.execute(select(ResidentDataCopy.state, func.count())
            .where(ResidentDataCopy.erase_requested.is_(True)).group_by(ResidentDataCopy.state))).all()
        result = {state: count for state, count in rows}
        result["backups_unresolved"] = int(await session.scalar(select(func.count())
            .select_from(ErasureTombstone).where(ErasureTombstone.backups_state != "done")) or 0)
        return result


async def reconcile_copy(copy_id: int, *, confirmed_absent: bool = False,
                         recovered_message_id: str | None = None) -> bool:
    """IT supplies a checked message id or confirms absence after inspecting MAX.

    This explicit operation never repeats a mutation with an unknown outcome.
    Recovered ids only queue their first deletion; an uncertain deletion is
    closed by confirmed absence, not reset to pending blindly.
    """
    async with session_scope() as session:
        copy = await session.get(ResidentDataCopy, copy_id, with_for_update=True)
        if copy is None or not copy.erase_requested or copy.state == "deleted":
            return False
        if confirmed_absent:
            copy.state = "deleted"
            copy.last_error = "operator_confirmed_absent"
            _mirror_copy(copy)
            return True
        if recovered_message_id and not copy.message_id and copy.state in {"reserved", "uncertain"}:
            copy.message_id = recovered_message_id
            copy.state = "pending"
            copy.last_error = None
            _mirror_copy(copy)
            return True
        return False


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


async def request_appeal_cleanup(session, appeal_ids: list[int]) -> None:
    """Retention removes only eligible appeals, never other appeals of their owner."""
    from aemr_bot.db.models import OperatorReplyAttempt
    for appeal_id in appeal_ids:
        digest = hmac.new(_key(), f"appeal:{appeal_id}".encode(), hashlib.sha256).hexdigest()
        cutoff = _restore_cutoff.get() or datetime.now(UTC)
        path = _journal_dir() / f"{digest}.retention.json"
        if not path.exists():
            # Individual identifiers describe records, without names or MAX ids.
            _write_instruction(path.name, {"appeal_id": appeal_id, "erased_before": cutoff.isoformat()})
        await session.execute(insert(ErasureTombstone).values(subject_digest=digest,
            erased_before=cutoff, backups_state="pending").on_conflict_do_nothing())
    if not appeal_ids:
        return
    await session.execute(update(Appeal).where(Appeal.id.in_(appeal_ids), Appeal.content_erased_at.is_(None))
                          .values(content_erased_at=_restore_cutoff.get() or datetime.now(UTC)))
    appeals = (await session.scalars(select(Appeal).where(Appeal.id.in_(appeal_ids)))).all()
    mids = {mid for item in appeals for mid in (item.admin_message_id, item.last_admin_card_mid) if mid}
    mids.update(mid for mid in (await session.scalars(select(Message.max_message_id)
                .where(Message.appeal_id.in_(appeal_ids)))).all() if mid)
    attempts = (await session.scalars(select(OperatorReplyAttempt)
                                     .where(OperatorReplyAttempt.appeal_id.in_(appeal_ids)))).all()
    mids.update(item.max_message_id for item in attempts if item.max_message_id)
    for item in attempts:
        mids.update(part["mid"] for part in item.delivery_parts if isinstance(part, dict) and part.get("mid"))
    existing = set((await session.scalars(select(ResidentDataCopy.message_id)
        .where(ResidentDataCopy.message_id.in_(mids)))).all()) if mids else set()
    for mid in mids - existing:
        session.add(ResidentDataCopy(subjects=[], kind="legacy_retention", message_id=mid,
                                    state="pending", erase_requested=True))
    belongs_to_generation = select(Appeal.id).where(
        Appeal.id == ResidentDataCopy.appeal_id,
        Appeal.created_at <= ResidentDataCopy.created_at,
    ).exists()
    owner_ids = list((await session.scalars(select(User.max_user_id).join(Appeal, Appeal.user_id == User.id)
                                           .where(Appeal.id.in_(appeal_ids), User.max_user_id > 0))).all())
    from sqlalchemy import or_
    owner_views = or_(*(ResidentDataCopy.subjects.contains([subject_digest(uid)]) for uid in owner_ids)) if owner_ids else False
    unscoped_views = (ResidentDataCopy.appeal_id.is_(None)) & (ResidentDataCopy.kind.in_(["personal_view", "citizen_screen"])) & owner_views
    criterion = (((ResidentDataCopy.appeal_id.in_(appeal_ids)) & belongs_to_generation)
                 | unscoped_views | (ResidentDataCopy.kind.in_(["bulk_export", "bulk_view"])))
    await session.execute(update(ResidentDataCopy).where(criterion, ResidentDataCopy.state != "deleted")
                          .values(erase_requested=True))
    await session.execute(update(ResidentDataCopy).where(criterion, ResidentDataCopy.state == "retained")
                          .values(state="pending"))
    await _mirror_matching(session, criterion | ResidentDataCopy.message_id.in_(mids))


def journal_restriction(max_user_id: int, action: str) -> None:
    """Keep withdrawals/unsubscribe effective after restoring an older dump."""
    if action not in {"revoke", "unsubscribe"}:
        raise ValueError("Unknown restriction")
    digest = subject_digest(max_user_id)
    cutoff = datetime.now(UTC)
    _write_instruction(f"{digest}.{action}.restriction.json",
                       {"subject_digest": digest, "action": action, "at": cutoff.isoformat()})


async def confirm_backup_cleanup(digest: str, *, expected_erased_before: str | None = None) -> bool:
    """IT confirms all external versions/locks/replicas were checked separately."""
    async with session_scope() as session:
        item = await session.get(ErasureTombstone, digest, with_for_update=True)
        if (item is None or item.backups_state != "manual" or expected_erased_before is None
                or item.erased_before != datetime.fromisoformat(expected_erased_before)):
            return False
        item.backups_state = "done"
        item.last_error = None
        _write_instruction(f"{digest}.backup.json", {
            "subject_digest": digest, "erased_before": item.erased_before.isoformat(), "state": "done", "error": None,
        })
        return True


async def unresolved_copies(*, limit: int = 20) -> list[dict]:
    async with session_scope() as session:
        copies = (await session.scalars(select(ResidentDataCopy).where(
            ResidentDataCopy.erase_requested.is_(True), ResidentDataCopy.state != "deleted")
            .order_by(ResidentDataCopy.id).limit(limit))).all()
        return [{"id": item.id, "kind": item.kind, "state": item.state,
                 "message_id": item.message_id, "last_error": item.last_error} for item in copies]


async def record_known_copy(session, *, message_id: str, kind: str,
                            max_user_ids: list[int] | None = None,
                            appeal_id: int | None = None) -> None:
    """Inventory incoming MAX messages atomically with their deduplicated receipt."""
    ids = list(max_user_ids or [])
    if appeal_id is not None:
        owner = await session.scalar(select(User.max_user_id).join(Appeal, Appeal.user_id == User.id)
                                     .where(Appeal.id == appeal_id))
        if owner is not None and owner > 0:
            ids.append(owner)
    subjects = sorted({subject_digest(uid) for uid in ids if uid > 0})
    await session.execute(insert(ResidentDataCopy).values(kind=kind, appeal_id=appeal_id,
        subjects=subjects, message_id=message_id, state="retained", erase_requested=False)
        .on_conflict_do_nothing(index_elements=[ResidentDataCopy.message_id]))
    await _mirror_matching(session, ResidentDataCopy.message_id == message_id)


def install_outgoing_hook(bot) -> None:
    """Install after the admin notification hook; explicit inventory is never doubled."""
    if getattr(bot, "_erasure_outgoing_installed", False) is True:
        return
    original_send = bot.send_message
    original_edit = getattr(bot, "edit_message", None)

    async def send_message(*args, **kwargs):
        if _outgoing_depth.get():
            return await original_send(*args, **kwargs)
        view = _personal_view.get()
        if view is not None:
            if not view.active:
                raise PersonalDataErasedError("Personal view expired before output")
            if view.aggregate or view.max_user_ids:
                return await tracked_send(SimpleNamespace(send_message=original_send),
                    kind="bulk_view" if view.aggregate else "personal_view",
                    appeal_id=view.appeal_id, max_user_ids=view.max_user_ids,
                    send_args=args, **kwargs)
        # Pinned SDK order is chat_id, user_id. Preserve original positional
        # arguments through generic admin_bus wrappers without signature binding.
        uid = kwargs.get("user_id", args[1] if len(args) > 1 else None)
        chat_id = kwargs.get("chat_id", args[0] if args else None)
        if uid is None and chat_id != settings.admin_group_id:
            from aemr_bot.utils.resident_lifecycle import current_subject
            uid = current_subject.get()
        if not isinstance(uid, int) or uid <= 0:
            return await original_send(*args, **kwargs)
        return await tracked_send(SimpleNamespace(send_message=original_send), kind="citizen_screen",
                                  max_user_ids=[uid], send_args=args, **kwargs)

    async def edit_message(*args, **kwargs):
        from aemr_bot.services import privacy_epoch
        mid = kwargs.get("message_id") or (args[0] if args else None)
        if _outgoing_depth.get() or not mid:
            return await original_edit(*args, **kwargs)
        async with session_scope() as session:
            await privacy_epoch.hold_delivery(session)
            erased = await session.scalar(select(ResidentDataCopy.id).where(
                ResidentDataCopy.message_id == mid, ResidentDataCopy.erase_requested.is_(True)).limit(1))
            if erased is not None:
                raise PersonalDataErasedError("Edit blocked: resident copy is queued for erasure")
            view = _personal_view.get()
            if view is not None:
                if not view.active:
                    raise PersonalDataErasedError("Personal view expired before edit")
                if view.aggregate or view.max_user_ids:
                    # Commit union BEFORE MAX: a lost edit result may have left
                    # either the previous or the proposed personal content.
                    async with session_scope() as inventory:
                        copy = await inventory.scalar(select(ResidentDataCopy).where(
                            ResidentDataCopy.message_id == mid).with_for_update())
                        if copy is not None and copy.erase_requested:
                            raise PersonalDataErasedError("Personal copy is queued for erasure")
                        subjects = {subject_digest(uid) for uid in view.max_user_ids}
                        if copy is None:
                            copy = ResidentDataCopy(kind="bulk_view" if view.aggregate else "personal_view",
                                appeal_id=view.appeal_id, subjects=sorted(subjects), message_id=mid,
                                state="retained", erase_requested=False)
                            inventory.add(copy)
                            await inventory.flush()
                        else:
                            copy.subjects = sorted(set(copy.subjects or []) | subjects)
                            if copy.appeal_id != view.appeal_id:
                                copy.appeal_id = None
                            if view.aggregate or copy.kind == "bulk_view":
                                copy.kind = "bulk_view"
                            elif copy.appeal_id is None:
                                copy.kind = "personal_view"
                        _mirror_copy(copy)
            return await original_edit(*args, **kwargs)

    bot.send_message = send_message
    if original_edit is not None:
        bot.edit_message = edit_message
    bot._erasure_outgoing_installed = True


async def initialize_journal(*, allow_existing_database: bool = False) -> None:
    """Explicit install/upgrade step. Never initialize over existing journal records.

    A new empty DB may initialize automatically. An existing DB requires the
    owner's deliberate install step; a restored DB must supply its original key.
    """
    directory = _journal_dir()
    if (directory / "subject.key").exists():
        _key()
        return
    if list(directory.glob("*.json")):
        raise RuntimeError("Erasure records exist but their original key is missing")
    async with session_scope() as session:
        residents = await session.scalar(select(User.id).where(User.max_user_id > 0).limit(1))
        instructions = await session.scalar(select(ErasureTombstone.subject_digest).limit(1))
    if instructions is not None or (residents is not None and not allow_existing_database):
        raise RuntimeError("Existing DB requires its independent journal or explicit first-install initialization")
    _key()


async def prepare_startup() -> int:
    """Before accepting traffic: validate independent ledger and replay restrictions."""
    await initialize_journal()
    async with session_scope() as session:
        return await replay_restore_tombstones(session)


async def record_incoming_copies(session, messages: list[tuple[str, int]]) -> None:
    """One bounded SQL insert per transport batch; no per-message database lookup."""
    if not messages:
        return
    key = _key()
    digests = {uid: hmac.new(key, str(uid).encode(), hashlib.sha256).hexdigest()
               for _, uid in messages}
    rows = [{"kind": "resident_input", "subjects": [digests[uid]], "message_id": mid,
             "state": "retained", "erase_requested": False} for mid, uid in messages]
    await session.execute(insert(ResidentDataCopy).values(rows)
                          .on_conflict_do_nothing(index_elements=[ResidentDataCopy.message_id]))
    await _mirror_matching(session, ResidentDataCopy.message_id.in_([mid for mid, _ in messages]))


async def unresolved_backups(*, limit: int = 5) -> list[dict]:
    async with session_scope() as session:
        items = (await session.scalars(select(ErasureTombstone)
            .where(ErasureTombstone.backups_state != "done").limit(limit))).all()
        return [{"digest": item.subject_digest, "erased_before": item.erased_before.isoformat(),
                 "state": item.backups_state, "error": item.last_error} for item in items]
