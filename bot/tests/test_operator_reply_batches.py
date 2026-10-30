from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from aemr_bot.handlers.operator_reply import _send_reply_to_citizen


@pytest.fixture(autouse=True)
def isolate_copy_inventory(monkeypatch):
    # These unit tests exercise transport ordering. The durable inventory is
    # exercised by PostgreSQL handler/service tests with real resident rows.
    from aemr_bot.services import data_erasure

    async def tracked_send(bot, *, kind, appeal_id=None, max_user_ids=None, **kwargs):
        return await bot.send_message(**kwargs)
    monkeypatch.setattr(data_erasure, "tracked_send", tracked_send)


def event():
    return SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()),
                           message=SimpleNamespace(body=SimpleNamespace(attachments=[])),
                           chat_id=123)


@pytest.mark.asyncio
async def test_two_documents_are_confirmed_separately_in_order():
    e = event()
    e.bot.send_message.side_effect = [SimpleNamespace(body=SimpleNamespace(mid="a")),
                                      SimpleNamespace(body=SimpleNamespace(mid="b"))]
    files = [{"type": "file", "payload": {"token": "one"}},
             {"type": "file", "payload": {"token": "two"}}]
    before = AsyncMock(return_value=True)
    part = AsyncMock()
    with patch("aemr_bot.services.card_format.citizen_reply", return_value="Ответ"):
        ok, mid, stored = await _send_reply_to_citizen(
            e, fresh_appeal=SimpleNamespace(user=SimpleNamespace(max_user_id=1)),
            appeal_id=2, text="Ответ", before_send=before,
            stored_attachments=files, on_part=part,
        )
    assert ok and mid == "a" and stored == files
    assert e.bot.send_message.await_count == 2
    assert [call.args for call in part.await_args_list] == [
        (0, "sending", None), (0, "confirmed", "a"),
        (1, "sending", None), (1, "confirmed", "b"),
    ]


@pytest.mark.asyncio
async def test_second_part_timeout_stops_without_retry_or_success():
    e = event()
    e.bot.send_message.side_effect = [SimpleNamespace(body=SimpleNamespace(mid="a")),
                                      TimeoutError(), SimpleNamespace()]
    files = [{"type": "file", "payload": {"token": name}} for name in ("one", "two", "three")]
    part = AsyncMock()
    with patch("aemr_bot.services.card_format.citizen_reply", return_value="Ответ"), \
         patch("aemr_bot.handlers.operator_reply._safe_admin_notice", AsyncMock()) as notice:
        ok, _, _ = await _send_reply_to_citizen(
            e, fresh_appeal=SimpleNamespace(user=SimpleNamespace(max_user_id=1)),
            appeal_id=2, text="Ответ", stored_attachments=files, on_part=part,
        )
    assert not ok
    assert e.bot.send_message.await_count == 2
    assert part.await_args_list[-1].args == (1, "sending", None)
    assert "неизвестен" in notice.await_args.args[1]
