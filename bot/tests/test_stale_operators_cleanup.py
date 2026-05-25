"""Тесты на `cleanup_stale_operators` (SECURITY_REVIEW M2 / CVE-9).

Сценарии:
1. Active оператор, которого нет в MAX-группе → деактивирован.
2. Active оператор, который ЕСТЬ в MAX-группе → не тронут.
3. IT-оператор НЕ деактивируется даже если его нет в группе (защита
   от self-lock-out).
4. Если `current_member_ids` пустой (MAX API упал) → НЕ деактивирует
   никого (safety).
5. Аудит-запись `operator_auto_deactivated_stale` создаётся.

PG-зависимый — пропускается в чисто-unit окружении.
"""
from __future__ import annotations

import pytest

from sqlalchemy import select

from aemr_bot.db.models import AuditLog, OperatorRole
from aemr_bot.services import operators as ops_svc


@pytest.mark.asyncio
class TestCleanupStaleOperators:
    async def _seed(self, session, *records: tuple[int, OperatorRole, str]) -> None:
        for max_user_id, role, name in records:
            await ops_svc.upsert(
                session, max_user_id=max_user_id, full_name=name, role=role
            )
        await session.flush()

    async def test_stale_operator_deactivated(self, session) -> None:
        await self._seed(
            session,
            (1001, OperatorRole.AEMR, "Иванов"),
            (1002, OperatorRole.AEMR, "Петров"),
        )
        # 1002 нет в группе → должен быть деактивирован
        deactivated = await ops_svc.cleanup_stale_operators(
            session, current_member_ids={1001}
        )
        await session.flush()

        assert len(deactivated) == 1
        assert deactivated[0].max_user_id == 1002

        # 1001 остался активным:
        active = await ops_svc.list_active(session)
        active_ids = {op.max_user_id for op in active}
        assert 1001 in active_ids
        assert 1002 not in active_ids

    async def test_it_operator_protected(self, session) -> None:
        """IT-оператор НЕ деактивируется даже если его нет в группе."""
        await self._seed(
            session,
            (2001, OperatorRole.IT, "Сидоров IT"),
            (2002, OperatorRole.AEMR, "Иванов"),
        )
        # Оба ушли из группы
        deactivated = await ops_svc.cleanup_stale_operators(
            session, current_member_ids=set()  # пустой → safety, никого не трогаем
        )
        assert deactivated == []

        # Но и при non-empty (без обоих) — IT всё равно защищён:
        deactivated2 = await ops_svc.cleanup_stale_operators(
            session, current_member_ids={9999}  # никого из наших
        )
        await session.flush()

        # Только AEMR-оператор деактивирован, IT остался:
        ids = {op.max_user_id for op in deactivated2}
        assert ids == {2002}
        active = await ops_svc.list_active(session)
        active_ids = {op.max_user_id for op in active}
        assert 2001 in active_ids  # IT по-прежнему активен

    async def test_empty_member_list_does_nothing(self, session) -> None:
        """Safety: если MAX API дал пустой список — никого не трогаем
        (иначе одна сетевая флуктуация деактивирует всех)."""
        await self._seed(
            session,
            (3001, OperatorRole.AEMR, "Один"),
            (3002, OperatorRole.AEMR, "Два"),
        )
        deactivated = await ops_svc.cleanup_stale_operators(
            session, current_member_ids=set()
        )
        assert deactivated == []

        active = await ops_svc.list_active(session)
        assert len(active) == 2

    async def test_audit_log_written(self, session) -> None:
        """Каждая авто-деактивация пишет запись в audit_log."""
        await self._seed(
            session, (4001, OperatorRole.AEMR, "Сергеев")
        )
        await ops_svc.cleanup_stale_operators(
            session, current_member_ids={9999}
        )
        await session.flush()

        rows = await session.scalars(
            select(AuditLog).where(
                AuditLog.action == "operator_auto_deactivated_stale"
            )
        )
        records = list(rows)
        assert len(records) == 1
        assert records[0].target == "4001"
        assert records[0].details.get("reason") == "left_admin_chat"

    async def test_inactive_operators_skipped(self, session) -> None:
        """Уже деактивированных не трогаем (idempotent)."""
        await self._seed(session, (5001, OperatorRole.AEMR, "Уже ушёл"))
        await ops_svc.deactivate(session, 5001)
        await session.flush()

        deactivated = await ops_svc.cleanup_stale_operators(
            session, current_member_ids={9999}
        )
        assert deactivated == []
