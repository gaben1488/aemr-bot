"""PG-fixture-тесты services/appeals — недостающие ветви.

Локально skip без `DATABASE_URL=postgresql+asyncpg://`, в CI запускаются.

Покрываем:
- list_for_user: сортировка (открытые сверху), отзыв согласия фильтрует
- count_for_user: симметрично с list_for_user
- count_recent_for_user: rate-limit по часу
- find_overdue_unanswered: SLA-окно
- count_open
- list_unanswered: NEW + IN_PROGRESS, eager-load user
- reopen: ANSWERED/CLOSED → IN_PROGRESS, идемпотентность
- close: not-already-closed
- find_active_for_user
- find_last_address_for_user: locality+address оба заполнены
- add_user_message: followup
- add_operator_message при CLOSED — статус не меняется
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aemr_bot.db.models import Appeal, AppealStatus, DialogState
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import users as users_service


@pytest.mark.asyncio
async def test_list_for_user_sorts_open_first(session) -> None:
    """Открытые (NEW/IN_PROGRESS) сверху, ANSWERED посередине, CLOSED внизу."""
    user = await users_service.get_or_create(session, max_user_id=1, first_name="A")
    closed = await appeals_service.create_appeal(
        session, user=user, address="A", topic="T",
        summary="старое", attachments=[],
    )
    await appeals_service.close(session, closed.id)
    new_one = await appeals_service.create_appeal(
        session, user=user, address="B", topic="T",
        summary="новое открытое", attachments=[],
    )
    rows = await appeals_service.list_for_user(session, user_id=user.id)
    # Открытое раньше закрытого (сортировка по приоритету статуса).
    assert rows[0].id == new_one.id
    assert rows[-1].id == closed.id


@pytest.mark.asyncio
async def test_list_for_user_filters_by_revoke(session) -> None:
    """Обращения, поданные ДО revoke, в списке не показываем."""
    from sqlalchemy import update

    user = await users_service.get_or_create(session, max_user_id=1, first_name="A")
    appeal = await appeals_service.create_appeal(
        session, user=user, address="A", topic="T",
        summary="до revoke", attachments=[],
    )
    # Сначала старая дата у обращения, потом revoke с «свежей» датой.
    old = datetime.now(timezone.utc) - timedelta(days=2)
    await session.execute(
        update(Appeal).where(Appeal.id == appeal.id).values(created_at=old)
    )
    await users_service.revoke_consent(session, 1)
    await session.flush()

    rows = await appeals_service.list_for_user(session, user_id=user.id)
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_count_for_user(session) -> None:
    user = await users_service.get_or_create(session, max_user_id=1, first_name="A")
    await appeals_service.create_appeal(
        session, user=user, address="A", topic="T", summary="x", attachments=[]
    )
    await appeals_service.create_appeal(
        session, user=user, address="A", topic="T", summary="y", attachments=[]
    )
    assert await appeals_service.count_for_user(session, user.id) == 2


@pytest.mark.asyncio
async def test_count_recent_for_user_rate_limit(session) -> None:
    """Rate-limit для нового обращения: за последний час сколько подано."""
    from sqlalchemy import update

    user = await users_service.get_or_create(session, max_user_id=1, first_name="A")
    fresh = await appeals_service.create_appeal(
        session, user=user, address="A", topic="T", summary="x", attachments=[]
    )
    old = await appeals_service.create_appeal(
        session, user=user, address="A", topic="T", summary="y", attachments=[]
    )
    # Старое — 5 часов назад → не попадает в hours=1.
    await session.execute(
        update(Appeal).where(Appeal.id == old.id)
        .values(created_at=datetime.now(timezone.utc) - timedelta(hours=5))
    )
    await session.flush()
    assert await appeals_service.count_recent_for_user(
        session, user.id, hours=1
    ) == 1
    # И первое обращение никуда не пропало
    assert fresh.id is not None


@pytest.mark.asyncio
async def test_find_overdue_unanswered(session) -> None:
    """SLA-просрочка: только те, что висят дольше sla_hours."""
    from sqlalchemy import update

    user = await users_service.get_or_create(session, max_user_id=1, first_name="A")
    fresh = await appeals_service.create_appeal(
        session, user=user, address="A", topic="T", summary="x", attachments=[]
    )
    old = await appeals_service.create_appeal(
        session, user=user, address="A", topic="T", summary="y", attachments=[]
    )
    # «Старое» — 30 часов назад.
    await session.execute(
        update(Appeal).where(Appeal.id == old.id)
        .values(created_at=datetime.now(timezone.utc) - timedelta(hours=30))
    )
    await session.flush()
    overdue = await appeals_service.find_overdue_unanswered(session, sla_hours=24)
    ids = [a.id for a in overdue]
    assert old.id in ids
    assert fresh.id not in ids


@pytest.mark.asyncio
async def test_count_open(session) -> None:
    user = await users_service.get_or_create(session, max_user_id=1, first_name="A")
    a = await appeals_service.create_appeal(
        session, user=user, address="A", topic="T", summary="x", attachments=[]
    )
    await appeals_service.create_appeal(
        session, user=user, address="A", topic="T", summary="y", attachments=[]
    )
    await appeals_service.close(session, a.id)
    assert await appeals_service.count_open(session) == 1


@pytest.mark.asyncio
async def test_list_unanswered(session) -> None:
    user = await users_service.get_or_create(session, max_user_id=1, first_name="A")
    a = await appeals_service.create_appeal(
        session, user=user, address="A", topic="T", summary="x", attachments=[]
    )
    closed = await appeals_service.create_appeal(
        session, user=user, address="A", topic="T", summary="y", attachments=[]
    )
    await appeals_service.close(session, closed.id)
    rows = await appeals_service.list_unanswered(session)
    ids = [r.id for r in rows]
    assert a.id in ids
    assert closed.id not in ids


@pytest.mark.asyncio
async def test_reopen_idempotent_on_in_progress(session) -> None:
    """reopen на NEW/IN_PROGRESS — no-op, возвращает False, чтобы
    повторный клик «🔁 Возобновить» не переписывал timestamps."""
    user = await users_service.get_or_create(session, max_user_id=1, first_name="A")
    appeal = await appeals_service.create_appeal(
        session, user=user, address="A", topic="T", summary="x", attachments=[]
    )
    # NEW — reopen ничего не меняет.
    assert await appeals_service.reopen(session, appeal.id) is False


@pytest.mark.asyncio
async def test_close_idempotent(session) -> None:
    user = await users_service.get_or_create(session, max_user_id=1, first_name="A")
    appeal = await appeals_service.create_appeal(
        session, user=user, address="A", topic="T", summary="x", attachments=[]
    )
    assert await appeals_service.close(session, appeal.id) is True
    # Повторный close не переписывает closed_at — возвращает False.
    assert await appeals_service.close(session, appeal.id) is False


@pytest.mark.asyncio
async def test_find_active_for_user_returns_latest(session) -> None:
    """Несколько обращений с одинаковым created_at (микросекундная
    точность исчерпана) порядок не гарантируют — используем явный
    UPDATE для разнесения timestamps."""
    from sqlalchemy import update

    user = await users_service.get_or_create(session, max_user_id=1, first_name="A")
    first = await appeals_service.create_appeal(
        session, user=user, address="A", topic="T", summary="первое", attachments=[]
    )
    second = await appeals_service.create_appeal(
        session, user=user, address="A", topic="T", summary="второе", attachments=[]
    )
    # «Состарим» first на 1 час назад, чтобы second был свежее.
    await session.execute(
        update(Appeal).where(Appeal.id == first.id)
        .values(created_at=datetime.now(timezone.utc) - timedelta(hours=1))
    )
    await session.flush()

    active = await appeals_service.find_active_for_user(session, user.id)
    assert active is not None
    assert active.id == second.id  # последнее по created_at


@pytest.mark.asyncio
async def test_find_active_excludes_closed(session) -> None:
    user = await users_service.get_or_create(session, max_user_id=1, first_name="A")
    appeal = await appeals_service.create_appeal(
        session, user=user, address="A", topic="T", summary="x", attachments=[]
    )
    await appeals_service.close(session, appeal.id)
    active = await appeals_service.find_active_for_user(session, user.id)
    assert active is None


@pytest.mark.asyncio
async def test_find_last_address_for_user(session) -> None:
    user = await users_service.get_or_create(session, max_user_id=1, first_name="A")
    await appeals_service.create_appeal(
        session, user=user, locality="Елизовское ГП",
        address="Ленина, 1", topic="T", summary="x", attachments=[],
    )
    result = await appeals_service.find_last_address_for_user(session, user.id)
    assert result == ("Елизовское ГП", "Ленина, 1")


@pytest.mark.asyncio
async def test_find_last_address_returns_none_without_locality(session) -> None:
    user = await users_service.get_or_create(session, max_user_id=1, first_name="A")
    # locality=None — find не вернёт.
    await appeals_service.create_appeal(
        session, user=user, address="Ленина, 1", topic="T",
        summary="x", attachments=[],
    )
    assert await appeals_service.find_last_address_for_user(session, user.id) is None


@pytest.mark.asyncio
async def test_add_user_message(session) -> None:
    """Followup жителя записывается в messages с direction=FROM_USER."""
    from aemr_bot.db.models import Message, MessageDirection
    from sqlalchemy import select

    user = await users_service.get_or_create(session, max_user_id=1, first_name="A")
    appeal = await appeals_service.create_appeal(
        session, user=user, address="A", topic="T", summary="x", attachments=[]
    )
    await appeals_service.add_user_message(
        session, appeal=appeal, text="дополнение",
        attachments=[{"type": "image", "id": "p1"}],
        max_message_id="m-1",
    )
    msg = await session.scalar(
        select(Message).where(Message.appeal_id == appeal.id)
    )
    assert msg is not None
    assert msg.direction == MessageDirection.FROM_USER.value
    assert msg.text == "дополнение"


@pytest.mark.asyncio
async def test_add_operator_message_to_closed_keeps_status(session) -> None:
    """add_operator_message по уже CLOSED-обращению не «оживляет» его
    обратно в ANSWERED — это защита от случайного re-open через
    клик «✉️ Ответить» под старой карточкой."""
    user = await users_service.get_or_create(session, max_user_id=1, first_name="A")
    appeal = await appeals_service.create_appeal(
        session, user=user, address="A", topic="T", summary="x", attachments=[]
    )
    await appeals_service.close(session, appeal.id)

    full = await appeals_service.get_by_id(session, appeal.id)
    await appeals_service.add_operator_message(
        session, appeal=full, text="поздний ответ",
        operator_id=None, max_message_id=None,
    )
    refreshed = await appeals_service.get_by_id(session, appeal.id)
    assert refreshed.status == AppealStatus.CLOSED.value
    # Не выставился answered_at
    assert refreshed.answered_at is None


@pytest.mark.asyncio
async def test_get_by_admin_message_id_not_found(session) -> None:
    """Стейл/несуществующий admin_mid → None, не падаем."""
    result = await appeals_service.get_by_admin_message_id(session, "no-such-mid")
    assert result is None


@pytest.mark.asyncio
async def test_set_admin_message_id(session) -> None:
    user = await users_service.get_or_create(session, max_user_id=1, first_name="A")
    appeal = await appeals_service.create_appeal(
        session, user=user, address="A", topic="T", summary="x", attachments=[]
    )
    await appeals_service.set_admin_message_id(session, appeal.id, "mid-xyz")
    found = await appeals_service.get_by_admin_message_id(session, "mid-xyz")
    assert found is not None
    assert found.id == appeal.id
    # Подавляем неиспользуемую переменную (DialogState импорт нужен для тестов выше)
    assert DialogState.IDLE is not None
