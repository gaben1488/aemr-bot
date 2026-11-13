from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aemr_bot.db.models import DialogState
from aemr_bot.handlers import appeal
from tests._helpers import make_event


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [
    DialogState.AWAITING_LOCALITY, DialogState.AWAITING_ADDRESS,
    DialogState.AWAITING_TOPIC, DialogState.AWAITING_GEO_CONFIRM,
])
@pytest.mark.parametrize("current", [True, False])
async def test_related_draft_early_cancel_is_bound_to_its_progress_screen(monkeypatch, state, current):
    user = SimpleNamespace(dialog_state=state.value, dialog_data={
        "draft_token": "new-related-draft", "progress_message_id": "current-screen",
        "summary_chunks": ["Saved continuation"], "attachments": [{"type": "image"}],
    })
    @asynccontextmanager
    async def scope():
        yield object()
    reset = AsyncMock()
    monkeypatch.setattr(appeal, "session_scope", scope)
    monkeypatch.setattr(appeal.users_service, "find_by_max_id", AsyncMock(return_value=user))
    monkeypatch.setattr(appeal.users_service, "reset_state", reset)
    monkeypatch.setattr(appeal, "get_callback_message_id", lambda e: "current-screen" if current else "previous-screen")
    monkeypatch.setattr(appeal, "_send_to_citizen", AsyncMock())
    await appeal._cb_cancel(make_event(with_callback=True), 42, "cancel")
    assert reset.await_count == int(current)


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True])
async def test_repeated_geo_screen_binds_only_current_dialog(monkeypatch, stale):
    from aemr_bot.handlers import appeal_geo

    @asynccontextmanager
    async def current(_):
        yield object(), SimpleNamespace(dialog_data={"draft_token": "related", "locality": "Елизово"})

    @asynccontextmanager
    async def scope():
        yield object()

    update = AsyncMock(side_effect=appeal_geo.users_service.StaleDialogStateError() if stale else None)
    monkeypatch.setattr(appeal_geo, "current_user", current)
    monkeypatch.setattr(appeal_geo, "session_scope", scope)
    monkeypatch.setattr(appeal_geo.users_service, "update_dialog_data", update)
    event = make_event()
    event.message.answer = AsyncMock(return_value=SimpleNamespace(body=SimpleNamespace(mid="fresh-geo")))
    await appeal_geo.on_awaiting_geo_confirm(event, None, "text", 42)
    assert update.await_args.args[2] == {"progress_message_id": "fresh-geo"}
    assert update.await_args.kwargs["expected_states"] == {DialogState.AWAITING_GEO_CONFIRM.value}
