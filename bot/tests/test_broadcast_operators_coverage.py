"""Покрытие сервис-слоя БД: ``services/operators.py`` и
``services/broadcasts.py``.

Зачем отдельный файл. Оба модуля — тонкие SQLAlchemy-сервисы (CRUD +
агрегаты). Существующие тесты их НЕ исполняют по-настоящему:

- ``test_admin_operators*.py`` гоняют *handler*-слой и патчат
  ``operators_service.*`` AsyncMock'ами — сами SQL-запросы не выполняются
  (модуль висел на 17 % покрытия).
- ``test_broadcasts_service_pg.py`` — интеграционный, требует Postgres и
  целиком ``skip``-ается в pure-unit окружении (модуль — 35 %).

Здесь поднимается настоящий движок ``sqlite+aiosqlite`` в памяти и SQL
исполняется по-честному: проверяются ветки фильтрации получателей,
guard'ы rowcount при отмене рассылки, fallback счётчиков при аварийном
завершении, мягкая деактивация операторов, защита единственного IT и
audit-журналирование. Это REAL-поведение и REAL-ветки, а не моки.

JSONB→JSON. Модели используют ``postgresql.JSONB`` (``dialog_data``,
``attachments``, ``details`` и т. д.), который sqlite не знает. Локальный
``@compiles(JSONB, "sqlite")`` транслирует его в обычный ``JSON`` — этого
достаточно: ни один тест здесь не опирается на серверную JSONB-семантику
(операторы ``@>``, индексы GIN), только на хранение/чтение dict|list.
Override регистрируется один раз на процесс и на Postgres-прогон в CI не
влияет (диалект там ``postgresql`` — visit_JSONB остаётся родным).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import JSON, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from aemr_bot.db.models import (
    AuditLog,
    Base,
    Broadcast,
    BroadcastStatus,
    OperatorRole,
    User,
)
from aemr_bot.services import broadcasts as bc
from aemr_bot.services import operators as ops


# ── JSONB-шим для sqlite ──────────────────────────────────────────────────
# Регистрируется идемпотентно: повторный импорт модуля при сборе тестов не
# создаёт второй обработчик (SQLAlchemy держит реестр по (тип, диалект)).
@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json_on_sqlite(element, compiler, **kw):  # noqa: ANN001
    return compiler.visit_JSON(JSON(), **kw)


@pytest_asyncio.fixture
async def sqlite_session() -> AsyncIterator:
    """Чистая in-memory сессия на каждый тест.

    Своя, а не conftest-фикстура ``session``: та намеренно ``skip``-ает
    sqlite (ждёт Postgres ради серверной JSONB-семантики). Здесь же
    JSONB-шим выше делает sqlite пригодным для CRUD-проверок, поэтому
    поднимаем движок локально. ``expire_on_commit=False`` — чтобы читать
    атрибуты ORM-объектов после ``flush`` без повторного select.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()
    await engine.dispose()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ══════════════════════════════════════════════════════════════════════════
#                          services/operators.py
# ══════════════════════════════════════════════════════════════════════════


class TestOperatorsUpsertAndGetters:
    """``upsert`` (insert vs update), ``get`` vs ``get_any``."""

    @pytest.mark.asyncio
    async def test_upsert_inserts_new_then_updates_existing(
        self, sqlite_session
    ) -> None:
        """Первый upsert вставляет строку; повторный с тем же
        ``max_user_id`` НЕ плодит дубль, а обновляет имя/роль и снимает
        деактивацию (``is_active=True``)."""
        s = sqlite_session
        op = await ops.upsert(s, max_user_id=1, full_name="Иванов", role=OperatorRole.AEMR)
        assert op.id is not None
        assert op.is_active is True
        assert op.role == OperatorRole.AEMR.value

        # Деактивируем, затем upsert с другой ролью/именем.
        await ops.deactivate(s, 1)
        op2 = await ops.upsert(
            s, max_user_id=1, full_name="Иванов-Петров", role=OperatorRole.IT
        )
        # Та же строка (тот же PK), реактивирована, поля перезаписаны.
        assert op2.id == op.id
        assert op2.full_name == "Иванов-Петров"
        assert op2.role == OperatorRole.IT.value
        assert op2.is_active is True
        # Ровно одна строка в таблице.
        assert len(await ops.list_all(s)) == 1

    @pytest.mark.asyncio
    async def test_get_hides_deactivated_but_get_any_returns_it(
        self, sqlite_session
    ) -> None:
        """``get`` отдаёт только активного; ``get_any`` — любого, включая
        деактивированного (это нужно для реактивации из карточки)."""
        s = sqlite_session
        await ops.upsert(s, max_user_id=2, full_name="Спящий", role=OperatorRole.AEMR)
        await ops.deactivate(s, 2)

        assert await ops.get(s, 2) is None
        any_op = await ops.get_any(s, 2)
        assert any_op is not None
        assert any_op.is_active is False

    @pytest.mark.asyncio
    async def test_get_returns_none_for_unknown_id(self, sqlite_session) -> None:
        """Неизвестный max_user_id → None и в ``get``, и в ``get_any``."""
        s = sqlite_session
        assert await ops.get(s, 99999) is None
        assert await ops.get_any(s, 99999) is None


