"""Explicit SQL barrier boundary for unit cases; real races use PostgreSQL."""
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests._helpers import fake_session_scope


@pytest.fixture
def personal_view_boundary(monkeypatch):
    @asynccontextmanager
    async def view(**source):
        yield SimpleNamespace(include=AsyncMock())

    monkeypatch.setattr("aemr_bot.services.data_erasure.personal_view", view)


@pytest.fixture
def privacy_prompt_resident(monkeypatch, personal_view_boundary):
    resident = SimpleNamespace(id=1, created_at=datetime(2026, 1, 1, tzinfo=UTC),
                               consent_pdn_at=datetime(2026, 2, 1, tzinfo=UTC),
                               consent_pdn_text_sha256="edition", dialog_data={})
    monkeypatch.setattr("aemr_bot.handlers.menu.session_scope", fake_session_scope)
    monkeypatch.setattr("aemr_bot.services.users.find_by_max_id", AsyncMock(return_value=resident))
    return resident
