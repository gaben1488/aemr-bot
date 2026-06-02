"""P2 resource-exhaustion guards: per-user throttle, bounded polling
dispatch, bounded startup recovery.

Кластер «P2-1 rate-limit/семафор + P2-2 bounded recovery». Эти тесты
доказывают, что:

P2-1 (main.py):
  * Токен-бакет `_UserThrottle` режет бёрст ОДНОГО max_user_id: после
    исчерпания capacity дальнейшие события отклоняются, а по времени
    бакет восстанавливается (refill).
  * `_throttle_allows_event` пропускает админ-группу (операторов не
    троттлим) и события без user_id (lifecycle / fail-open), но гасит
    флуд конкретного жителя.
  * Обёртка `dp.handle` (`_install_dispatch_guards`) в polling-режиме
    не пускает в обработку больше N одновременных handle()-тасков —
    bounded-семафор симметричен webhook-семафору.

P2-2 (handlers/appeal_runtime.py):
  * `recover_stuck_funnels` не запускает больше `_RECOVERY_CONCURRENCY`
    одновременных `persist_and_dispatch_appeal` даже когда застрявших
    воронок намного больше.

Все тесты чистые: без Postgres, БД-слой замокан.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from tests._helpers import make_callback_event, make_event

pytest.importorskip("maxapi", reason="maxapi нужен для main/dispatcher")

# asyncio_mode=auto (pyproject) сам метит корутинные тесты — глобальный
# pytestmark=asyncio избыточен и навешивался бы на синхронные токен-бакет
# тесты (_UserThrottle.allow синхронна), давая PytestWarning. Async-тесты
# ниже остаются async и собираются автоматически.


# ---------------------------------------------------------------------------
# P2-1: per-user токен-бакет
# ---------------------------------------------------------------------------


def test_token_bucket_cuts_single_user_burst() -> None:
    """Бёрст одного user_id гасится после capacity, затем refill восстанавливает."""
    from aemr_bot.main import _UserThrottle

    cap = 5.0
    refill = 2.0  # ток/сек
    bucket = _UserThrottle(cap, refill)
    uid = 4242

    # При t=0 ровно `cap` событий проходят, дальше — отказ (бакет пуст).
    allowed = [bucket.allow(uid, now=0.0) for _ in range(int(cap))]
    assert all(allowed), "первые capacity событий должны пройти"

    # Следующее — в тот же момент времени, токенов нет.
    assert bucket.allow(uid, now=0.0) is False, (
        "бёрст сверх capacity в тот же момент обязан быть отклонён"
    )
    # И ещё несколько подряд — тоже отказ (флуд не пробивает бакет).
    assert not any(bucket.allow(uid, now=0.0) for _ in range(10))

    # Спустя 1 секунду капнуло refill токенов → ровно столько и проходит.
    assert bucket.allow(uid, now=1.0) is True
    assert bucket.allow(uid, now=1.0) is True
    assert bucket.allow(uid, now=1.0) is False, (
        "за 1с восстанавливается ровно refill токенов, не больше"
    )


def test_token_bucket_is_per_user_independent() -> None:
    """Флуд одного user_id не отбирает токены у другого."""
    from aemr_bot.main import _UserThrottle

    bucket = _UserThrottle(3.0, 1.0)
    flooder, victim = 1, 2

    # Опустошаем бакет флудера.
    for _ in range(3):
        assert bucket.allow(flooder, now=0.0) is True
    assert bucket.allow(flooder, now=0.0) is False

    # Жертва в тот же момент времени всё ещё с полным бакетом.
    for _ in range(3):
        assert bucket.allow(victim, now=0.0) is True


def test_token_bucket_gc_drops_idle_users() -> None:
    """gc() выкидывает давно неактивные бакеты, словарь не растёт вечно."""
    from aemr_bot.main import _UserThrottle

    bucket = _UserThrottle(2.0, 1.0)
    bucket.allow(111, now=0.0)
    bucket.allow(222, now=0.0)
    assert len(bucket._buckets) == 2

    # 111 активен недавно, 222 — давно; ttl=10с, now=20с.
    bucket.allow(111, now=19.0)
    bucket.gc(now=20.0, ttl=10.0)
    assert 111 in bucket._buckets
    assert 222 not in bucket._buckets


async def test_throttle_event_exempts_admin_group(monkeypatch) -> None:
    """События из админ-группы НЕ троттлятся даже при флуде одного оператора."""
    import aemr_bot.main as main_mod
    from aemr_bot.config import settings

    # Свежий бакет с крошечной capacity — чтобы доказать, что exempt
    # обходит его, а не просто «не успел исчерпаться».
    monkeypatch.setattr(main_mod, "_user_throttle", main_mod._UserThrottle(1.0, 0.0))

    admin_id = settings.admin_group_id
    assert admin_id is not None, "conftest ставит ADMIN_GROUP_ID=123"

    # 50 событий одного оператора из админ-чата — все проходят.
    for _ in range(50):
        ev = make_callback_event(chat_id=admin_id, user_id=999, payload="op:menu")
        assert main_mod._throttle_allows_event(ev) is True


async def test_throttle_event_passes_events_without_user_id(monkeypatch) -> None:
    """Безатрибутное событие (нет user_id) пропускается — fail-open."""
    import aemr_bot.main as main_mod

    monkeypatch.setattr(main_mod, "_user_throttle", main_mod._UserThrottle(1.0, 0.0))

    # Событие без message/callback/user — get_user_id вернёт None.
    ev = SimpleNamespace(bot=None)
    for _ in range(10):
        assert main_mod._throttle_allows_event(ev) is True


async def test_throttle_event_cuts_single_citizen_flood(monkeypatch) -> None:
    """Флуд ОДНОГО жителя в личке гасится после исчерпания бакета."""
    import aemr_bot.main as main_mod

    # Capacity 8, refill 0 — внутри одного теста время loop'а почти не
    # двигается, поэтому restock пренебрежимо мал; так бёрст детерминирован.
    monkeypatch.setattr(
        main_mod, "_user_throttle", main_mod._UserThrottle(8.0, 0.0)
    )

    citizen_chat = 555  # не равен admin_group_id (123)
    citizen_uid = 70001

    results = [
        main_mod._throttle_allows_event(
            make_event(chat_id=citizen_chat, user_id=citizen_uid, text="спам")
        )
        for _ in range(40)
    ]

    accepted = sum(1 for r in results if r)
    rejected = sum(1 for r in results if not r)
    # Принять должны примерно capacity (±refill за время теста), а
    # подавляющее большинство флуда — отклонить.
    assert accepted <= 10, f"бакет пропустил слишком много: {accepted}"
    assert rejected >= 25, f"флуд почти не порезан: rejected={rejected}"


async def test_throttled_callback_gets_silent_ack() -> None:
    """Затроттленный callback тихо ack'ается (спиннер гаснет), без ответа."""
    import aemr_bot.main as main_mod

    acked = {"n": 0}

    async def _ack(notification=None):
        acked["n"] += 1

    ev = make_callback_event(chat_id=555, user_id=7, payload="menu:main")
    ev.ack = _ack  # type: ignore[attr-defined]

    await main_mod._ack_throttled_callback(ev)
    assert acked["n"] == 1, "callback должен быть подтверждён ровно один раз"

    # Не-callback (обычный текст) — ack-метода нет, обёртка не должна падать.
    plain = make_event(chat_id=555, user_id=7, text="hi")
    await main_mod._ack_throttled_callback(plain)  # без исключений