class TestOperatorsDeactivateAndRole:
    @pytest.mark.asyncio
    async def test_deactivate_active_returns_updated_row(
        self, sqlite_session
    ) -> None:
        """Деактивация активного → возвращается строка с
        ``is_active=False``; повторная деактивация той же записи → None
        (активного больше нет)."""
        s = sqlite_session
        await ops.upsert(s, max_user_id=3, full_name="Уволенный", role=OperatorRole.COORDINATOR)
        res = await ops.deactivate(s, 3)
        assert res is not None
        assert res.is_active is False
        # Второй вызов — активного нет → None.
        assert await ops.deactivate(s, 3) is None

    @pytest.mark.asyncio
    async def test_deactivate_unknown_returns_none(self, sqlite_session) -> None:
        s = sqlite_session
        assert await ops.deactivate(s, 12345) is None

    @pytest.mark.asyncio
    async def test_change_role_active_and_missing(self, sqlite_session) -> None:
        """``change_role`` меняет роль активному и возвращает строку; для
        несуществующего/деактивированного → None, роль не трогается."""
        s = sqlite_session
        await ops.upsert(s, max_user_id=4, full_name="Сидоров", role=OperatorRole.AEMR)
        changed = await ops.change_role(s, 4, OperatorRole.COORDINATOR)
        assert changed is not None
        assert changed.role == OperatorRole.COORDINATOR.value
        # Деактивированный не меняется (where is_active=True не находит).
        await ops.deactivate(s, 4)
        assert await ops.change_role(s, 4, OperatorRole.IT) is None
        # Несуществующий.
        assert await ops.change_role(s, 777, OperatorRole.IT) is None


class TestOperatorsListAndCount:
    @pytest.mark.asyncio
    async def test_list_active_excludes_deactivated(self, sqlite_session) -> None:
        """``list_active`` отдаёт только активных; ``list_all`` — всех,
        деактивированные в конце (order by is_active desc)."""
        s = sqlite_session
        await ops.upsert(s, max_user_id=10, full_name="Активный А", role=OperatorRole.AEMR)
        await ops.upsert(s, max_user_id=11, full_name="Активный Б", role=OperatorRole.IT)
        await ops.upsert(s, max_user_id=12, full_name="Бывший В", role=OperatorRole.COORDINATOR)
        await ops.deactivate(s, 12)

        active = await ops.list_active(s)
        assert {o.max_user_id for o in active} == {10, 11}

        all_ops = await ops.list_all(s)
        assert len(all_ops) == 3
        # Деактивированный — последним (is_active.desc()).
        assert all_ops[-1].max_user_id == 12

    @pytest.mark.asyncio
    async def test_count_active_by_role_and_has_any_it(
        self, sqlite_session
    ) -> None:
        """``count_active_by_role`` считает только активных нужной роли;
        ``has_any_it`` — есть ли хоть один активный IT."""
        s = sqlite_session
        assert await ops.has_any_it(s) is False
        assert await ops.count_active_by_role(s, OperatorRole.IT) == 0

        await ops.upsert(s, max_user_id=20, full_name="IT-1", role=OperatorRole.IT)
        await ops.upsert(s, max_user_id=21, full_name="IT-2", role=OperatorRole.IT)
        await ops.upsert(s, max_user_id=22, full_name="AEMR-1", role=OperatorRole.AEMR)
        # Один IT деактивирован — в счёт не идёт.
        await ops.deactivate(s, 21)

        assert await ops.count_active_by_role(s, OperatorRole.IT) == 1
        assert await ops.count_active_by_role(s, OperatorRole.AEMR) == 1
        assert await ops.has_any_it(s) is True
        # Деактивируем последнего IT → has_any_it False.
        await ops.deactivate(s, 20)
        assert await ops.has_any_it(s) is False


