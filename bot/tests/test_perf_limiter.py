"""Perf-кластер «Глобальный лимитер исходящих 2 RPS».

Проблема (находка): процесс-глобального троттла исходящих не было.
Единственным ограничителем был локальный `asyncio.sleep(rate_delay)`
в ОДНОЙ рассылке (handlers/broadcast). Рассылка (~1 RPS) + ответы
операторов + новые карточки + cron-уведомления (pulse, funnel-watchdog,
retention-алёрты) шлются НЕЗАВИСИМО и СКЛАДЫВАЮТСЯ. Их сумма пробивает
исходящий лимит MAX (~2 RPS) → 429 → часть рассылки (включая оповещение
о ЧС) теряется, потому что `_send_one` сдаётся после 3 ретраев.

Фикс (`services/admin_bus`): один процесс-глобальный async token-bucket
(`_GlobalOutgoingLimiter`, ~1.5 msg/s, burst 3), через который проходят
ВСЕ `bot.send_message` / `bot.edit_message`. Навеска — внутри
`install_outgoing_tracker_hook` (он уже оборачивает send_message):
`await _acquire_outgoing_slot()` ПЕРЕД фактической отправкой. Рассылка и
интерактив делят ОДИН бюджет, а не суммируются.

Эти тесты — защита от регрессии и доказательство контракта:
1. burst до capacity проходит без задержки, дальше троттл по времени;
2. при низкой нагрузке (есть токен) лимитер НЕ спит — latency ~0;
3. FAIL-OPEN: если внутри лимитера падает исключение — отправка всё
   равно идёт (acquire не бросает);
4. интеграция: и `bot.send_message`, и `bot.edit_message` после
   install'а реально проходят через лимитер (acquire вызывается ровно
   перед каждой отправкой), а исключение самой отправки по-прежнему
   всплывает наружу (поведение tracker-hook сохранено).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from aemr_bot.services import admin_bus


ADMIN_CHAT_ID = 777


class _FakeClock:
    """Управляемые monotonic-часы. `advance(dt)` двигает время вперёд.

    Лимитер читает `admin_bus.time.monotonic`; в тестах подменяем его на
    `clock.now`, чтобы token-bucket-математика была детерминированной без
    реального ожидания.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    c = _FakeClock()
    monkeypatch.setattr(admin_bus.time, "monotonic", c.now)
    return c


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Каждый тест — свежий лимитер-синглтон, чтобы накопленные токены
    из предыдущего теста не протекали."""
    admin_bus._outgoing_limiter = None
    yield
    admin_bus._outgoing_limiter = None


def _make_limiter(rate: float = 2.0, capacity: float = 3.0):
    return admin_bus._GlobalOutgoingLimiter(rate_per_sec=rate, capacity=capacity)


# ---------------------------------------------------------------------------
# 1. Token-bucket: burst до capacity, затем троттл по времени
# ---------------------------------------------------------------------------


class TestTokenBucketShape:
    @pytest.mark.asyncio
    async def test_burst_up_to_capacity_no_sleep(self, clock, monkeypatch) -> None:
        """Первые `capacity` отправок (бакет полон на старте) идут без сна."""
        slept: list[float] = []

        async def fake_sleep(s):
            slept.append(s)

        monkeypatch.setattr(admin_bus.asyncio, "sleep", fake_sleep)

        lim = _make_limiter(rate=2.0, capacity=3.0)
        # Время не двигаем — пополнения нет, расходуем стартовый бурст.
        for _ in range(3):
            await lim.acquire()

        # Ни одного сна — бурст уложился в стартовые 3 токена.
        assert slept == []

    @pytest.mark.asyncio
    async def test_throttles_after_capacity_exhausted(
        self, clock, monkeypatch
    ) -> None:
        """4-я подряд отправка (без хода времени) обязана подождать
        ~1/rate секунд: бакет пуст, токен ещё не накопился."""
        slept: list[float] = []

        async def fake_sleep(s):
            slept.append(s)
            # Имитируем, что время реально прошло на величину сна —
            # тогда последующее списание токена не зациклится.
            clock.advance(s)

        monkeypatch.setattr(admin_bus.asyncio, "sleep", fake_sleep)

        lim = _make_limiter(rate=2.0, capacity=3.0)
        for _ in range(3):  # выбираем стартовый бурст
            await lim.acquire()
        assert slept == []  # бурст без сна

        await lim.acquire()  # 4-я — бакет пуст
        assert len(slept) == 1
        # rate=2/сек → один токен копится 0.5 сек.
        assert slept[0] == pytest.approx(0.5, abs=1e-6)

    @pytest.mark.asyncio
    async def test_steady_rate_after_refill(self, clock, monkeypatch) -> None:
        """После расхода бурста устойчивый темп ограничен rate: за каждый
        1/rate-интервал освобождается ровно один слот без сна."""
        slept: list[float] = []

        async def fake_sleep(s):
            slept.append(s)
            clock.advance(s)

        monkeypatch.setattr(admin_bus.asyncio, "sleep", fake_sleep)

        lim = _make_limiter(rate=2.0, capacity=3.0)
        for _ in range(3):  # стартовый бурст
            await lim.acquire()

        # Двигаем время на 0.5 сек → ровно 1 токен (rate=2). Следующая
        # отправка должна пройти без сна.
        clock.advance(0.5)
        await lim.acquire()
        assert slept == []

        # Сразу ещё одна — токенов снова нет, ждём ~0.5 сек.
        await lim.acquire()
        assert len(slept) == 1
        assert slept[0] == pytest.approx(0.5, abs=1e-6)

    @pytest.mark.asyncio
    async def test_tokens_capped_at_capacity(self, clock, monkeypatch) -> None:
        """Долгий простой не копит токены сверх capacity: после паузы
        проходит ровно `capacity` мгновенных отправок, не больше."""
        slept: list[float] = []

        async def fake_sleep(s):
            slept.append(s)
            clock.advance(s)

        monkeypatch.setattr(admin_bus.asyncio, "sleep", fake_sleep)

        lim = _make_limiter(rate=2.0, capacity=3.0)
        for _ in range(3):  # опустошаем стартовый бакет
            await lim.acquire()
        slept.clear()

        # Простаиваем 100 сек — токены пополнятся, но не выше capacity (3).
        clock.advance(100.0)
        for _ in range(3):
            await lim.acquire()
        assert slept == []  # три ушли без сна (capacity накоплен)

        # Четвёртая подряд — бакет снова пуст, троттл.
        await lim.acquire()
        assert len(slept) == 1
        assert slept[0] == pytest.approx(0.5, abs=1e-6)


# ---------------------------------------------------------------------------
# 2. Низкая нагрузка — без блокировки (latency ~0)
# ---------------------------------------------------------------------------


class TestLowLoadNoBlock:
    @pytest.mark.asyncio
    async def test_spaced_sends_never_sleep(self, clock, monkeypatch) -> None:
        """Редкие отправки (интервал >= 1/rate) никогда не спят: к каждой
        уже накоплен токен. Это гарантия, что в обычном (не-burst) режиме
        бот не получает искусственной задержки."""
        slept: list[float] = []

        async def fake_sleep(s):
            slept.append(s)
            clock.advance(s)

        monkeypatch.setattr(admin_bus.asyncio, "sleep", fake_sleep)

        lim = _make_limiter(rate=2.0, capacity=3.0)
        # Сбрасываем стартовый бурст, дальше шлём строго раз в 0.6 сек
        # (медленнее, чем 1 токен / 0.5 сек).
        for _ in range(3):
            await lim.acquire()
        slept.clear()

        for _ in range(20):
            clock.advance(0.6)
            await lim.acquire()

        assert slept == []  # ни одной задержки на устойчивом редком темпе


# ---------------------------------------------------------------------------
# 2b. Concurrency: одновременные acquire разносятся по слотам (не стадо)
# ---------------------------------------------------------------------------


class TestConcurrencyReservation:
    """Под use_create_task хендлеры — параллельные Task'и (до 32 через
    dispatch-семафор). Лимитер обязан РАЗНОСИТЬ одновременные acquire по
    слотам (синхронное резервирование), а не пропускать их стадом. Это был
    дефект первой версии: 10 параллельных acquire → ~14.6 msg/s вместо 1.5,
    потому что все считали одинаковый wait и просыпались вместе."""

    def test_reserve_spaces_simultaneous_slots(self, clock: _FakeClock) -> None:
        """N резерваций в ОДИН момент (часы стоят — имитация конкурентных
        acquire до первого await): burst до capacity уходит с wait=0, дальше
        ожидания СТРОГО разносятся по 1/rate (не одинаковые → нет herd)."""
        lim = admin_bus._GlobalOutgoingLimiter(rate_per_sec=1.5, capacity=3.0)
        # Часы не двигаем: все резервации «одновременны».
        waits = [lim._reserve() for _ in range(7)]
        interval = 1.0 / 1.5
        # Первые capacity (3) — burst, без задержки.
        assert waits[:3] == [0.0, 0.0, 0.0]
        # Дальше — строго по слотам, каждое ОТЛИЧАЕТСЯ (стадо ждало бы одно).
        assert waits[3] == pytest.approx(interval, abs=1e-9)
        assert waits[4] == pytest.approx(2 * interval, abs=1e-9)
        assert waits[5] == pytest.approx(3 * interval, abs=1e-9)
        assert waits[6] == pytest.approx(4 * interval, abs=1e-9)
        assert len(set(waits[3:])) == 4  # все ожидания различны


# ---------------------------------------------------------------------------
# 3. Fail-open: внутренняя ошибка лимитера не ломает отправку
# ---------------------------------------------------------------------------


class TestFailOpen:
    @pytest.mark.asyncio
    async def test_acquire_swallows_internal_error(self) -> None:
        """Если внутри `acquire` падает работа с часами — `acquire()` НЕ
        пробрасывает исключение (fail-open): отправка приоритетнее
        идеального темпа. Ломаем источник времени (`monotonic`), который
        читает token-bucket внутри `_reserve`. Сам метод подменить на
        инстансе нельзя из-за __slots__, поэтому бьём по monotonic.

        ВАЖНО: monotonic восстанавливаем в finally ДО выхода из теста —
        НЕ monkeypatch'ем (он откатывает слишком поздно). Иначе teardown
        event-loop'а pytest-asyncio (loop.close → _run_once → self.time()
        → time.monotonic) вызвал бы boom и уронил уже runner, а не лимитер."""
        lim = admin_bus._GlobalOutgoingLimiter(rate_per_sec=2.0, capacity=3.0)

        def boom() -> float:
            raise RuntimeError("clock exploded")

        original = admin_bus.time.monotonic
        admin_bus.time.monotonic = boom
        try:
            # Не должно бросить — иначе обёрнутый send упал бы из-за лимитера.
            await lim.acquire()
        finally:
            admin_bus.time.monotonic = original

    @pytest.mark.asyncio
    async def test_acquire_swallows_sleep_error(self, monkeypatch) -> None:
        """Если падает сам `asyncio.sleep` (например, отмена внутренней
        реализации) — acquire тоже глотает и возвращается."""
        clock = _FakeClock()
        monkeypatch.setattr(admin_bus.time, "monotonic", clock.now)

        async def boom_sleep(s):
            raise RuntimeError("sleep failed")

        monkeypatch.setattr(admin_bus.asyncio, "sleep", boom_sleep)

        lim = _make_limiter(rate=2.0, capacity=1.0)
        await lim.acquire()  # съели единственный токен, без сна
        # Вторая — нужен сон, но он бросит; acquire обязан проглотить.
        await lim.acquire()


# ---------------------------------------------------------------------------
# 4. Интеграция с install_outgoing_tracker_hook
# ---------------------------------------------------------------------------


def _make_bot_with_edit(send_mid: str = "m-1"):
    """Bot c send_message и edit_message как AsyncMock'ами."""
    return SimpleNamespace(
        send_message=AsyncMock(
            return_value=SimpleNamespace(
                message=SimpleNamespace(body=SimpleNamespace(mid=send_mid))
            )
        ),
        edit_message=AsyncMock(
            return_value=SimpleNamespace(
                message=SimpleNamespace(body=SimpleNamespace(mid=send_mid))
            )
        ),
    )


