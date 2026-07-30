"""Черновик мастера рассылок переживает рестарт бота.

Мастер операторов умел это давно (wizard_state, kind='op'), а мастер
рассылок — нет: оператор набирал текст экстренного оповещения, бот
перезапускался, черновик пропадал. Здесь проверяется достроенная
цепочка:

    handlers/broadcast_wizard._wizards  (мутация)
        → wizard_registry.schedule_persist_broadcast
        → wizard_persist.save_broadcast_wizard      (таблица wizard_state)
        → wizard_persist.hydrate_into_registry      (старт бота)
        → main._hydrate_wizards                     (обратно в _wizards)

Плюс два предохранителя: протухшая запись не воскресает, и восстановление
НЕ запускает никакую отправку — поднимается только черновик.

БД здесь нет: `save/delete_broadcast_wizard` подменены записывающими
заглушками, а для hydrate поднята мини-эмуляция сессии (реальный SQL
проверяется на PG в CI).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from aemr_bot import main as main_module
from aemr_bot.config import settings as cfg
from aemr_bot.handlers import broadcast_wizard as bw
from aemr_bot.services import wizard_persist as wp
from aemr_bot.services import wizard_registry as wr


@pytest.fixture(autouse=True)
def _clean_state():
    bw._wizards.clear()
    wr.reset_all()
    yield
    bw._wizards.clear()
    wr.reset_all()


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch):
    """Заглушка «таблицы wizard_state» + перехват фоновых persist-задач.

    Возвращает объект с `.saved` (список (operator_id, snapshot)),
    `.deleted` (список operator_id) и `.drain()` — дождаться фоновых
    задач, которые спавнит `wizard_registry`.
    """
    saved: list[tuple[int, dict]] = []
    deleted: list[int] = []
    tasks: list[asyncio.Task] = []

    async def _fake_save(_session, operator_id, state):
        saved.append((operator_id, dict(state)))

    async def _fake_delete(_session, operator_id):
        deleted.append(operator_id)

    @asynccontextmanager
    async def _fake_session_scope():
        yield object()

    real_spawn = wr.spawn_background_task

    def _record(coro, *, name=None):
        task = real_spawn(coro, name=name)
        tasks.append(task)
        return task

    monkeypatch.setattr(wp, "save_broadcast_wizard", _fake_save)
    monkeypatch.setattr(wp, "delete_broadcast_wizard", _fake_delete)
    monkeypatch.setattr(wr, "session_scope", _fake_session_scope)
    monkeypatch.setattr(wr, "spawn_background_task", _record)

    async def _drain() -> None:
        if tasks:
            await asyncio.gather(*tasks)
            tasks.clear()

    return NS(saved=saved, deleted=deleted, drain=_drain)


class _FakeSession:
    """Мини-эмуляция PG для `hydrate_into_registry`.

    `execute(delete)` выкидывает протухшие строки, `scalars(select)`
    отдаёт живые. Отбор по `expires_at` повторён здесь руками — сам SQL
    dialect-specific и проверяется на настоящем PG.
    """

    def __init__(self, rows: list) -> None:
        self.rows = list(rows)

    async def execute(self, _stmt):
        now = datetime.now(timezone.utc)
        stale = [r for r in self.rows if r.expires_at <= now]
        self.rows = [r for r in self.rows if r.expires_at > now]
        return NS(rowcount=len(stale))

    async def scalars(self, _stmt):
        now = datetime.now(timezone.utc)
        alive = [r for r in self.rows if r.expires_at > now]
        return NS(all=lambda: alive)


def _row(operator_id: int, state: dict, *, age_sec: float = 0.0) -> NS:
    """Строка wizard_state так, как её записал бы `_upsert`: expires_at =
    момент записи + TTL. `age_sec` сдвигает момент записи в прошлое."""
    written_at = datetime.now(timezone.utc) - timedelta(seconds=age_sec)
    return NS(
        id=operator_id,
        kind=wp.KIND_BROADCAST,
        operator_max_user_id=operator_id,
        state=state,
        expires_at=written_at + timedelta(seconds=wp._ttl_for(wp.KIND_BROADCAST)),
    )


def _hydrate(monkeypatch: pytest.MonkeyPatch, rows: list) -> None:
    """Прогнать стартовую гидратацию main.py поверх заданных строк."""
    session = _FakeSession(rows)

    @asynccontextmanager
    async def _scope():
        yield session

    monkeypatch.setattr(main_module, "session_scope", _scope)


class TestPersistOnMutation:
    """Каждое изменение черновика доезжает до таблицы wizard_state."""

    @pytest.mark.asyncio
    async def test_start_saves_snapshot(self, db) -> None:
        bw._wizards[7] = bw._WizardState(step="awaiting_text")
        await db.drain()

        assert db.saved, "старт мастера должен сохранять черновик"
        operator_id, snapshot = db.saved[-1]
        assert operator_id == 7
        assert snapshot["step"] == "awaiting_text"
        # monotonic-дедлайн в БД не едет: после рестарта он бессмыслен.
        assert "expires_at" not in snapshot

    @pytest.mark.asyncio
    async def test_typed_text_and_images_saved(self, db) -> None:
        bw._wizards[7] = bw._WizardState(step="awaiting_text")
        state = bw._wizards[7]
        state.text = "🚨 [ЧС] Отключение воды на Ленина"
        state.attachments = [{"type": "image", "payload": {"token": "t1"}}]
        state.step = "awaiting_confirm"
        bw._schedule_persist(7, state)
        await db.drain()

        _, snapshot = db.saved[-1]
        assert snapshot["step"] == "awaiting_confirm"
        assert snapshot["text"] == "🚨 [ЧС] Отключение воды на Ленина"
        assert snapshot["attachments"] == [
            {"type": "image", "payload": {"token": "t1"}}
        ]

    @pytest.mark.asyncio
    async def test_pop_deletes_row(self, db) -> None:
        bw._wizards[7] = bw._WizardState(step="awaiting_text")
        bw._wizards.pop(7, None)
        await db.drain()

        assert db.deleted == [7]

    @pytest.mark.asyncio
    async def test_pop_of_absent_operator_is_silent(self, db) -> None:
        bw._wizards.pop(999, None)
        await db.drain()

        assert db.deleted == []

    @pytest.mark.asyncio
    async def test_cancel_from_another_module_deletes_row(self, db) -> None:
        """`/cancel` в handlers/appeal гасит черновик чужим pop'ом.

        Если бы запись при этом оставалась в БД, после рестарта оператор
        получил бы превью отменённой рассылки с живой кнопкой
        «Разослать».
        """
        from aemr_bot.handlers import broadcast as broadcast_facade

        bw._wizards[7] = bw._WizardState(step="awaiting_confirm", text="x")
        broadcast_facade._wizards.pop(7, None)
        await db.drain()

        assert db.deleted == [7]
        assert bw._wizards == {}

    @pytest.mark.asyncio
    async def test_delete_wins_over_hydrated_registry_copy(self, db) -> None:
        """Удаление после гидратации — именно удаление.

        `schedule_persist_broadcast(id, None)` без явного state берёт его
        из registry. Если бы зеркало в registry не чистилось, уцелевший
        там снимок превратил бы DELETE в повторный UPSERT.
        """
        wr.set_broadcast_wizard(7, {"step": "awaiting_confirm", "text": "x"})
        bw._wizards[7] = bw._WizardState(step="awaiting_confirm", text="x")
        await db.drain()
        db.saved.clear()

        bw._wizards.pop(7, None)
        await db.drain()

        assert db.deleted == [7]
        assert db.saved == []
        assert wr.get_broadcast_wizard(7) is None


class TestHydrateAfterRestart:
    """Старт бота: черновик из БД снова доступен мастеру."""

    @pytest.mark.asyncio
    async def test_draft_restored_into_wizard_dict(
        self, db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 1. Оператор набрал текст — черновик уехал в БД.
        bw._wizards[7] = bw._WizardState(step="awaiting_text")
        state = bw._wizards[7]
        state.text = "Отключение воды"
        state.attachments = [{"type": "image", "payload": {"token": "t1"}}]
        state.step = "awaiting_confirm"
        bw._schedule_persist(7, state)
        await db.drain()
        _, snapshot = db.saved[-1]

        # 2. Рестарт: память пуста, в БД лежит снимок.
        bw._wizards.clear()
        wr.reset_all()
        _hydrate(monkeypatch, [_row(7, snapshot)])

        await main_module._hydrate_wizards()

        restored = bw._wizards[7]
        assert isinstance(restored, bw._WizardState)
        assert restored.step == "awaiting_confirm"
        assert restored.text == "Отключение воды"
        assert restored.attachments == [
            {"type": "image", "payload": {"token": "t1"}}
        ]
        # TTL отсчитывается заново от старта процесса.
        assert not restored.expired()

    @pytest.mark.asyncio
    async def test_restore_does_not_start_any_send(
        self, db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Гидратация поднимает ТОЛЬКО черновик.

        Ни одной фоновой задачи рассылки не появляется: отправку
        по-прежнему запускает подтверждение оператора. Сравниваем «до и
        после», а не с пустым словарём: `_pending_broadcasts` глобален на
        процесс, и соседние файлы тестов оставляют там свои заглушки —
        нас интересует ровно прирост от гидратации.
        """
        from aemr_bot.handlers import broadcast as broadcast_facade

        before = set(broadcast_facade._pending_broadcasts)
        _hydrate(
            monkeypatch,
            [_row(7, {"step": "awaiting_confirm", "text": "готово к отправке"})],
        )

        await main_module._hydrate_wizards()

        assert set(broadcast_facade._pending_broadcasts) == before
        assert bw._wizards[7].step == "awaiting_confirm"

    @pytest.mark.asyncio
    async def test_unknown_step_skipped(
        self, db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _hydrate(monkeypatch, [_row(7, {"step": "sending", "text": "x"})])

        await main_module._hydrate_wizards()

        assert 7 not in bw._wizards


class TestStaleDraftNotRevived:
    """Протухшее не воскресает."""

    def test_db_ttl_matches_in_memory_ttl(self) -> None:
        # Если бы TTL в БД был длиннее, рестарт поднимал бы черновик,
        # который в живом процессе уже вытеснился бы по времени.
        assert wp._ttl_for(wp.KIND_BROADCAST) == cfg.broadcast_wizard_ttl_sec

    @pytest.mark.asyncio
    async def test_expired_row_not_restored(
        self, db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stale = _row(
            7,
            {"step": "awaiting_confirm", "text": "вчерашний черновик"},
            age_sec=cfg.broadcast_wizard_ttl_sec + 60,
        )
        fresh = _row(8, {"step": "awaiting_text", "text": "свежий"})
        _hydrate(monkeypatch, [stale, fresh])

        await main_module._hydrate_wizards()

        assert 7 not in bw._wizards
        assert bw._wizards[8].text == "свежий"

    @pytest.mark.asyncio
    async def test_restored_draft_still_expires_in_memory(
        self, db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _hydrate(monkeypatch, [_row(7, {"step": "awaiting_text", "text": "x"})])
        await main_module._hydrate_wizards()

        # Перематываем монотонные часы за TTL — черновик обязан
        # вытесниться обычным механизмом, без всяких поблажек «он же
        # восстановленный».
        base = bw.time.monotonic()
        monkeypatch.setattr(
            bw.time, "monotonic", lambda: base + cfg.broadcast_wizard_ttl_sec + 1
        )
        assert bw._wizards[7].expired()
        bw._drop_expired_wizards()
        assert 7 not in bw._wizards