class TestWriteAudit:
    @pytest.mark.asyncio
    async def test_write_audit_persists_row(self, sqlite_session) -> None:
        """``write_audit`` пишет строку в ``audit_log`` с action/target/
        details. details — JSON-dict (через шим)."""
        s = sqlite_session
        await ops.write_audit(
            s,
            operator_max_user_id=7,
            action="operator_role_change",
            target="42",
            details={"old_role": "aemr", "new_role": "it"},
        )
        from sqlalchemy import select

        rows = (await s.scalars(select(AuditLog))).all()
        assert len(rows) == 1
        assert rows[0].action == "operator_role_change"
        assert rows[0].target == "42"
        assert rows[0].details == {"old_role": "aemr", "new_role": "it"}
        assert rows[0].operator_max_user_id == 7

    @pytest.mark.asyncio
    async def test_write_audit_allows_null_operator_and_details(
        self, sqlite_session
    ) -> None:
        """Системное действие: operator_max_user_id=None, details=None —
        обе колонки nullable, запись проходит."""
        s = sqlite_session
        await ops.write_audit(s, operator_max_user_id=None, action="system_event")
        from sqlalchemy import select

        row = (await s.scalars(select(AuditLog))).one()
        assert row.operator_max_user_id is None
        assert row.details is None
        assert row.target is None


class TestCleanupStaleOperators:
    @pytest.mark.asyncio
    async def test_empty_member_ids_is_noop(self, sqlite_session) -> None:
        """Защита от сетевой флуктуации: пустой ``current_member_ids`` →
        НИКОГО не деактивируем, возвращаем []."""
        s = sqlite_session
        await ops.upsert(s, max_user_id=30, full_name="Кто-то", role=OperatorRole.AEMR)
        result = await ops.cleanup_stale_operators(s, current_member_ids=set())
        assert result == []
        # Оператор всё ещё активен.
        assert await ops.get(s, 30) is not None

    @pytest.mark.asyncio
    async def test_deactivates_only_absent_non_protected(
        self, sqlite_session
    ) -> None:
        """Деактивируется только активный не-IT, которого нет в группе.
        IT защищён; присутствующий в группе — не трогается. На каждого
        деактивированного пишется audit-строка с reason=left_admin_chat.
        """
        s = sqlite_session
        await ops.upsert(s, max_user_id=40, full_name="IT Главный", role=OperatorRole.IT)
        await ops.upsert(s, max_user_id=41, full_name="В группе", role=OperatorRole.AEMR)
        await ops.upsert(s, max_user_id=42, full_name="Ушёл", role=OperatorRole.COORDINATOR)
        # IT (40) тоже отсутствует в группе, но protected → не трогаем.
        deactivated = await ops.cleanup_stale_operators(
            s, current_member_ids={41}
        )
        ids = {o.max_user_id for o in deactivated}
        assert ids == {42}
        # 40 (IT) и 41 (в группе) остались активны.
        assert await ops.get(s, 40) is not None
        assert await ops.get(s, 41) is not None
        assert await ops.get(s, 42) is None

        # Audit зафиксировал авто-деактивацию.
        from sqlalchemy import select

        audit = (
            await s.scalars(
                select(AuditLog).where(
                    AuditLog.action == "operator_auto_deactivated_stale"
                )
            )
        ).all()
        assert len(audit) == 1
        assert audit[0].target == "42"
        assert audit[0].details["reason"] == "left_admin_chat"
        assert audit[0].details["role"] == "coordinator"
        # Системное действие — без operator_max_user_id.
        assert audit[0].operator_max_user_id is None

    @pytest.mark.asyncio
    async def test_custom_protected_role(self, sqlite_session) -> None:
        """``protected_role`` параметризуется: с protected=COORDINATOR
        IT-оператор уже НЕ защищён и деактивируется, если его нет в
        группе."""
        s = sqlite_session
        await ops.upsert(s, max_user_id=50, full_name="IT Уязвимый", role=OperatorRole.IT)
        await ops.upsert(s, max_user_id=51, full_name="Coord Защищён", role=OperatorRole.COORDINATOR)
        deactivated = await ops.cleanup_stale_operators(
            s,
            current_member_ids={999},  # никого из наших нет
            protected_role=OperatorRole.COORDINATOR,
        )
        ids = {o.max_user_id for o in deactivated}
        # IT деактивирован (он больше не protected), Coordinator защищён.
        assert ids == {50}
        assert await ops.get(s, 51) is not None

    @pytest.mark.asyncio
    async def test_all_members_present_deactivates_nobody(
        self, sqlite_session
    ) -> None:
        """Все активные операторы в группе → список деактивированных пуст,
        audit не пишется."""
        s = sqlite_session
        await ops.upsert(s, max_user_id=60, full_name="A", role=OperatorRole.AEMR)
        await ops.upsert(s, max_user_id=61, full_name="B", role=OperatorRole.COORDINATOR)
        result = await ops.cleanup_stale_operators(
            s, current_member_ids={60, 61}
        )
        assert result == []
        from sqlalchemy import select

        assert (await s.scalars(select(AuditLog))).all() == []