class TestHookRoutesThroughLimiter:
    @pytest.mark.asyncio
    async def test_send_message_acquires_before_send(self) -> None:
        """Установленный hook гонит каждый send_message через
        `_acquire_outgoing_slot` ровно ПЕРЕД фактической отправкой."""
        order: list[str] = []

        async def spy_acquire():
            order.append("acquire")

        async def spy_send(*a, **k):
            order.append("send")
            return SimpleNamespace(
                message=SimpleNamespace(body=SimpleNamespace(mid="m-1"))
            )

        bot = SimpleNamespace(send_message=AsyncMock(side_effect=spy_send))

        with patch("aemr_bot.config.settings.admin_group_id", ADMIN_CHAT_ID):
            admin_bus.install_outgoing_tracker_hook(bot)
            with patch.object(admin_bus, "_acquire_outgoing_slot", spy_acquire):
                await bot.send_message(chat_id=ADMIN_CHAT_ID, text="hi")

        # acquire строго ДО фактической отправки.
        assert order == ["acquire", "send"]

    @pytest.mark.asyncio
    async def test_edit_message_routed_through_limiter(self) -> None:
        """edit_message тоже считается в лимит MAX → hook оборачивает и
        его: acquire вызывается перед фактическим edit."""
        bot = _make_bot_with_edit()
        order: list[str] = []

        async def spy_acquire():
            order.append("acquire")

        async def spy_edit(*a, **k):
            order.append("edit")
            return SimpleNamespace(
                message=SimpleNamespace(body=SimpleNamespace(mid="m-1"))
            )

        bot.edit_message = AsyncMock(side_effect=spy_edit)

        with patch("aemr_bot.config.settings.admin_group_id", ADMIN_CHAT_ID):
            admin_bus.install_outgoing_tracker_hook(bot)
            with patch.object(admin_bus, "_acquire_outgoing_slot", spy_acquire):
                await bot.edit_message(message_id="m-1", text="edited")

        assert order == ["acquire", "edit"]

    @pytest.mark.asyncio
    async def test_real_limiter_throttles_burst_through_hook(
        self, clock, monkeypatch
    ) -> None:
        """End-to-end: с НАСТОЯЩИМ лимитером пакет sends через hook
        упирается в темп — после исчерпания бурста появляется сон.

        Это и есть фикс: рассылка + интерактив, идущие подряд через ОДИН
        bot, делят общий бюджет, а не суммируются."""
        slept: list[float] = []

        async def fake_sleep(s):
            slept.append(s)
            clock.advance(s)

        monkeypatch.setattr(admin_bus.asyncio, "sleep", fake_sleep)

        bot = _make_bot_with_edit()
        with patch("aemr_bot.config.settings.admin_group_id", ADMIN_CHAT_ID):
            admin_bus.install_outgoing_tracker_hook(bot)
            # _OUTGOING_BURST штук уходят без сна, дальше троттл.
            burst = int(admin_bus._OUTGOING_BURST)
            for _ in range(burst):
                await bot.send_message(chat_id=ADMIN_CHAT_ID, text="x")
            assert slept == []
            # Следующая — бакет пуст, должен появиться ровно один сон.
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text="x")
        assert len(slept) == 1
        assert slept[0] == pytest.approx(
            1.0 / admin_bus._OUTGOING_RATE_PER_SEC, abs=1e-6
        )

    @pytest.mark.asyncio
    async def test_send_failure_still_propagates(self) -> None:
        """Лимитер fail-open и стоит ПЕРЕД отправкой, поэтому исключение
        самой send_message по-прежнему всплывает наружу (контракт
        tracker-hook не сломан)."""
        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=RuntimeError("MAX 500")),
        )
        with patch("aemr_bot.config.settings.admin_group_id", ADMIN_CHAT_ID):
            admin_bus.install_outgoing_tracker_hook(bot)
            with pytest.raises(RuntimeError, match="MAX 500"):
                await bot.send_message(chat_id=ADMIN_CHAT_ID, text="x")
