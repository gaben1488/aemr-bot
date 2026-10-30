"""IT diagnostics distinguish manual copies from automated local cleanup."""

from unittest.mock import AsyncMock

import pytest

from aemr_bot.handlers import admin_data_erasure as handler
from tests._helpers import make_event


@pytest.mark.asyncio
async def test_erasure_status_does_not_offer_automatic_max_or_s3_cleanup(monkeypatch):
    monkeypatch.setattr(handler, "ensure_role", AsyncMock(return_value=True))
    monkeypatch.setattr(handler.data_erasure, "diagnostics", AsyncMock(return_value={
        "manual": 3, "pending": 2, "uncertain": 1, "backups_unresolved": 4,
    }))
    monkeypatch.setattr(handler.data_erasure, "unresolved_copies", AsyncMock(return_value=[]))
    monkeypatch.setattr(handler.data_erasure, "unresolved_backups", AsyncMock(return_value=[]))
    output = AsyncMock()
    monkeypatch.setattr(handler, "op_screen", output)
    await handler.show_status(make_event())
    text = output.await_args.kwargs["text"]
    assert "автоматически не удаляются" in text
    assert "Ручная проверка учтённых копий: 6" in text
    assert "Локальные резервные копии требуют проверки: 4" in text
    assert "В очереди:" not in text
    assert "S3" not in text