class TestBootstrapItFromEnv:
    """``bootstrap_it_from_env`` использует ``pg_advisory_xact_lock`` —
    Postgres-специфичный. На sqlite этой функции нет, поэтому перехватываем
    ровно тот ``session.execute``, который шлёт advisory-lock, и пропускаем
    его (no-op). Вся остальная логика (has_any_it → upsert) исполняется
    по-настоящему."""

    @staticmethod
    def _patch_advisory_lock(session, monkeypatch):
        orig_execute = session.execute

        async def _execute(stmt, *args, **kwargs):
            if "pg_advisory_xact_lock" in str(stmt):
                return None
            return await orig_execute(stmt, *args, **kwargs)

        monkeypatch.setattr(session, "execute", _execute)

    @pytest.mark.asyncio
    async def test_inserts_first_it_when_none(
        self, sqlite_session, monkeypatch
    ) -> None:
        """Пустая таблица → bootstrap вставляет IT и возвращает True."""
        s = sqlite_session
        self._patch_advisory_lock(s, monkeypatch)
        inserted = await ops.bootstrap_it_from_env(
            s, max_user_id=100, full_name="Первый IT"
        )
        assert inserted is True
        assert await ops.has_any_it(s) is True
        op = await ops.get(s, 100)
        assert op.role == OperatorRole.IT.value

    @pytest.mark.asyncio
    async def test_noop_when_it_already_exists(
        self, sqlite_session, monkeypatch
    ) -> None:
        """Активный IT уже есть → bootstrap НИЧЕГО не делает, возвращает
        False, новой строки не появляется."""
        s = sqlite_session
        self._patch_advisory_lock(s, monkeypatch)
        await ops.upsert(s, max_user_id=200, full_name="Существующий IT", role=OperatorRole.IT)
        before = len(await ops.list_all(s))

        inserted = await ops.bootstrap_it_from_env(
            s, max_user_id=201, full_name="Лишний IT"
        )
        assert inserted is False
        assert len(await ops.list_all(s)) == before
        # Новый id не вставлен.
        assert await ops.get_any(s, 201) is None


# ══════════════════════════════════════════════════════════════════════════
#                          services/broadcasts.py
# ══════════════════════════════════════════════════════════════════════════


def _add_user(
    session,
    *,
    max_user_id: int,
    first_name: str = "Имя",
    subscribed: bool = True,
    consent: bool = True,
    blocked: bool = False,
) -> None:
    session.add(
        User(
            max_user_id=max_user_id,
            first_name=first_name,
            subscribed_broadcast=subscribed,
            consent_broadcast_at=_utcnow() if consent else None,
            is_blocked=blocked,
        )
    )


