"""Perf-кластер «Latency-UX» (Волна 2): relay-ack, find_resident typing,
find_active LIMIT 1.

Три поведение-сохраняющих perf-фикса, каждый со своим доказывающим
тестом:

(a) `appeal_runtime.persist_and_dispatch_appeal` — подтверждение жителю
    (`APPEAL_ACCEPTED`) уходит СРАЗУ после commit обращения, ДО рендера
    админ-карточки и ДО relay вложений; relay вложений уносится в фон
    (`spawn_background_task`). Раньше житель ждал render + relay (батчи
    под 2 RPS + retry-backoff) — секунды молчания при вложениях.
    Гарантия: обращение закоммичено до ack (commit-блок закрыт раньше).

(b) `admin_resident_search.run_find_resident` — `mark_typing(event,
    admin_group_id)` после guard'ов (is_admin_chat + ensure_operator +
    непустой/валидный query), перед `session_scope`. Симметрично соседним
    operator-listing действиям (admin_panel._do_open_tickets).

(c) `appeals.find_active_for_user` — `.limit(1)` на select открытого
    обращения. `scalar()` и так берёт первую строку; limit лишь снимает
    с БД обязанность отсортировать/материализовать весь набор. Поведение
    идентично (то же последнее обращение).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aemr_bot.db.models import AppealStatus, DialogState


pytest.importorskip("maxapi", reason="нужен maxapi для handler-импортов")

from tests._helpers import make_event


# ──────────────────────────────────────────────────────────────────────
# (c) appeals.find_active_for_user — .limit(1)
# ──────────────────────────────────────────────────────────────────────


class _CapturingSession:
    """Фейковая сессия: запоминает Select, переданный в `scalar()`,
    и возвращает заранее заданный результат."""

    def __init__(self, result=None) -> None:
        self.captured = None
        self._result = result

    async def scalar(self, statement):
        self.captured = statement
        return self._result


class TestFindActiveForUserHasLimit:
    """Поведенческая проверка: запрос несёт LIMIT 1.

    Не строковый assert по исходнику — компилируем реальный Select,
    который функция передаёт в `session.scalar()`, и проверяем, что в
    SQL есть `LIMIT` и что `_limit == 1`.
    """

    @pytest.mark.asyncio
    async def test_query_carries_limit_one(self) -> None:
        from aemr_bot.services import appeals as appeals_service

        session = _CapturingSession(result=None)
        await appeals_service.find_active_for_user(session, user_id=7)

        assert session.captured is not None, (
            "find_active_for_user должен передать Select в session.scalar()"
        )
        compiled = str(
            session.captured.compile(compile_kwargs={"literal_binds": True})
        )
        assert "LIMIT" in compiled, (
            "find_active_for_user обязан добавить .limit(1) — без него БД "
            "сортирует и отдаёт весь набор открытых обращений жителя"
        )
        # Явная проверка значения лимита — ровно 1.
        assert session.captured._limit == 1

    @pytest.mark.asyncio
    async def test_returns_scalar_result_unchanged(self) -> None:
        """Поведение-сохранение: что вернул scalar(), то и вернула
        функция (limit не меняет, какое обращение приходит)."""
        from aemr_bot.services import appeals as appeals_service

        sentinel = SimpleNamespace(id=99, status=AppealStatus.NEW.value)
        session = _CapturingSession(result=sentinel)
        out = await appeals_service.find_active_for_user(session, user_id=7)
        assert out is sentinel

    @pytest.mark.asyncio
    async def test_filters_only_open_statuses(self) -> None:
        """Регресс-гард: фильтр статусов (NEW/IN_PROGRESS) сохранён —
        limit не должен подменять where-clause."""
        from aemr_bot.services import appeals as appeals_service

        session = _CapturingSession(result=None)
        await appeals_service.find_active_for_user(session, user_id=7)
        compiled = str(
            session.captured.compile(compile_kwargs={"literal_binds": True})
        )
        # Значения статусов попадают в SQL ТОЛЬКО как quoted-литералы
        # IN-списка (`appeals.status IN ('new', 'in_progress')`). Сверять
        # надо именно их: select(Appeal) тянет ВСЕ колонки, среди которых
        # answered_at / closed_at / closed_due_to_revoke — их (без кавычек)
        # имена всегда содержат подстроки 'answered'/'closed', поэтому голый
        # substring-assert по значению статуса был бы ложно-провальным.
        assert f"'{AppealStatus.NEW.value}'" in compiled
        assert f"'{AppealStatus.IN_PROGRESS.value}'" in compiled
        # Закрытые/отвеченные не участвуют в фильтре: их quoted-литерал в
        # IN-списке отсутствует (имена колонок closed_at/answered_at — без
        # кавычек и потому в счёт не идут).
        assert f"'{AppealStatus.CLOSED.value}'" not in compiled
        assert f"'{AppealStatus.ANSWERED.value}'" not in compiled


# ──────────────────────────────────────────────────────────────────────
# (b) run_find_resident — mark_typing после guard'ов, перед БД
# ──────────────────────────────────────────────────────────────────────


class TestFindResidentTyping:
    """`run_find_resident` шлёт typing-индикатор после guard'ов и до
    обращения к БД (как соседние operator-listing хендлеры)."""

    @pytest.mark.asyncio
    async def test_mark_typing_called_after_guards(self) -> None:
        """Happy-path: авторизованный оператор, валидный query —
        mark_typing вызван с (event, admin_group_id)."""
        from aemr_bot.config import settings as cfg
        from aemr_bot.handlers import admin_resident_search as mod

        event = make_event(user_id=4242)
        with patch.object(mod, "is_admin_chat", return_value=True), \
             patch.object(mod, "ensure_operator",
                          AsyncMock(return_value=True)), \
             patch.object(mod, "mark_typing", AsyncMock()) as typing, \
             patch.object(mod, "session_scope") as scope, \
             patch.object(mod.users_service, "find_by_max_id",
                          AsyncMock(return_value=None)), \
             patch.object(mod.ops_svc, "write_audit", AsyncMock()):
            scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            await mod.run_find_resident(event, "123456")

        typing.assert_awaited_once()
        # Позиционные аргументы: (event, admin_group_id).
        args = typing.await_args.args
        assert args[0] is event
        assert args[1] == cfg.admin_group_id

    @pytest.mark.asyncio
    async def test_mark_typing_before_db_lookup(self) -> None:
        """Порядок: typing уходит ДО session_scope/lookup — иначе
        оператор видит «зависание» на время запроса."""
        from aemr_bot.handlers import admin_resident_search as mod

        order: list[str] = []

        async def _typing(*_a, **_k) -> None:
            order.append("typing")

        async def _find(*_a, **_k):
            order.append("lookup")
            return None

        scope = MagicMock()
        scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        scope.return_value.__aexit__ = AsyncMock(return_value=False)

        event = make_event(user_id=4242)
        with patch.object(mod, "is_admin_chat", return_value=True), \
             patch.object(mod, "ensure_operator",
                          AsyncMock(return_value=True)), \
             patch.object(mod, "mark_typing", _typing), \
             patch.object(mod, "session_scope", scope), \
             patch.object(mod.users_service, "find_by_max_id", _find), \
             patch.object(mod.ops_svc, "write_audit", AsyncMock()):
            await mod.run_find_resident(event, "123456")

        assert order == ["typing", "lookup"], (
            f"typing должен предшествовать lookup, получили {order}"
        )

    @pytest.mark.asyncio
    async def test_no_typing_for_non_admin_chat(self) -> None:
        """Guard раньше typing: не-админ чат — ни typing, ни ответа."""
        from aemr_bot.handlers import admin_resident_search as mod

        event = make_event(chat_id=42)
        with patch.object(mod, "is_admin_chat", return_value=False), \
             patch.object(mod, "mark_typing", AsyncMock()) as typing:
            await mod.run_find_resident(event, "123456")
        typing.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_typing_for_non_operator(self) -> None:
        """Неоператор отбивается раньше typing."""
        from aemr_bot.handlers import admin_resident_search as mod

        event = make_event(user_id=999)
        with patch.object(mod, "is_admin_chat", return_value=True), \
             patch.object(mod, "ensure_operator",
                          AsyncMock(return_value=False)), \
             patch.object(mod, "mark_typing", AsyncMock()) as typing:
            await mod.run_find_resident(event, "123456")
        typing.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_typing_for_invalid_query(self) -> None:
        """Невалидный query → usage-подсказка, без typing (до БД дело
        не доходит — нечего «печатать»)."""
        from aemr_bot.handlers import admin_resident_search as mod

        event = make_event(user_id=999)
        with patch.object(mod, "is_admin_chat", return_value=True), \
             patch.object(mod, "ensure_operator",
                          AsyncMock(return_value=True)), \
             patch.object(mod, "mark_typing", AsyncMock()) as typing:
            await mod.run_find_resident(event, "abc")
        typing.assert_not_awaited()


# ──────────────────────────────────────────────────────────────────────
# (a) persist_and_dispatch_appeal — ack до relay, relay в фоне
# ──────────────────────────────────────────────────────────────────────


def _make_user(max_user_id: int = 7):
    """Житель в состоянии воронки (не IDLE) с накопленными данными."""
    return SimpleNamespace(
        id=1,
        max_user_id=max_user_id,
        dialog_state=DialogState.AWAITING_SUMMARY.value,
        dialog_data={
            "summary_chunks": ["Яма во дворе"],
            "attachments": [{"type": "image", "payload": {"token": "T"}}],
            "topic": "Дороги",
            "address": "ул. Ленина, 5",
            "locality": "Елизовское ГП",
        },
    )


def _patched_runtime(mod, *, user, order: list[str]):
    """Собрать набор патчей для `persist_and_dispatch_appeal`, который
    пишет порядок ключевых шагов в `order`."""
    session = MagicMock()

    @asynccontextmanager
    async def _current_user(max_user_id, *, first_name=None):
        yield session, user

    @asynccontextmanager
    async def _sub_scope():
        yield MagicMock()

    created_appeal = SimpleNamespace(id=555)

    async def _create_appeal(*_a, **_k):
        order.append("commit")  # appeal создан (в рамках транзакции)
        return created_appeal

    async def _render(*_a, **_k):
        order.append("render")
        return "admin-mid-1"

    async def _relay(*_a, **_k):
        order.append("relay")

    scheduled: list = []

    def _spawn(coro, *, name=None):
        order.append("spawn")
        # Верно production-семантике: spawn_background_task ставит корутину
        # на текущий event-loop как Task (fire-and-forget) и СИНХРОННО
        # возвращает Task. Тело relay выполнится позже, на следующем тике —
        # тест дренирует через `await asyncio.sleep(0)`. Это доказывает,
        # что ack уже ушёл к моменту постановки relay в фон.
        import asyncio as _asyncio

        task = _asyncio.ensure_future(coro)
        scheduled.append(task)
        return task

    return {
        "session": session,
        "created_appeal": created_appeal,
        "render": _render,
        "relay": _relay,
        "scheduled": scheduled,
        "patches": [
            patch.object(mod, "current_user", _current_user),
            patch.object(mod, "get_user_lock",
                         MagicMock(return_value=_FakeLock())),
            patch.object(mod, "drop_user_lock", MagicMock()),
            patch.object(mod.appeals_service, "count_recent_for_user",
                         AsyncMock(return_value=0)),
            patch.object(mod.appeals_service, "create_appeal", _create_appeal),
            patch.object(mod.users_service, "reset_state", AsyncMock()),
            patch.object(mod, "session_scope", _sub_scope),
            patch.object(mod.broadcasts_service, "is_subscribed",
                         AsyncMock(return_value=False)),
            patch.object(mod, "spawn_background_task", _spawn),
            patch("aemr_bot.services.admin_card.render", _render),
            patch("aemr_bot.services.admin_relay.relay_attachments_to_admin",
                  _relay),
        ],
    }


class _FakeLock:
    """asyncio.Lock-совместимый async-CM для теста (без реального loop-
    contention)."""

    def locked(self) -> bool:
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class TestPersistAcksBeforeRelay:
    """Главный perf-кейс: житель получает ack ДО relay вложений."""

    @pytest.mark.asyncio
    async def test_ack_sent_before_relay(self) -> None:
        from aemr_bot.handlers import appeal_runtime as mod

        user = _make_user()
        order: list[str] = []
        setup = _patched_runtime(mod, user=user, order=order)

        bot = MagicMock()

        async def _send_message(*_a, **_k):
            order.append("ack")

        bot.send_message = _send_message

        with _apply(setup["patches"]):
            result = await mod.persist_and_dispatch_appeal(bot, user.max_user_id)
            # Дренируем фоновый relay-task (он запланирован на loop в
            # spawn-моке) — иначе его тело не выполнится в рамках теста.
            import asyncio

            await asyncio.gather(*setup["scheduled"])

        assert result is True
        # Ключевая гарантия порядка:
        #  - commit обращения ДО ack (запись в БД раньше подтверждения),
        #  - ack ДО relay (житель не ждёт пересылку вложений).
        assert "commit" in order and "ack" in order and "relay" in order
        assert order.index("commit") < order.index("ack"), (
            f"обращение обязано быть закоммичено до ack, получили {order}"
        )
        assert order.index("ack") < order.index("relay"), (
            f"ack должен уйти жителю ДО relay вложений, получили {order}"
        )

    @pytest.mark.asyncio
    async def test_relay_is_backgrounded(self) -> None:
        """Relay уносится в фон через spawn_background_task — он не висит
        на latency-пути жителя (ack уже отправлен к моменту spawn)."""
        from aemr_bot.handlers import appeal_runtime as mod

        user = _make_user()
        order: list[str] = []
        setup = _patched_runtime(mod, user=user, order=order)

        bot = MagicMock()

        async def _send_message(*_a, **_k):
            order.append("ack")

        bot.send_message = _send_message

        with _apply(setup["patches"]):
            await mod.persist_and_dispatch_appeal(bot, user.max_user_id)
            import asyncio

            await asyncio.gather(*setup["scheduled"])

        # spawn_background_task вызван — relay запланирован фоном, а не
        # awaited синхронно перед ack.
        assert "spawn" in order, "relay вложений должен уйти в фон"
        assert order.index("ack") < order.index("spawn"), (
            f"ack жителю должен предшествовать постановке relay в фон, "
            f"получили {order}"
        )

    @pytest.mark.asyncio
    async def test_admin_card_rendered_synchronously(self) -> None:
        """Карточка оператора рендерится синхронно (основной артефакт
        для служебной группы) — а не теряется при backgrounding relay."""
        from aemr_bot.handlers import appeal_runtime as mod

        user = _make_user()
        order: list[str] = []
        setup = _patched_runtime(mod, user=user, order=order)

        bot = MagicMock()
        bot.send_message = AsyncMock()

        with _apply(setup["patches"]):
            await mod.persist_and_dispatch_appeal(bot, user.max_user_id)
            import asyncio

            await asyncio.gather(*setup["scheduled"])

        assert "render" in order, "админ-карточка обязана быть отрендерена"

    @pytest.mark.asyncio
    async def test_ack_failure_does_not_block_card(self) -> None:
        """Fail-open: если ack жителю упал (Exception в send_message),
        карточка оператора всё равно публикуется, обращение не теряется."""
        from aemr_bot.handlers import appeal_runtime as mod

        user = _make_user()
        order: list[str] = []
        setup = _patched_runtime(mod, user=user, order=order)

        bot = MagicMock()

        async def _send_message(*_a, **_k):
            order.append("ack-fail")
            raise RuntimeError("MAX 5xx (simulated)")

        bot.send_message = _send_message

        with _apply(setup["patches"]):
            result = await mod.persist_and_dispatch_appeal(bot, user.max_user_id)
            import asyncio

            await asyncio.gather(*setup["scheduled"])

        # Не упало наружу, обращение «успешно» (доставлено оператору),
        # карточка отрендерена несмотря на сбой ack.
        assert result is True
        assert "render" in order
        assert "spawn" in order


class TestPersistGuardsUnchanged:
    """Регресс-гарды: ранние ветки (IDLE / rate-limit / пустое) НЕ
    шлют ack и НЕ создают обращение — фикс их не задел."""

    @pytest.mark.asyncio
    async def test_idle_state_returns_none_no_ack(self) -> None:
        from aemr_bot.handlers import appeal_runtime as mod

        user = _make_user()
        user.dialog_state = DialogState.IDLE.value
        order: list[str] = []
        setup = _patched_runtime(mod, user=user, order=order)

        bot = MagicMock()
        bot.send_message = AsyncMock()

        with _apply(setup["patches"]):
            result = await mod.persist_and_dispatch_appeal(bot, user.max_user_id)

        assert result is None
        bot.send_message.assert_not_awaited()
        assert order == []

    @pytest.mark.asyncio
    async def test_empty_appeal_returns_false_no_ack(self) -> None:
        from aemr_bot.handlers import appeal_runtime as mod

        user = _make_user()
        user.dialog_data = {"summary_chunks": [], "attachments": []}
        order: list[str] = []
        setup = _patched_runtime(mod, user=user, order=order)

        bot = MagicMock()
        bot.send_message = AsyncMock()

        with _apply(setup["patches"]):
            result = await mod.persist_and_dispatch_appeal(bot, user.max_user_id)

        assert result is False
        bot.send_message.assert_not_awaited()
        assert "render" not in order and "ack" not in order


# ──────────────────────────────────────────────────────────────────────
# Утилита: применить список патчей как один контекст
# ──────────────────────────────────────────────────────────────────────


class _apply:
    """Контекст-менеджер: входит во все переданные patch-объекты и
    выходит из них в обратном порядке. Заменяет глубокую лесенку
    `with a, b, c, ...`."""

    def __init__(self, patches) -> None:
        self._patches = list(patches)
        self._entered: list = []

    def __enter__(self):
        for p in self._patches:
            p.__enter__()
            self._entered.append(p)
        return self

    def __exit__(self, *exc):
        for p in reversed(self._entered):
            p.__exit__(*exc)
        return False
