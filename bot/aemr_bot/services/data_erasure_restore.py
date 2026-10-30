"""Replay independent erasure and consent restrictions before restored DB traffic."""
from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from aemr_bot.db.models import Appeal, ErasureTombstone, Message, ResidentDataCopy, User
from aemr_bot.services.data_erasure_journal import _journal_dir, subject_digest


async def replay_restore_tombstones(session) -> int:
    """Must run offline after restore and before polling/exports/backups resume."""
    from aemr_bot.services import data_erasure as erasure
    from aemr_bot.services import users
    directory = _journal_dir()
    if not (directory / "subject.key").exists():
        raise RuntimeError("Restore requires the independent erasure journal and subject.key")
    # The independent inventory includes copies newer than the restored dump.
    # Replay it even when the erased user did not yet exist in that dump.
    for path in directory.glob("*.copy.json"):
        entry = json.loads(path.read_text())
        entry["created_at"] = datetime.fromisoformat(entry["created_at"])
        if entry.get("erase_requested") and entry["state"] in {"pending", "deleting"}:
            entry["state"] = "manual"
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
    await session.execute(update(ResidentDataCopy).where(
        ResidentDataCopy.erase_requested.is_(True), ResidentDataCopy.state.in_(["pending", "deleting"])
    ).values(state="manual"))
    for digest, cutoff in tombstones.items():
        await session.execute(insert(ErasureTombstone).values(subject_digest=digest,
            erased_before=cutoff, backups_state="pending").on_conflict_do_nothing())
        criterion = ((ResidentDataCopy.subjects.contains([digest])) | (ResidentDataCopy.kind.in_(["bulk_export", "bulk_view"]))) & (ResidentDataCopy.created_at <= cutoff)
        await session.execute(update(ResidentDataCopy).where(criterion, ResidentDataCopy.state != "deleted")
                              .values(erase_requested=True))
        await session.execute(update(ResidentDataCopy).where(criterion, ResidentDataCopy.state.in_(["retained", "pending", "deleting"]))
                              .values(state="manual"))
        await erasure._mirror_matching(session, criterion)
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
        await erasure.request_appeal_cleanup(session, [appeal_id])
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
            token = erasure._restore_cutoff.set(resident_cutoff)
            try:
                await users.erase_pdn_detailed(session, user.max_user_id)
            finally:
                erasure._restore_cutoff.reset(token)
            erased += 1
    return erased
