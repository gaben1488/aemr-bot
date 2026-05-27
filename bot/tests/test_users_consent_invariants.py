"""Регрессионные тесты на критические инварианты согласия в
`services/users` (cluster D из плана MLP 2026-05-27).

Покрываем явно:

1. **SEC #1 invariant** — `set_consent` НЕ сбрасывает `is_blocked`.
   Защита от self-unblock через тап старой кнопки «Согласен» после
   блокировки IT-оператором. Без этого регресс-теста SEC #1 может
   тихо вернуться при будущем рефакторинге.

2. **Re-consent после revoke** — `set_consent` обнуляет
   `consent_revoked_at`, иначе retention-cron через 30 дней
   обезличит жителя несмотря на актуальное свежее согласие.

3. **`revoke_consent` дополнительные эффекты** — отписка от рассылки
   (`subscribed_broadcast=False`, `consent_broadcast_at=NULL`), сброс
   `dialog_state=IDLE`, обнуление `dialog_data` (если житель отзывал
   посреди воронки).

4. **`revoke_consent` сохраняет is_blocked** — житель может передумать
   и дать согласие заново; блокировка отдельный axis.

5. **`revoke_consent` НЕ закрывает open appeals** — оператор должен
   отправить финальный ответ через стандартный путь, который сам
   закроет обращение. Закрытие здесь забрало бы у оператора окно
   для прощального ответа.

Все тесты требуют PG-fixture `session` (см. `tests/conftest.py`):
локально skip без `DATABASE_URL=postgresql+asyncpg://`, в CI работают.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aemr_bot.db.models import AppealStatus, DialogState, User
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import users as users_service


class TestSetConsentInvariants:
    """`set_consent` — главный путь согласия из воронки + re-consent."""

    @pytest.mark.asyncio
    async def test_set_consent_does_not_clear_is_blocked(self, session) -> None:
        """SEC #1: blocked житель НЕ может разблокировать себя нажав
        старую кнопку «Согласен» из истории чата.

        Сценарий атаки: оператор-IT заблокировал жителя через
        `set_blocked(True)`. Житель открывает старое сообщение с
        кнопкой «✅ Согласен» (из past consent flow), тапает. До
        SEC #1 fix — `set_consent` сбрасывал `is_blocked=False`,
        житель сам разблокировался. После fix — `is_blocked`
        остаётся True, разблокировка только через IT с
        ensure_role(IT).
        """
        from sqlalchemy import select

        await users_service.get_or_create(session, max_user_id=1, first_name="X")
        await users_service.set_blocked(session, 1, blocked=True)

        # Подтверждаем pre-state: is_blocked=True.
        user = await session.scalar(select(User).where(User.max_user_id == 1))
        assert user.is_blocked is True

        # SEC #1: тап «Согласен» из старого сообщения НЕ снимает блок.
        await users_service.set_consent(session, 1)
        await session.flush()

        user = await session.scalar(select(User).where(User.max_user_id == 1))
        assert user.is_blocked is True, (
            "SEC #1 регресс: set_consent сбросил is_blocked у "
            "заблокированного жителя. Это позволяет self-unblock через "
            "тап старой кнопки «Согласен» из истории чата."
        )
        # При этом consent_pdn_at всё-таки выставлен (set_consent
        # делает свою основную работу — отказ от выполнения был бы
        # ещё хуже UX'но; SEC #1 fix именно про non-effect на блок).
        assert user.consent_pdn_at is not None

    @pytest.mark.asyncio
    async def test_set_consent_clears_consent_revoked_at(self, session) -> None:
        """Re-consent после revoke: `consent_revoked_at` должен быть
        обнулён, иначе retention-cron через 30 дней с того старого
        отзыва обезличит жителя несмотря на актуальное свежее согласие.
        """
        from sqlalchemy import select, update

        await users_service.get_or_create(session, max_user_id=2, first_name="Y")
        await users_service.revoke_consent(session, 2)

        # Pre-check: revoke поставил consent_revoked_at, обнулил consent_pdn_at.
        user = await session.scalar(select(User).where(User.max_user_id == 2))
        assert user.consent_revoked_at is not None
        assert user.consent_pdn_at is None

        # Re-consent: pdn_at должен появиться, revoked_at — сброситься.
        await users_service.set_consent(session, 2)
        await session.flush()

        user = await session.scalar(select(User).where(User.max_user_id == 2))
        assert user.consent_pdn_at is not None
        assert user.consent_revoked_at is None, (
            "set_consent должен обнулить consent_revoked_at — иначе "
            "retention-cron через 30 дней обезличит жителя несмотря "
            "на свежее согласие."
        )

    @pytest.mark.asyncio
    async def test_set_consent_fresh_timestamp(self, session) -> None:
        """consent_pdn_at должен быть ~now (не далёкое прошлое)."""
        from sqlalchemy import select

        before = datetime.now(timezone.utc) - timedelta(seconds=5)
        await users_service.get_or_create(session, max_user_id=3, first_name="Z")
        await users_service.set_consent(session, 3)
        after = datetime.now(timezone.utc) + timedelta(seconds=5)

        user = await session.scalar(select(User).where(User.max_user_id == 3))
        assert user.consent_pdn_at is not None
        assert before <= user.consent_pdn_at <= after


class TestRevokeConsentInvariants:
    """`revoke_consent` — отзыв согласия + выход из бота без erase."""

    @pytest.mark.asyncio
    async def test_revoke_clears_broadcast_subscription(self, session) -> None:
        """Отзыв согласия → автоматическая отписка от рассылки.
        Иначе житель получал бы рассылки несмотря на отказ от обработки
        ПДн — формальное нарушение 152-ФЗ + противоречие тексту
        «подписка отключится» в UI отзыва."""
        from sqlalchemy import select, update

        user = await users_service.get_or_create(
            session, max_user_id=4, first_name="A"
        )
        now = datetime.now(timezone.utc)
        await session.execute(
            update(User).where(User.id == user.id).values(
                subscribed_broadcast=True,
                consent_broadcast_at=now,
            )
        )
        await session.flush()

        await users_service.revoke_consent(session, 4)
        await session.flush()

        user = await session.scalar(select(User).where(User.max_user_id == 4))
        assert user.subscribed_broadcast is False
        assert user.consent_broadcast_at is None

    @pytest.mark.asyncio
    async def test_revoke_resets_dialog_state(self, session) -> None:
        """Если житель отзывал посреди воронки (например, в
        AWAITING_NAME) — стейт сбрасывается в IDLE + dialog_data
        очищается, чтобы следующий /start стартанул чистую воронку."""
        from sqlalchemy import select

        await users_service.get_or_create(session, max_user_id=5, first_name="B")
        await users_service.set_state(
            session, 5, DialogState.AWAITING_SUMMARY,
            data={"locality": "X", "address": "Y", "topic": "T"},
        )

        await users_service.revoke_consent(session, 5)
        await session.flush()

        user = await session.scalar(select(User).where(User.max_user_id == 5))
        assert user.dialog_state == DialogState.IDLE.value
        assert user.dialog_data == {}

    @pytest.mark.asyncio
    async def test_revoke_keeps_is_blocked_intact(self, session) -> None:
        """Житель отзывает согласие → его статус блокировки не меняется.
        Block — отдельный axis: оператор-IT мог заблокировать ранее,
        revoke не «прощает» эту блокировку."""
        from sqlalchemy import select

        await users_service.get_or_create(session, max_user_id=6, first_name="C")
        await users_service.set_blocked(session, 6, blocked=True)

        await users_service.revoke_consent(session, 6)
        await session.flush()

        user = await session.scalar(select(User).where(User.max_user_id == 6))
        assert user.is_blocked is True

    @pytest.mark.asyncio
    async def test_revoke_does_not_close_open_appeals(self, session) -> None:
        """Открытые обращения остаются в работе — оператор должен
        отправить финальный ответ через стандартный путь, который сам
        закроет обращение. Закрытие здесь забрало бы у оператора
        окно для прощального ответа жителю."""
        user = await users_service.get_or_create(
            session, max_user_id=7, first_name="D"
        )
        appeal = await appeals_service.create_appeal(
            session, user=user, address="ул. A", topic="T",
            summary="S", attachments=[],
        )
        # appeal NEW сразу после create.

        await users_service.revoke_consent(session, 7)
        await session.flush()

        refreshed = await appeals_service.get_by_id(session, appeal.id)
        assert refreshed.status == AppealStatus.NEW.value, (
            "revoke_consent не должен закрывать open appeals — это "
            "задача стандартного пути ответа оператора"
        )

    @pytest.mark.asyncio
    async def test_revoke_missing_user_returns_false(self, session) -> None:
        """Несуществующий житель → rowcount=0 → False, не падаем."""
        assert await users_service.revoke_consent(session, 999999) is False

    @pytest.mark.asyncio
    async def test_revoke_sets_revoked_at_fresh(self, session) -> None:
        """consent_revoked_at должен быть ~now."""
        from sqlalchemy import select

        await users_service.get_or_create(session, max_user_id=8, first_name="E")
        before = datetime.now(timezone.utc) - timedelta(seconds=5)
        await users_service.revoke_consent(session, 8)
        after = datetime.now(timezone.utc) + timedelta(seconds=5)

        user = await session.scalar(select(User).where(User.max_user_id == 8))
        assert user.consent_revoked_at is not None
        assert before <= user.consent_revoked_at <= after
        # И consent_pdn_at должен быть обнулён.
        assert user.consent_pdn_at is None