class TestSubscriptionAndEligibility:
    @pytest.mark.asyncio
    async def test_is_subscribed_and_set_subscription(
        self, sqlite_session
    ) -> None:
        """``is_subscribed`` читает флаг; ``set_subscription`` его меняет.
        Несуществующий пользователь → False (нет строки)."""
        s = sqlite_session
        _add_user(s, max_user_id=1, subscribed=True)
        await s.flush()
        assert await bc.is_subscribed(s, 1) is True
        assert await bc.is_subscribed(s, 404) is False

        await bc.set_subscription(s, 1, False)
        assert await bc.is_subscribed(s, 1) is False
        await bc.set_subscription(s, 1, True)
        assert await bc.is_subscribed(s, 1) is True

    @pytest.mark.asyncio
    async def test_eligibility_excludes_blocked_erased_and_unconsented(
        self, sqlite_session
    ) -> None:
        """В выборку получателей попадает только подписанный + с
        consent_broadcast_at + не заблокированный + не обезличенный.

        Проверяем все четыре условия ``_eligible_filter`` через
        ``count_subscribers`` и ``list_subscriber_targets``.
        """
        s = sqlite_session
        _add_user(s, max_user_id=1, first_name="Годный")  # eligible
        _add_user(s, max_user_id=2, first_name="Заблок", blocked=True)
        _add_user(s, max_user_id=3, first_name="Удалено")  # обезличен
        _add_user(s, max_user_id=4, first_name="БезСогл", consent=False)
        _add_user(s, max_user_id=5, first_name="Отписан", subscribed=False)
        await s.flush()

        assert await bc.count_subscribers(s) == 1
        targets = await bc.list_subscriber_targets(s)
        # (db_id, max_user_id) — единственный годный.
        assert len(targets) == 1
        assert targets[0][1] == 1

    @pytest.mark.asyncio
    async def test_count_subscribers_zero_when_empty(
        self, sqlite_session
    ) -> None:
        """Пустая таблица пользователей → 0 (ветка ``or 0``)."""
        s = sqlite_session
        assert await bc.count_subscribers(s) == 0
        assert await bc.list_subscriber_targets(s) == []


class TestBroadcastLifecycle:
    @pytest.mark.asyncio
    async def test_create_draft_defaults_and_attachments(
        self, sqlite_session
    ) -> None:
        """``create_broadcast`` создаёт DRAFT; attachments=None → []
        (text-only), переданный список сохраняется как есть."""
        s = sqlite_session
        b1 = await bc.create_broadcast(
            s, text="Без картинок", operator_id=None, subscriber_count=10
        )
        assert b1.status == BroadcastStatus.DRAFT.value
        assert b1.attachments == []
        assert b1.subscriber_count_at_start == 10

        b2 = await bc.create_broadcast(
            s,
            text="С картинкой",
            operator_id=7,
            subscriber_count=3,
            attachments=[{"type": "image", "token": "T"}],
        )
        assert b2.attachments == [{"type": "image", "token": "T"}]
        assert b2.created_by_operator_id == 7

    @pytest.mark.asyncio
    async def test_mark_started_sets_sending_and_admin_mid(
        self, sqlite_session
    ) -> None:
        """``mark_started`` → статус SENDING, проставляется
        admin_message_id и started_at; ``get_status`` это видит."""
        s = sqlite_session
        b = await bc.create_broadcast(
            s, text="x", operator_id=None, subscriber_count=1
        )
        await bc.mark_started(s, b.id, "ADMIN-MID-9")
        assert await bc.get_status(s, b.id) == BroadcastStatus.SENDING.value
        refreshed = await bc.get_by_id(s, b.id)
        assert refreshed.admin_message_id == "ADMIN-MID-9"
        assert refreshed.started_at is not None

    @pytest.mark.asyncio
    async def test_get_status_none_for_missing(self, sqlite_session) -> None:
        s = sqlite_session
        assert await bc.get_status(s, 99999) is None
        assert await bc.get_by_id(s, 99999) is None