# ---------------------------------------------------------------------------
# P2-1: bounded polling-dispatch семафор вокруг dp.handle
# ---------------------------------------------------------------------------


async def test_polling_dispatch_semaphore_bounds_concurrency(monkeypatch) -> None:
    """`_install_dispatch_guards` в polling-режиме не пускает в обработку
    больше N одновременных handle()-вызовов."""
    import aemr_bot.main as main_mod
    from aemr_bot.config import settings
    from maxapi import Dispatcher

    assert settings.bot_mode == "polling", "тест рассчитан на polling-дефолт"

    # Маленький bound, чтобы тест был быстрым и явно «кусал».
    bound = 3
    sem = asyncio.Semaphore(bound)
    monkeypatch.setattr(main_mod, "_get_polling_dispatch_semaphore", lambda: sem)
    # Троттлинг в этом тесте не мешаем измерению семафора: даём огромный
    # бакет, чтобы все события прошли по разным user_id.
    monkeypatch.setattr(
        main_mod, "_user_throttle", main_mod._UserThrottle(10_000.0, 0.0)
    )

    state = {"cur": 0, "peak": 0}

    async def fake_handle(event_object, *args, **kwargs):
        state["cur"] += 1
        state["peak"] = max(state["peak"], state["cur"])
        # Держим «обработку» открытой, чтобы заставить корутины перекрыться.
        await asyncio.sleep(0.02)
        state["cur"] -= 1

    dp = Dispatcher(use_create_task=True)
    # Подменяем базовый handle ДО установки guard'ов — обёртка захватит его.
    dp.handle = fake_handle  # type: ignore[method-assign]
    main_mod._install_dispatch_guards(dp)

    # Уникальные user_id, чтобы троттл не вмешался; 30 параллельных событий
    # против семафора 3.
    events = [make_event(chat_id=555, user_id=1000 + i, text="x") for i in range(30)]
    await asyncio.gather(*(dp.handle(ev) for ev in events))

    assert state["peak"] <= bound, (
        f"одновременных handle() было {state['peak']}, лимит семафора {bound} — "
        f"polling-dispatch не ограничен"
    )
    assert state["peak"] >= 2, (
        "перекрытия не случилось — тест не доказывает работу семафора"
    )


