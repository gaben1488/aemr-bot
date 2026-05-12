"""PG-fixture-тесты services/users — недостающие ветви.

Тесты, требующие реального PostgreSQL (JSONB, advisory-lock, rowcount
из asyncpg). Локально все skip без `DATABASE_URL=postgresql+asyncpg://`,
в CI запускаются на postgres-сервисе.

Покрываем:
- update_dialog_data: merge, none-user
- set_first_name
- set_blocked: ставит/снимает, закрывает открытые обращения
- find_by_phone: один матч, ноль, два (возвращает None и логирует)
- erase_pdn_by_phone
- erase_pdn: переподвеска на anonymous + стирание свободного текста/attachments
- list_subscribers / list_consented / list_blocked
- find_pending_pdn_retention: окно, исключения по first_name='Удалено'
- has_open_appeals
- find_stuck_in_summary / find_stuck_in_funnel: фильтры по времени
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aemr_bot.db.models import (
    ANONYMOUS_MAX_USER_ID,
    Appeal,
    AppealStatus,
    DialogState,
    Message,
    User,
)
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import users as users_service


class TestUpdateDialogData:
    @pytest.mark.asyncio
    async def test_merges_keys(self, session) -> None:
        await users_service.get_or_create(session, max_user_id=1, first_name="X")
        await users_service.update_dialog_data(session, 1, {"a": 1, "b": 2})
        await users_service.update_dialog_data(session, 1, {"b": 99, "c": 3})
        u = await users_service.get_or_create(session, max_user_id=1)
        assert u.dialog_data == {"a": 1, "b": 99, "c": 3}

    @pytest.mark.asyncio
    async def test_none_user_returns_empty(self, session) -> None:
        # Жителя нет — функция возвращает {} и не падает.
        result = await users_service.update_dialog_data(session, 99999, {"x": 1})
        assert result == {}


class TestSetFirstName:
    @pytest.mark.asyncio
    async def test_overwrites(self, session) -> None:
        await users_service.get_or_create(session, max_user_id=1, first_name="Старое")
        await users_service.set_first_name(session, 1, "Новое")
        u = await users_service.get_or_create(session, max_user_id=1)
        assert u.first_name == "Новое"


class TestSetBlocked:
    @pytest.mark.asyncio
    async def test_block_closes_open_appeals(self, session) -> None:
        user = await users_service.get_or_create(session, max_user_id=1, first_name="X")
        await appeals_service.create_appeal(
            session, user=user, address="ул. A", topic="T",
            summary="S", attachments=[],
        )
        ok = await users_service.set_blocked(session, 1, blocked=True)
        assert ok is True
        # Проверяем, что обращение закрылось с флагом revoke
        from sqlalchemy import select
        rows = (await session.execute(
            select(Appeal.status, Appeal.closed_due_to_revoke)
            .where(Appeal.user_id == user.id)
        )).all()
        assert rows[0][0] == AppealStatus.CLOSED.value
        assert rows[0][1] is True

    @pytest.mark.asyncio
    async def test_unblock_does_not_touch_appeals(self, session) -> None:
        user = await users_service.get_or_create(session, max_user_id=1, first_name="X")
        appeal = await appeals_service.create_appeal(
            session, user=user, address="ул. A", topic="T",
            summary="S", attachments=[],
        )
        ok = await users_service.set_blocked(session, 1, blocked=False)
        assert ok is True
        refreshed = await appeals_service.get_by_id(session, appeal.id)
        assert refreshed.status == AppealStatus.NEW.value


class TestFindByPhone:
    @pytest.mark.asyncio
    async def test_single_match(self, session) -> None:
        await users_service.get_or_create(session, max_user_id=1, first_name="X")
        await users_service.set_phone(session, 1, "+79991234567")
        found = await users_service.find_by_phone(session, "8-999-123-45-67")
        assert found is not None
        assert found.max_user_id == 1

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self, session) -> None:
        result = await users_service.find_by_phone(session, "+79990000000")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_phone_returns_none(self, session) -> None:
        result = await users_service.find_by_phone(session, "---")
        assert result is None

    @pytest.mark.asyncio
    async def test_multiple_matches_returns_none(self, session) -> None:
        # Один номер — два жителя (муж+жена на одной симке).
        # Безопасный fallback — ничего не возвращаем.
        await users_service.get_or_create(session, max_user_id=1, first_name="A")
        await users_service.set_phone(session, 1, "+79991111111")
        await users_service.get_or_create(session, max_user_id=2, first_name="B")
        await users_service.set_phone(session, 2, "+79991111111")
        result = await users_service.find_by_phone(session, "+79991111111")
        assert result is None


class TestErasePdnByPhone:
    @pytest.mark.asyncio
    async def test_erases_match(self, session) -> None:
        await users_service.get_or_create(session, max_user_id=42, first_name="X")
        await users_service.set_phone(session, 42, "+79992223344")
        result = await users_service.erase_pdn_by_phone(session, "+79992223344")
        assert result == 42

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self, session) -> None:
        result = await users_service.erase_pdn_by_phone(session, "+79990000000")
        assert result is None


class TestErasePdn:
    @pytest.mark.asyncio
    async def test_erases_user_and_redacts_appeal_payloads(self, session) -> None:
        from sqlalchemy import select

        user = await users_service.get_or_create(
            session, max_user_id=777, first_name="Иван"
        )
        await users_service.set_phone(session, 777, "+79992223344")
        appeal = await appeals_service.create_appeal(
            session,
            user=user,
            address="ул. Ленина, 13, кв. 7",
            topic="Дороги",
            summary="Меня зовут Иван, телефон +79992223344, проблема у квартиры 7",
            attachments=[{"type": "photo", "token": "secret-media-token"}],
            locality="Елизово",
        )
        await appeals_service.add_user_message(
            session,
            appeal,
            text="Дополнение: Иван, +79992223344",
            attachments=[{"type": "file", "token": "secret-file-token"}],
        )
        await appeals_service.add_operator_message(
            session,
            appeal,
            text="Ответ операторa с пересказом адреса ул. Ленина, 13",
            operator_id=None,
            max_message_id="op-mid-1",
        )

        ok = await users_service.erase_pdn(session, 777)
        assert ok is True
        await session.flush()

        assert await session.scalar(select(User).where(User.max_user_id == 777)) is None
        anonymous = await session.scalar(
            select(User).where(User.max_user_id == ANONYMOUS_MAX_USER_ID)
        )
        assert anonymous is not None

        stored_appeal = await session.scalar(select(Appeal).where(Appeal.id == appeal.id))
        assert stored_appeal is not None
        assert stored_appeal.user_id == anonymous.id
        assert stored_appeal.address is None
        assert stored_appeal.summary is None
        assert stored_appeal.attachments == []
        assert stored_appeal.topic == "Дороги"
        assert stored_appeal.locality == "Елизово"
        assert stored_appeal.closed_due_to_revoke is True

        messages = (
            await session.scalars(
                select(Message).where(Message.appeal_id == appeal.id).order_by(Message.id)
            )
        ).all()
        assert len(messages) == 2
        assert all(msg.text is None for msg in messages)
        assert all(msg.attachments == [] for msg in messages)

    @pytest.mark.asyncio
    async def test_missing_user_returns_false(self, session) -> None:
        assert await users_service.erase_pdn(session, 404404) is False


class TestListSubscribers:
    @pytest.mark.asyncio
    async def test_returns_subscribed_only(self, session) -> None:
        from sqlalchemy import update

        user1 = await users_service.get_or_create(session, max_user_id=1, first_name="A")
        user2 = await users_service.get_or_create(session, max_user_id=2, first_name="B")
        # subscribed_broadcast и is_blocked правится прямым UPDATE: в
        # services/users нет публичного set_subscribed.
        await session.execute(
            update(User).where(User.id == user1.id).values(subscribed_broadcast=True)
        )
        # user2 заблокирован — из списка исключается, даже если subscribed
        await session.execute(
            update(User).where(User.id == user2.id)
            .values(subscribed_broadcast=True, is_blocked=True)
        )
        await session.flush()
        subs = await users_service.list_subscribers(session)
        assert len(subs) == 1
        assert subs[0].max_user_id == 1


class TestListConsented:
    @pytest.mark.asyncio
    async def test_returns_with_consent_only(self, session) -> None:
        await users_service.get_or_create(session, max_user_id=1, first_name="A")
        await users_service.set_consent(session, 1)
        await users_service.get_or_create(session, max_user_id=2, first_name="B")
        consented = await users_service.list_consented(session)
        ids = [u.max_user_id for u in consented]
        assert 1 in ids
        assert 2 not in ids


class TestListBlocked:
    @pytest.mark.asyncio
    async def test_returns_blocked_only(self, session) -> None:
        await users_service.get_or_create(session, max_user_id=1, first_name="A")
        await users_service.get_or_create(session, max_user_id=2, first_name="B")
        await users_service.set_blocked(session, 2, blocked=True)
        blocked = await users_service.list_blocked(session)
        ids = [u.max_user_id for u in blocked]
        assert 2 in ids
        assert 1 not in ids


class TestFindPendingPdnRetention:
    @pytest.mark.asyncio
    async def test_returns_revoked_older_than_window(self, session) -> None:
        from sqlalchemy import update

        # Свежий отзыв — не попадает.
        await users_service.get_or_create(session, max_user_id=1, first_name="A")
        await users_service.revoke_consent(session, 1)
        # Старый отзыв (40 дней назад) — попадает.
        u = await users_service.get_or_create(session, max_user_id=2, first_name="B")
        await users_service.revoke_consent(session, 2)
        old = datetime.now(timezone.utc) - timedelta(days=40)
        await session.execute(
            update(User).where(User.id == u.id).values(consent_revoked_at=old)
        )
        await session.flush()

        ids = await users_service.find_pending_pdn_retention(
            session, days_after_revoke=30
        )
        assert 2 in ids
        assert 1 not in ids

    @pytest.mark.asyncio
    async def test_already_erased_excluded(self, session) -> None:
        """first_name='Удалено' — признак уже выполненного обезличивания."""
        from sqlalchemy import update

        u = await users_service.get_or_create(session, max_user_id=1, first_name="X")
        await users_service.revoke_consent(session, 1)
        old = datetime.now(timezone.utc) - timedelta(days=40)
        await session.execute(
            update(User).where(User.id == u.id)
            .values(consent_revoked_at=old, first_name="Удалено")
        )
        await session.flush()

        ids = await users_service.find_pending_pdn_retention(
            session, days_after_revoke=30
        )
        assert 1 not in ids


class TestHasOpenAppeals:
    @pytest.mark.asyncio
    async def test_returns_true_with_open_appeal(self, session) -> None:
        user = await users_service.get_or_create(session, max_user_id=1, first_name="X")
        await appeals_service.create_appeal(
            session, user=user, address="ул. A", topic="T",
            summary="S", attachments=[],
        )
        assert await users_service.has_open_appeals(session, user.id) is True

    @pytest.mark.asyncio
    async def test_returns_false_without_open(self, session) -> None:
        user = await users_service.get_or_create(session, max_user_id=1, first_name="X")
        assert await users_service.has_open_appeals(session, user.id) is False

    @pytest.mark.asyncio
    async def test_closed_appeals_dont_count(self, session) -> None:
        user = await users_service.get_or_create(session, max_user_id=1, first_name="X")
        appeal = await appeals_service.create_appeal(
            session, user=user, address="ул. A", topic="T",
            summary="S", attachments=[],
        )
        await appeals_service.close(session, appeal.id)
        assert await users_service.has_open_appeals(session, user.id) is False


class TestFindStuckInSummary:
    @pytest.mark.asyncio
    async def test_returns_stuck_users(self, session) -> None:
        from sqlalchemy import update

        # Свежий — не попадает (idle_seconds=3600, updated_at=сейчас).
        await users_service.get_or_create(session, max_user_id=1, first_name="A")
        await users_service.set_state(session, 1, DialogState.AWAITING_SUMMARY)
        # Старый — попадает (updated_at = 2 часа назад).
        u = await users_service.get_or_create(session, max_user_id=2, first_name="B")
        await users_service.set_state(session, 2, DialogState.AWAITING_SUMMARY)
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        await session.execute(
            update(User).where(User.id == u.id).values(updated_at=old)
        )
        await session.flush()

        ids = await users_service.find_stuck_in_summary(session, idle_seconds=3600)
        assert 2 in ids
        assert 1 not in ids


class TestFindStuckInFunnel:
    @pytest.mark.asyncio
    async def test_returns_pending_states(self, session) -> None:
        from sqlalchemy import update

        # AWAITING_NAME — попадает в funnel-watchdog.
        u = await users_service.get_or_create(session, max_user_id=1, first_name="A")
        await users_service.set_state(session, 1, DialogState.AWAITING_NAME)
        # AWAITING_SUMMARY — НЕ попадает (отдельный watchdog).
        u2 = await users_service.get_or_create(session, max_user_id=2, first_name="B")
        await users_service.set_state(session, 2, DialogState.AWAITING_SUMMARY)
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        await session.execute(
            update(User).where(User.id.in_([u.id, u2.id])).values(updated_at=old)
        )
        await session.flush()

        rows = await users_service.find_stuck_in_funnel(session, idle_seconds=3600)
        ids = [r[0] for r in rows]
        assert 1 in ids
        assert 2 not in ids

    @pytest.mark.asyncio
    async def test_blocked_excluded(self, session) -> None:
        """Заблокированных в watchdog не трогаем — они и так исключены
        из бизнес-логики."""
        from sqlalchemy import update

        u = await users_service.get_or_create(session, max_user_id=1, first_name="A")
        await users_service.set_state(session, 1, DialogState.AWAITING_NAME)
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        await session.execute(
            update(User).where(User.id == u.id)
            .values(updated_at=old, is_blocked=True)
        )
        await session.flush()

        rows = await users_service.find_stuck_in_funnel(session, idle_seconds=3600)
        ids = [r[0] for r in rows]
        assert 1 not in ids
