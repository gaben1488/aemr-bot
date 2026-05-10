import pytest

from aemr_bot.db.models import AppealStatus, DialogState, OperatorRole
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import card_format
from aemr_bot.services import operators as operators_service
from aemr_bot.services import users as users_service


@pytest.mark.asyncio
async def test_user_lifecycle(session):
    user = await users_service.get_or_create(session, max_user_id=42, first_name="Алексей")
    assert user.id is not None
    assert user.consent_pdn_at is None

    await users_service.set_consent(session, 42)
    assert await users_service.has_consent(session, 42)

    await users_service.set_phone(session, 42, "+79991234567")
    await users_service.set_state(session, 42, DialogState.AWAITING_ADDRESS, data={"address": "г. Елизово, ул. Ленина, 13"})
    await users_service.update_dialog_data(session, 42, {"topic": "Дороги"})

    refreshed = await users_service.get_or_create(session, max_user_id=42)
    assert refreshed.phone == "+79991234567"
    assert refreshed.dialog_data["topic"] == "Дороги"
    assert refreshed.dialog_state == DialogState.AWAITING_ADDRESS.value


@pytest.mark.asyncio
async def test_appeal_creation_and_card(session):
    user = await users_service.get_or_create(session, max_user_id=42, first_name="Алексей")
    await users_service.set_phone(session, 42, "89964240723")
    user = await users_service.get_or_create(session, max_user_id=42)

    appeal = await appeals_service.create_appeal(
        session,
        user=user,
        address="г. Елизово, ул. Ленина, д. 13",
        topic="Другое",
        summary="Проверка работоспособности бота и функционала.",
        attachments=[],
    )
    assert appeal.id is not None
    assert appeal.status == AppealStatus.NEW.value

    admin_card = card_format.admin_card(appeal, user)
    assert f"#{appeal.id}" in admin_card
    assert "Алексей" in admin_card
    assert "г. Елизово" in admin_card
    assert "Другое" in admin_card

    user_card = card_format.user_card(appeal)
    assert f"#{appeal.id}" in user_card
    assert "Новое" in user_card


@pytest.mark.asyncio
async def test_operator_reply_flow(session):
    user = await users_service.get_or_create(session, max_user_id=42, first_name="Мария")
    op = await operators_service.upsert(
        session, max_user_id=999, full_name="Координатор АЕМР", role=OperatorRole.COORDINATOR
    )
    appeal = await appeals_service.create_appeal(
        session,
        user=user,
        address="ул. Ленина, 13",
        topic="Благоустройство",
        summary="Тест",
        attachments=[],
    )
    await appeals_service.set_admin_message_id(session, appeal.id, "mid-abc")

    found = await appeals_service.get_by_admin_message_id(session, "mid-abc")
    assert found is not None and found.id == appeal.id

    full = await appeals_service.get_by_id(session, appeal.id)
    await appeals_service.add_operator_message(
        session,
        appeal=full,
        text="Здравствуйте, информация передана.",
        operator_id=op.id,
        max_message_id="mid-out-1",
    )

    refreshed = await appeals_service.get_by_id(session, appeal.id)
    assert refreshed.status == AppealStatus.ANSWERED.value
    assert refreshed.answered_at is not None
    assert refreshed.assigned_operator_id == op.id


@pytest.mark.asyncio
async def test_reopen_and_close(session):
    user = await users_service.get_or_create(session, max_user_id=42, first_name="Иван")
    appeal = await appeals_service.create_appeal(
        session, user=user, address="ул. X", topic="Дороги", summary="Тест", attachments=[]
    )
    full = await appeals_service.get_by_id(session, appeal.id)
    await appeals_service.add_operator_message(
        session, appeal=full, text="Ответ", operator_id=None, max_message_id=None
    )

    assert await appeals_service.reopen(session, appeal.id) is True
    refreshed = await appeals_service.get_by_id(session, appeal.id)
    assert refreshed.status == AppealStatus.IN_PROGRESS.value
    assert refreshed.answered_at is None

    assert await appeals_service.close(session, appeal.id) is True
    refreshed = await appeals_service.get_by_id(session, appeal.id)
    assert refreshed.status == AppealStatus.CLOSED.value


@pytest.mark.asyncio
async def test_erase_pdn(session):
    """erase_pdn — hard delete + anonymous-user pattern (Вариант 3,
    утверждено 2026-05). Запись физически удаляется; следующий get_or_create
    создаёт нового жителя без имени и без телефона — бот «не узнаёт» его."""
    await users_service.get_or_create(session, max_user_id=42, first_name="Пётр")
    await users_service.set_phone(session, 42, "89991112233")

    assert await users_service.erase_pdn(session, 42) is True
    refreshed = await users_service.get_or_create(session, max_user_id=42)
    assert refreshed.first_name is None
    assert refreshed.phone is None