class TestCancelGuards:
    @pytest.mark.asyncio
    async def test_request_cancel_only_affects_sending(
        self, sqlite_session
    ) -> None:
        """``request_cancel`` отменяет только SENDING (rowcount>0 → True);
        повторная отмена уже-терминального → False (rowcount=0)."""
        s = sqlite_session
        b = await bc.create_broadcast(
            s, text="x", operator_id=None, subscriber_count=1
        )
        await bc.mark_started(s, b.id, None)
        assert await bc.request_cancel(s, b.id) is True
        assert await bc.get_status(s, b.id) == BroadcastStatus.CANCELLED.value
        # Второй раз — уже не SENDING.
        assert await bc.request_cancel(s, b.id) is False

    @pytest.mark.asyncio
    async def test_request_cancel_draft_is_false(self, sqlite_session) -> None:
        """DRAFT никогда не был SENDING → request_cancel False, статус
        не меняется."""
        s = sqlite_session
        b = await bc.create_broadcast(
            s, text="x", operator_id=None, subscriber_count=1
        )
        assert await bc.request_cancel(s, b.id) is False
        assert await bc.get_status(s, b.id) == BroadcastStatus.DRAFT.value

    @pytest.mark.asyncio
    async def test_mark_cancelled_only_affects_draft(
        self, sqlite_session
    ) -> None:
        """``mark_cancelled`` (отмена в cooldown) гасит DRAFT → True;
        повтор → False; SENDING им не отменить (это дело request_cancel)."""
        s = sqlite_session
        b = await bc.create_broadcast(
            s, text="x", operator_id=None, subscriber_count=1
        )
        assert await bc.mark_cancelled(s, b.id) is True
        assert await bc.get_status(s, b.id) == BroadcastStatus.CANCELLED.value
        assert await bc.mark_cancelled(s, b.id) is False

        # Отдельная рассылка в SENDING — mark_cancelled её не трогает.
        b2 = await bc.create_broadcast(
            s, text="y", operator_id=None, subscriber_count=1
        )
        await bc.mark_started(s, b2.id, None)
        assert await bc.mark_cancelled(s, b2.id) is False
        assert await bc.get_status(s, b2.id) == BroadcastStatus.SENDING.value


class TestReapers:
    @pytest.mark.asyncio
    async def test_reap_orphaned_sending(self, sqlite_session) -> None:
        """``reap_orphaned_sending`` переводит все SENDING → FAILED при
        старте (умерший процесс). DRAFT и терминальные не трогаются."""
        s = sqlite_session
        b_sending = await bc.create_broadcast(
            s, text="s", operator_id=None, subscriber_count=1
        )
        await bc.mark_started(s, b_sending.id, None)
        b_draft = await bc.create_broadcast(
            s, text="d", operator_id=None, subscriber_count=1
        )

        count = await bc.reap_orphaned_sending(s)
        assert count == 1
        assert await bc.get_status(s, b_sending.id) == BroadcastStatus.FAILED.value
        # finished_at остаётся None (точное время неизвестно).
        assert (await bc.get_by_id(s, b_sending.id)).finished_at is None
        # DRAFT не тронут.
        assert await bc.get_status(s, b_draft.id) == BroadcastStatus.DRAFT.value

    @pytest.mark.asyncio
    async def test_reap_orphaned_sending_zero_when_none(
        self, sqlite_session
    ) -> None:
        """Нет SENDING → 0 (ветка ``rowcount or 0``)."""
        s = sqlite_session
        assert await bc.reap_orphaned_sending(s) == 0

    @pytest.mark.asyncio
    async def test_reap_orphaned_draft_respects_ttl(
        self, sqlite_session
    ) -> None:
        """``reap_orphaned_draft`` гасит DRAFT старше TTL → FAILED, свежий
        DRAFT не трогает."""
        s = sqlite_session
        old = await bc.create_broadcast(
            s, text="old", operator_id=None, subscriber_count=0
        )
        fresh = await bc.create_broadcast(
            s, text="fresh", operator_id=None, subscriber_count=0
        )
        # Состарим первый DRAFT на час.
        await s.execute(
            update(Broadcast)
            .where(Broadcast.id == old.id)
            .values(created_at=_utcnow() - timedelta(hours=1))
        )
        # ``reap_orphaned_draft`` сравнивает ``created_at < cutoff``. На
        # sqlite ORM-bulk-update делает синхронизацию identity-map в Python
        # и спотыкается о naive(sqlite-store) vs aware(cutoff) datetime —
        # артефакт sqlite, не баг кода (Postgres сравнивает на сервере).
        # ``expunge_all`` отцепляет объекты: синхронизировать в памяти
        # больше нечего, ленивой подгрузки нет (объекты detached).
        s.expunge_all()

        count = await bc.reap_orphaned_draft(s, ttl_minutes=30)
        assert count == 1
        assert await bc.get_status(s, old.id) == BroadcastStatus.FAILED.value
        assert await bc.get_status(s, fresh.id) == BroadcastStatus.DRAFT.value