async def test_throttle_blocks_before_dispatch(monkeypatch) -> None:
    """Затроттленное событие НЕ доходит до базового dp.handle."""
    import aemr_bot.main as main_mod
    from maxapi import Dispatcher

    # Бакет на 2 события, без refill.
    monkeypatch.setattr(
        main_mod, "_user_throttle", main_mod._UserThrottle(2.0, 0.0)
    )

    handled = {"n": 0}

    async def fake_handle(event_object, *args, **kwargs):
        handled["n"] += 1

    dp = Dispatcher(use_create_task=True)
    dp.handle = fake_handle  # type: ignore[method-assign]
    main_mod._install_dispatch_guards(dp)

    uid = 80002
    # 6 событий одного жителя; пройти до базового handle должны только 2.
    for _ in range(6):
        await dp.handle(make_event(chat_id=555, user_id=uid, text="flood"))

    assert handled["n"] == 2, (
        f"до dispatch'а дошло {handled['n']} событий, ожидалось 2 (capacity) — "
        f"троттл не отсекает на входе"
    )


# ---------------------------------------------------------------------------
# P2-2: bounded recovery в recover_stuck_funnels
# ---------------------------------------------------------------------------


async def test_recover_stuck_funnels_bounds_concurrency(monkeypatch) -> None:
    """recover_stuck_funnels не превышает _RECOVERY_CONCURRENCY одновременных
    корутин персиста, сколько бы воронок ни застряло."""
    from aemr_bot.handlers import appeal_runtime

    n_stuck = 50
    ids = list(range(1, n_stuck + 1))

    # find_stuck_in_summary читается через session_scope → users_service.
    async def fake_find(session, idle_seconds):
        return list(ids)

    @asynccontextmanager
    async def fake_scope():
        yield SimpleNamespace()

    monkeypatch.setattr(
        appeal_runtime.users_service, "find_stuck_in_summary", fake_find
    )
    monkeypatch.setattr(appeal_runtime, "session_scope", fake_scope)

    state = {"cur": 0, "peak": 0}

    async def fake_persist(bot, uid):
        state["cur"] += 1
        state["peak"] = max(state["peak"], state["cur"])
        await asyncio.sleep(0.01)  # форсируем перекрытие
        state["cur"] -= 1
        return True  # «финализировано» — нет empty_ids, нет reset-прохода

    monkeypatch.setattr(appeal_runtime, "persist_and_dispatch_appeal", fake_persist)

    finalized = await appeal_runtime.recover_stuck_funnels(bot=SimpleNamespace())

    assert finalized == n_stuck, "все воронки должны отметиться финализированными"
    assert state["peak"] <= appeal_runtime._RECOVERY_CONCURRENCY, (
        f"пиковая конкуренция {state['peak']} превысила лимит "
        f"{appeal_runtime._RECOVERY_CONCURRENCY} — fan-out не ограничен"
    )
    assert state["peak"] >= 2, (
        "перекрытия не было — тест не доказывает ограничение"
    )


async def test_recover_preserves_result_order_with_bound(monkeypatch) -> None:
    """Bounded gather сохраняет соответствие ids↔results: empty (False)
    воронки корректно вычисляются и сбрасываются в IDLE."""
    from aemr_bot.handlers import appeal_runtime

    ids = [10, 11, 12, 13]
    # 11 и 13 — «пустые» (вернут False), должны попасть в reset_state.
    empty = {11, 13}

    async def fake_find(session, idle_seconds):
        return list(ids)

    reset_calls: list[int] = []

    fake_session = SimpleNamespace()

    @asynccontextmanager
    async def fake_scope():
        yield fake_session

    async def fake_persist(bot, uid):
        return uid not in empty  # False для пустых, True для остальных

    async def fake_reset(session, uid):
        reset_calls.append(uid)

    monkeypatch.setattr(
        appeal_runtime.users_service, "find_stuck_in_summary", fake_find
    )
    monkeypatch.setattr(
        appeal_runtime.users_service, "reset_state", fake_reset
    )
    monkeypatch.setattr(appeal_runtime, "session_scope", fake_scope)
    monkeypatch.setattr(appeal_runtime, "persist_and_dispatch_appeal", fake_persist)

    finalized = await appeal_runtime.recover_stuck_funnels(bot=SimpleNamespace())

    assert finalized == 2, "финализировано должно быть 2 непустых воронки"
    assert sorted(reset_calls) == sorted(empty), (
        f"в IDLE сброшены {sorted(reset_calls)}, ожидались пустые {sorted(empty)} — "
        f"порядок results разъехался с ids"
    )
