from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from aemr_bot.services import cron


@pytest.mark.asyncio
@pytest.mark.parametrize('cleanup_failure', [False, True])
async def test_warning_runs_without_overdue_rows_or_when_cleanup_unavailable(monkeypatch, cleanup_failure):
    @asynccontextmanager
    async def scope():
        yield object()
    monkeypatch.setattr(cron, 'session_scope', scope)
    monkeypatch.setattr(cron.users_service, 'find_pending_pdn_retention',
                        AsyncMock(return_value=[], side_effect=RuntimeError('DB failure') if cleanup_failure else None))
    warning = AsyncMock()
    monkeypatch.setattr(cron, '_warn_about_approaching_pdn_deadline', warning)
    send = AsyncMock()
    await cron._job_pdn_retention_check(send)
    warning.assert_awaited_once_with(send)