@pytest.mark.asyncio
async def test_revoke_consent_152fz(session):
    """152-ФЗ: revoke_consent отзывает согласие, обнуляет рассылку и
    воронку, но НЕ удаляет запись (это делает /erase или 30-дневный
    retention). После revoke has_consent → False, повторный revoke
    идемпотентен (вернёт False — нечего обновлять)."""
    await users_service.get_or_create(session, max_user_id=99, first_name="Анна")
    await users_service.set_consent(session, 99)
    await users_service.set_phone(session, 99, "+79991234567")
    assert await users_service.has_consent(session, 99) is True

    # Revoke сработал
    assert await users_service.revoke_consent(session, 99) is True

    # Состояние после revoke
    user = await users_service.get_or_create(session, max_user_id=99)
    assert user.consent_pdn_at is None
    assert user.consent_revoked_at is not None
    assert user.subscribed_broadcast is False
    assert user.dialog_state == DialogState.IDLE.value
    assert user.dialog_data == {}
    assert await users_service.has_consent(session, 99) is False
    # Имя/телефон ОСТАЮТСЯ — будут стёрты только через 30 дней или /erase
    assert user.first_name == "Анна"
    assert user.phone == "+79991234567"


@pytest.mark.asyncio
async def test_anonymous_user_singleton(session):
    """get_anonymous_user_id должен быть идемпотентным: повторные вызовы
    возвращают один и тот же id, advisory lock защищает от race condition.
    Если упадёт — каждый /erase создаст НОВУЮ anonymous-запись и appeals
    разойдутся по разным «anonymous» жителям, ломая статистику."""
    id1 = await users_service.get_anonymous_user_id(session)
    id2 = await users_service.get_anonymous_user_id(session)
    id3 = await users_service.get_anonymous_user_id(session)
    assert id1 == id2 == id3
    # И только одна запись в БД
    from aemr_bot.db.models import ANONYMOUS_MAX_USER_ID, User
    from sqlalchemy import select
    rows = (await session.execute(
        select(User).where(User.max_user_id == ANONYMOUS_MAX_USER_ID)
    )).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_purge_old_appeals_5y_retention(session):
    """152-ФЗ: тексты обращений старше 5 лет должны обнуляться, но
    сама запись и метаданные (дата, статус, тематика) сохраняются для
    статистики. Это требование Минкультуры о номенклатуре дел."""
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import update
    from aemr_bot.db.models import Appeal

    user = await users_service.get_or_create(session, max_user_id=88, first_name="X")
    await users_service.set_phone(session, 88, "+70000000000")
    user = await users_service.get_or_create(session, max_user_id=88)

    # Старое обращение (6 лет назад)
    old = await appeals_service.create_appeal(
        session, user=user, address="ул. Старая, 1", topic="Дороги",
        summary="Очень старая жалоба", attachments=[{"type": "image", "id": "x"}],
    )
    # Свежее обращение
    fresh = await appeals_service.create_appeal(
        session, user=user, address="ул. Свежая, 2", topic="Мусор",
        summary="Свежая жалоба", attachments=[],
    )
    # purge смотрит closed_at + status ∈ (ANSWERED, CLOSED).
    # Закрываем старое обращение и фейкаем closed_at на 6 лет назад.
    from aemr_bot.db.models import AppealStatus
    six_y_ago = datetime.now(timezone.utc) - timedelta(days=365 * 6)
    await session.execute(
        update(Appeal)
        .where(Appeal.id == old.id)
        .values(
            created_at=six_y_ago,
            closed_at=six_y_ago,
            status=AppealStatus.CLOSED.value,
        )
    )
    await session.flush()

    purged_a, purged_m = await appeals_service.purge_old_appeals_content(
        session, years=5
    )
    assert purged_a >= 1

    # SQLAlchemy identity map хранит старые версии объектов после
    # bulk UPDATE — expire_all сбрасывает кэш и заставит переселектить.
    session.expire_all()

    # Старое — текст и attachments обнулены, метаданные на месте
    old_after = await appeals_service.get_by_id(session, old.id)
    assert old_after is not None
    assert old_after.summary is None
    assert old_after.attachments == []
    assert old_after.topic == "Дороги"  # метаданные

    # Свежее не тронуто
    fresh_after = await appeals_service.get_by_id(session, fresh.id)
    assert fresh_after.summary == "Свежая жалоба"