class TestDeliveriesAndCounts:
    @pytest.mark.asyncio
    async def test_record_delivery_single(self, sqlite_session) -> None:
        """``record_delivery`` пишет одну строку доставки; успех →
        delivered_at проставлен, ошибка → delivered_at None + error."""
        s = sqlite_session
        b = await bc.create_broadcast(
            s, text="x", operator_id=None, subscriber_count=2
        )
        await bc.record_delivery(s, broadcast_id=b.id, user_id=1, error=None)
        await bc.record_delivery(
            s, broadcast_id=b.id, user_id=2, error="user blocked bot"
        )
        delivered, failed = await bc.count_delivery_results(s, b.id)
        assert delivered == 1
        assert failed == 1

    @pytest.mark.asyncio
    async def test_record_deliveries_batch_and_empty(
        self, sqlite_session
    ) -> None:
        """``record_deliveries`` батчем; пустой список — ранний выход
        (ничего не пишет)."""
        s = sqlite_session
        b = await bc.create_broadcast(
            s, text="x", operator_id=None, subscriber_count=3
        )
        # Пустой батч — no-op.
        await bc.record_deliveries(s, broadcast_id=b.id, results=[])
        assert await bc.count_delivery_results(s, b.id) == (0, 0)

        await bc.record_deliveries(
            s,
            broadcast_id=b.id,
            results=[(1, None), (2, None), (3, "fail")],
        )
        assert await bc.count_delivery_results(s, b.id) == (2, 1)

    @pytest.mark.asyncio
    async def test_update_progress(self, sqlite_session) -> None:
        """``update_progress`` обновляет счётчики на лету (не финализирует
        статус)."""
        s = sqlite_session
        b = await bc.create_broadcast(
            s, text="x", operator_id=None, subscriber_count=5
        )
        await bc.mark_started(s, b.id, None)
        await bc.update_progress(s, b.id, delivered=3, failed=1)
        refreshed = await bc.get_by_id(s, b.id)
        assert refreshed.delivered_count == 3
        assert refreshed.failed_count == 1
        # Статус остался SENDING.
        assert refreshed.status == BroadcastStatus.SENDING.value


class TestMarkFinished:
    @pytest.mark.asyncio
    async def test_mark_finished_normal_counts(self, sqlite_session) -> None:
        """Обычное завершение DONE с явными счётчиками — пишутся как есть,
        проставляется finished_at."""
        s = sqlite_session
        b = await bc.create_broadcast(
            s, text="x", operator_id=None, subscriber_count=10
        )
        await bc.mark_started(s, b.id, None)
        await bc.mark_finished(
            s, b.id, status=BroadcastStatus.DONE, delivered=8, failed=2
        )
        refreshed = await bc.get_by_id(s, b.id)
        assert refreshed.status == BroadcastStatus.DONE.value
        assert refreshed.delivered_count == 8
        assert refreshed.failed_count == 2
        assert refreshed.finished_at is not None

    @pytest.mark.asyncio
    async def test_mark_finished_failed_zero_falls_back_to_recorded(
        self, sqlite_session
    ) -> None:
        """Защитный слой: FAILED с нулями, но в БД уже есть записи доставки
        → счётчики берутся из ``count_delivery_results`` (а не нули), чтобы
        оператор не запустил повторную рассылку вслепую."""
        s = sqlite_session
        b = await bc.create_broadcast(
            s, text="x", operator_id=None, subscriber_count=4
        )
        await bc.mark_started(s, b.id, None)
        # Часть доставок записана до падения.
        await bc.record_deliveries(
            s,
            broadcast_id=b.id,
            results=[(1, None), (2, None), (3, "err")],
        )
        # Wrapper передал нули (непредвиденная ошибка).
        await bc.mark_finished(
            s, b.id, status=BroadcastStatus.FAILED, delivered=0, failed=0
        )
        refreshed = await bc.get_by_id(s, b.id)
        # Не нули — реальные записанные результаты.
        assert refreshed.delivered_count == 2
        assert refreshed.failed_count == 1

    @pytest.mark.asyncio
    async def test_mark_finished_failed_zero_with_no_records_stays_zero(
        self, sqlite_session
    ) -> None:
        """FAILED с нулями и пустой таблицей доставок → fallback вернёт
        (0,0), счётчики остаются нулевыми (рассылка реально не стартовала
        доставлять)."""
        s = sqlite_session
        b = await bc.create_broadcast(
            s, text="x", operator_id=None, subscriber_count=4
        )
        await bc.mark_started(s, b.id, None)
        await bc.mark_finished(
            s, b.id, status=BroadcastStatus.FAILED, delivered=0, failed=0
        )
        refreshed = await bc.get_by_id(s, b.id)
        assert refreshed.delivered_count == 0
        assert refreshed.failed_count == 0


