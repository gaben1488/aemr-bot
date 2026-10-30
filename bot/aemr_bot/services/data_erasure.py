"""Erase bot-owned data and prevent restored or stale sources emitting personal data.

MAX messages and issued exports remain staff-managed external copies.
The independent journal contains privacy restrictions, never deletion jobs.
"""
from __future__ import annotations

import hashlib
import hmac
from contextvars import ContextVar
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from aemr_bot.config import settings
from aemr_bot.db.models import Appeal, ErasureTombstone, ResidentDataCopy, User
from aemr_bot.db.session import session_scope
from aemr_bot.services.data_erasure_delivery import (
    PersonalDataErasedError,
    PersonalView,
    _outgoing_depth,
    install_outgoing_hook,
    personal_view,
)
from aemr_bot.services.data_erasure_journal import (
    _journal_dir,
    _key,
    _mirror_copy,
    _write_instruction,
    _write_tombstone,
    journal_restriction,
    scrub_local_logs,
    subject_digest,
)
from aemr_bot.services.data_erasure_restore import replay_restore_tombstones
from aemr_bot.utils.event import extract_message_id

__all__ = [
    "PersonalDataErasedError",
    "PersonalView",
    "confirm_backup_cleanup",
    "diagnostics",
    "finish_copy",
    "initialize_journal",
    "install_outgoing_hook",
    "journal_restriction",
    "personal_view",
    "prepare_startup",
    "reconcile_copy",
    "recover_deletions",
    "replay_restore_tombstones",
    "request_appeal_cleanup",
    "request_erasure",
    "reserve_copy",
    "scrub_local_logs",
    "settings",
    "subject_digest",
    "tracked_send",
    "unresolved_backups",
    "unresolved_copies",
]

_restore_cutoff: ContextVar[datetime | None] = ContextVar("erasure_restore_cutoff", default=None)


async def _mirror_matching(session, criterion) -> None:
    await session.flush()
    for copy in (await session.scalars(select(ResidentDataCopy).where(criterion))).all():
        _mirror_copy(copy)


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
        copy.state = ("manual" if copy.erase_requested else "retained") if mid else "uncertain"
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
    appeal_ids = list((await session.scalars(select(Appeal.id).where(Appeal.user_id == user_id))).all())
    if appeal_ids:
        await request_appeal_cleanup(session, appeal_ids)
    await session.execute(update(ResidentDataCopy).where(((ResidentDataCopy.subjects.contains([digest])) | (ResidentDataCopy.kind.in_(["bulk_export", "bulk_view"]))),
        ResidentDataCopy.state != "deleted").values(erase_requested=True))
    await session.execute(update(ResidentDataCopy).where(((ResidentDataCopy.subjects.contains([digest])) | (ResidentDataCopy.kind.in_(["bulk_export", "bulk_view"]))),
        ResidentDataCopy.state.in_(["retained", "pending", "deleting"])).values(state="manual"))
    await _mirror_matching(session, (ResidentDataCopy.subjects.contains([digest])) | (ResidentDataCopy.kind.in_(["bulk_export", "bulk_view"])))


async def recover_deletions(bot=None, *, limit: int = 100) -> int:
    """Retire legacy automatic deletion jobs into manual external-copy status.

    Kept for older maintenance callers. MAX messages and issued exports are
    staff-managed copies; this operation only updates local privacy metadata.
    """
    async with session_scope() as session:
        copies = (await session.scalars(select(ResidentDataCopy).where(
            ResidentDataCopy.erase_requested.is_(True),
            ResidentDataCopy.state.in_(["pending", "deleting"]),
        ).order_by(ResidentDataCopy.id).limit(limit).with_for_update())).all()
        for copy in copies:
            copy.state = "manual"
            copy.last_error = "external_copy_requires_manual_cleanup"
            _mirror_copy(copy)
    return 0




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
    Recovered ids remain manual; only a checked absence marks them deleted.
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
            copy.state = "manual"
            copy.last_error = None
            _mirror_copy(copy)
            return True
        return False


async def request_appeal_cleanup(session, appeal_ids: list[int]) -> None:
    """Retention removes only eligible appeals, never other appeals of their owner."""
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
    existing = set((await session.scalars(select(ResidentDataCopy.message_id)
        .where(ResidentDataCopy.message_id.in_(mids)))).all()) if mids else set()
    for mid in mids - existing:
        session.add(ResidentDataCopy(subjects=[], kind="legacy_retention", message_id=mid,
                                    state="manual", erase_requested=True))
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
    await session.execute(update(ResidentDataCopy).where(criterion, ResidentDataCopy.state.in_(["retained", "pending", "deleting"]))
                          .values(state="manual"))
    await _mirror_matching(session, criterion | ResidentDataCopy.message_id.in_(mids))


async def confirm_backup_cleanup(digest: str, *, expected_erased_before: str | None = None) -> bool:
    """Compatibility entrypoint: complete LOCAL cleanup, never certify external copies."""
    if expected_erased_before is None:
        return False
    expected = datetime.fromisoformat(expected_erased_before)
    async with session_scope() as session:
        item = await session.get(ErasureTombstone, digest)
        if item is None or item.erased_before != expected:
            return False
    from aemr_bot.services.db_backup import cleanup_erased_backups
    await cleanup_erased_backups()
    async with session_scope() as session:
        item = await session.get(ErasureTombstone, digest)
        return bool(item is not None and item.erased_before == expected and item.backups_state == "done")


async def unresolved_copies(*, limit: int = 20) -> list[dict]:
    async with session_scope() as session:
        copies = (await session.scalars(select(ResidentDataCopy).where(
            ResidentDataCopy.erase_requested.is_(True), ResidentDataCopy.state != "deleted")
            .order_by(ResidentDataCopy.id).limit(limit))).all()
        return [{"id": item.id, "kind": item.kind, "state": item.state,
                 "message_id": item.message_id, "last_error": item.last_error} for item in copies]


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


async def unresolved_backups(*, limit: int = 5) -> list[dict]:
    async with session_scope() as session:
        items = (await session.scalars(select(ErasureTombstone)
            .where(ErasureTombstone.backups_state != "done").limit(limit))).all()
        return [{"digest": item.subject_digest, "erased_before": item.erased_before.isoformat(),
                 "state": item.backups_state, "error": item.last_error} for item in items]
