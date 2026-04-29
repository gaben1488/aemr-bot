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
    user = await users_service.get_or_create(session, max_user_id=42, first_name="Пётр")
    await users_service.set_phone(session, 42, "89991112233")

    assert await users_service.erase_pdn(session, 42) is True
    refreshed = await users_service.get_or_create(session, max_user_id=42)
    assert refreshed.first_name == "Удалено"
    assert refreshed.phone is None