class TestListings:
    @pytest.mark.asyncio
    async def test_list_recent_orders_by_created_desc_and_limit(
        self, sqlite_session
    ) -> None:
        """``list_recent`` — свежие первыми, лимит соблюдается."""
        s = sqlite_session
        ids = []
        for i in range(3):
            b = await bc.create_broadcast(
                s, text=f"b{i}", operator_id=None, subscriber_count=0
            )
            ids.append(b.id)
        # Разнесём created_at, чтобы порядок был детерминирован.
        base = _utcnow()
        for offset, bid in enumerate(ids):
            await s.execute(
                update(Broadcast)
                .where(Broadcast.id == bid)
                .values(created_at=base + timedelta(minutes=offset))
            )
        recent = await bc.list_recent(s, limit=2)
        assert len(recent) == 2
        # Последний созданный (ids[-1]) — первым.
        assert recent[0].id == ids[-1]

    @pytest.mark.asyncio
    async def test_list_failed_deliveries_join_and_fallback_name(
        self, sqlite_session
    ) -> None:
        """``list_failed_deliveries`` джойнит User для имени, отдаёт только
        строки с error, упорядочены по error→user_id. Имя None → '—'."""
        s = sqlite_session
        # Пользователи с db-id (PK), на них ссылается BroadcastDelivery.user_id.
        u1 = User(max_user_id=1001, first_name="Анна", subscribed_broadcast=True)
        u2 = User(max_user_id=1002, first_name=None, subscribed_broadcast=True)
        s.add_all([u1, u2])
        await s.flush()

        b = await bc.create_broadcast(
            s, text="x", operator_id=None, subscriber_count=2
        )
        await bc.record_deliveries(
            s,
            broadcast_id=b.id,
            results=[
                (u1.id, "bot blocked"),
                (u2.id, "user deleted"),
                (u1.id, None),  # успех — НЕ должен попасть в failed
            ],
        )
        failed = await bc.list_failed_deliveries(s, b.id)
        # Только две строки с error.
        assert len(failed) == 2
        # Кортежи (user_db_id, first_name, error); имя None → '—'.
        by_error = {row[2]: row for row in failed}
        assert by_error["bot blocked"][1] == "Анна"
        assert by_error["user deleted"][1] == "—"

    @pytest.mark.asyncio
    async def test_list_failed_deliveries_respects_limit(
        self, sqlite_session
    ) -> None:
        """Лимит отсечки соблюдается — caller по длине==limit понимает,
        что есть ещё."""
        s = sqlite_session
        u = User(max_user_id=2001, first_name="Петр", subscribed_broadcast=True)
        s.add(u)
        await s.flush()
        b = await bc.create_broadcast(
            s, text="x", operator_id=None, subscriber_count=5
        )
        await bc.record_deliveries(
            s,
            broadcast_id=b.id,
            results=[(u.id, f"err-{i}") for i in range(5)],
        )
        failed = await bc.list_failed_deliveries(s, b.id, limit=3)
        assert len(failed) == 3
