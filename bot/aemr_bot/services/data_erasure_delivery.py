"""Hold privacy barriers from personal reads through MAX sends and edits."""
from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aemr_bot.config import settings
from aemr_bot.db.models import Appeal, ResidentDataCopy, User

_outgoing_depth: ContextVar[int] = ContextVar("erasure_outgoing_depth", default=0)


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
    from aemr_bot.services import data_erasure as erasure
    from aemr_bot.services import privacy_epoch
    async with erasure.session_scope() as session:
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



def install_outgoing_hook(bot) -> None:
    """Install after the admin notification hook; explicit inventory is never doubled."""
    from aemr_bot.services import data_erasure as erasure
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
                return await erasure.tracked_send(SimpleNamespace(send_message=original_send),
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
        return await erasure.tracked_send(SimpleNamespace(send_message=original_send), kind="citizen_screen",
                                  max_user_ids=[uid], send_args=args, **kwargs)

    async def edit_message(*args, **kwargs):
        from aemr_bot.services import privacy_epoch
        mid = kwargs.get("message_id") or (args[0] if args else None)
        if _outgoing_depth.get() or not mid:
            return await original_edit(*args, **kwargs)
        async with erasure.session_scope() as session:
            await privacy_epoch.hold_delivery(session)
            erased = await session.scalar(select(ResidentDataCopy.id).where(
                ResidentDataCopy.message_id == mid, ResidentDataCopy.erase_requested.is_(True)).limit(1))
            if erased is not None:
                raise PersonalDataErasedError("Edit blocked: resident source was erased")
            view = _personal_view.get()
            if view is not None:
                if not view.active:
                    raise PersonalDataErasedError("Personal view expired before edit")
                if view.aggregate or view.max_user_ids:
                    # Commit union BEFORE MAX: a lost edit result may have left
                    # either the previous or the proposed personal content.
                    async with erasure.session_scope() as inventory:
                        copy = await inventory.scalar(select(ResidentDataCopy).where(
                            ResidentDataCopy.message_id == mid).with_for_update())
                        if copy is not None and copy.erase_requested:
                            raise PersonalDataErasedError("Personal copy belongs to an erased source")
                        subjects = {erasure.subject_digest(uid) for uid in view.max_user_ids}
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
                        erasure._mirror_copy(copy)
            return await original_edit(*args, **kwargs)

    bot.send_message = send_message
    if original_edit is not None:
        bot.edit_message = edit_message
    bot._erasure_outgoing_installed = True
