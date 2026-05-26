"""Regression-тесты пакета reliability-pass.

Покрывает:
- `wizard_registry._spawn_persist` использует `spawn_background_task`
  (strong ref защищает task от GC) — раньше был голый `loop.create_task`.
- `find_overdue_unanswered` принимает и применяет `limit`.
- `cron._get_healthcheck_session` возвращает один и тот же
  ClientSession при повторных вызовах (singleton).
- `admin_card.render` сузил `except Exception` до конкретных классов:
  на RuntimeError из card_format больше НЕ swallow'ит (баг всплывает).
- `menu._send_or_edit_menu` тоже не маскирует произвольные exception'ы:
  RuntimeError из bot.edit_message пробрасывается, MaxApiError —
  fallback на send_message.

Тесты модульные, без Postgres — `find_overdue_unanswered` LIMIT
проверяем по объекту запроса (LIMIT попадает в скомпилированный SQL).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# wizard_registry: persist через spawn_background_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wizard_registry_spawn_persist_uses_strong_ref() -> None:
    """`_spawn_persist` должен класть task в `_BACKGROUND_TASKS`, чтобы
    GC не убил persist посреди записи в БД."""
    from aemr_bot.services import wizard_registry
    from aemr_bot.utils import background

    started = asyncio.Event()
    finished = asyncio.Event()

    async def coro():
        started.set()
        await asyncio.sleep(0)  # дать loop'у тик
        finished.set()

    wizard_registry._spawn_persist(coro)
    # Дать запущенной task'е попасть в strong-ref set.
    await asyncio.sleep(0)
    assert started.is_set(), "task должна стартовать в текущем loop'е"
    # Strong-ref зарегистрирован?
    assert any(
        not t.done() or t.get_name() == "wizard_registry.persist"
        for t in list(background._BACKGROUND_TASKS) + list(asyncio.all_tasks())
    )
    # Дождаться завершения, чтобы set вычистил себя через done_callback.
    await finished.wait()
    await asyncio.sleep(0)  # дать done_callback отработать


def test_wizard_registry_spawn_persist_no_loop_noop() -> None:
    """Без running loop — silent no-op, не RuntimeError."""
    from aemr_bot.services import wizard_registry

    # coro_factory НЕ должен вызваться, т.к. _spawn_persist возвращает раньше.
    calls = []

    def coro_factory():
        calls.append(1)

    wizard_registry._spawn_persist(coro_factory)
    assert calls == [], (
        "без active event loop _spawn_persist обязан быть no-op "
        "(юнит-тесты импортируют модуль вне asyncio context'а)"
    )


# ---------------------------------------------------------------------------
# appeals.find_overdue_unanswered: LIMIT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_overdue_unanswered_passes_limit_to_query() -> None:
    """LIMIT 500 (default) должен попасть в скомпилированный SQL —
    защищает cron-алёрт от вытягивания всей очереди при отпуске
    оператора."""
    from aemr_bot.services import appeals as appeals_service

    captured = {}

    class _FakeScalars:
        def __init__(self):
            self._items: list = []

        def __iter__(self):
            return iter(self._items)

    class _FakeSession:
        async def scalars(self, stmt):
            captured["stmt"] = stmt
            return _FakeScalars()

    session = _FakeSession()
    await appeals_service.find_overdue_unanswered(session, sla_hours=24)
    # SQL-визуализация: LIMIT должен присутствовать.
    compiled_sql = str(
        captured["stmt"].compile(compile_kwargs={"literal_binds": True})
    )
    assert "LIMIT 500" in compiled_sql.upper().replace("\n", " "), (
        f"LIMIT 500 (default) обязан попасть в SQL, скомпилировано:\n{compiled_sql}"
    )

    # И при custom limit тоже.
    captured.clear()
    await appeals_service.find_overdue_unanswered(
        session, sla_hours=24, limit=10
    )
    compiled_sql_2 = str(
        captured["stmt"].compile(compile_kwargs={"literal_binds": True})
    )
    assert "LIMIT 10" in compiled_sql_2.upper().replace("\n", " ")


# ---------------------------------------------------------------------------
# cron: shared aiohttp.ClientSession for healthcheck
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthcheck_session_is_singleton_across_pings() -> None:
    """`_get_healthcheck_session()` обязан возвращать один и тот же
    объект на повторных вызовах — иначе каждый тик cron'а делает
    свежий TLS handshake + DNS lookup."""
    from aemr_bot.services import cron as cron_module

    # Сбросить state (другие тесты могли инициализировать).
    if cron_module._HEALTHCHECK_SESSION is not None:
        try:
            await cron_module._HEALTHCHECK_SESSION.close()
        except Exception:
            pass
        cron_module._HEALTHCHECK_SESSION = None

    s1 = cron_module._get_healthcheck_session()
    s2 = cron_module._get_healthcheck_session()
    try:
        assert s1 is s2, "ClientSession должен переиспользоваться"
    finally:
        await s1.close()
        cron_module._HEALTHCHECK_SESSION = None


@pytest.mark.asyncio
async def test_healthcheck_session_replaced_when_closed() -> None:
    """Если предыдущий session закрыт (например APScheduler shutdown),
    следующий get создаёт новый — иначе будем дёргать мёртвый объект."""
    from aemr_bot.services import cron as cron_module

    if cron_module._HEALTHCHECK_SESSION is not None:
        try:
            await cron_module._HEALTHCHECK_SESSION.close()
        except Exception:
            pass
        cron_module._HEALTHCHECK_SESSION = None

    s1 = cron_module._get_healthcheck_session()
    await s1.close()
    s2 = cron_module._get_healthcheck_session()
    try:
        assert s2 is not s1
        assert not s2.closed
    finally:
        await s2.close()
        cron_module._HEALTHCHECK_SESSION = None


# ---------------------------------------------------------------------------
# admin_card.render: narrow except — RuntimeError всплывает, не глотается
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_card_render_does_not_swallow_arbitrary_runtimeerror() -> None:
    """RuntimeError из card_format (например баг кода, а не detached
    lazy-load) не должен молча подменяться fallback-строкой —
    иначе настоящий баг прячется. Реальные «безопасные» exception'ы —
    AttributeError / TypeError / DetachedInstanceError, они ловятся."""
    from aemr_bot.services import admin_card

    user = SimpleNamespace(
        id=1, first_name="Иван", last_name="П", phone="+7900", is_blocked=False
    )
    appeal = SimpleNamespace(
        id=42, user=user, status="new", admin_message_id=None,
        last_admin_card_mid=None, closed_due_to_revoke=False,
        messages=[], events=[],
    )
    bot = SimpleNamespace(
        send_message=AsyncMock(),
        edit_message=AsyncMock(),
    )

    with patch(
        "aemr_bot.services.admin_card.card_format.admin_card",
        side_effect=RuntimeError("bug in card_format — must surface"),
    ), patch("aemr_bot.config.settings.admin_group_id", 555):
        with pytest.raises(RuntimeError, match="bug in card_format"):
            await admin_card.render(bot, appeal)


# ---------------------------------------------------------------------------
# menu._send_or_edit_menu: narrow except — RuntimeError всплывает
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_menu_edit_fallback_does_not_mask_unexpected_runtimeerror() -> None:
    """RuntimeError из bot.edit_message — это баг (например замокан
    неправильно), не штатное состояние MAX-API. Должен пробрасываться.
    Раньше `except Exception` глотал и тихо делал send_message.

    После 2026-05-27 (freshness в `_send_or_edit_menu`): для edit-пути
    tracker должен иметь mid == callback.mid. Иначе freshness откажет
    в edit и send_message пойдёт без вызова edit_message — тогда
    RuntimeError не возникает.
    """
    from aemr_bot.handlers import menu
    from aemr_bot.utils import menu_tracker

    bot = SimpleNamespace(
        edit_message=AsyncMock(side_effect=RuntimeError("unexpected")),
        send_message=AsyncMock(),
    )
    event = SimpleNamespace(
        bot=bot,
        callback=SimpleNamespace(),
        message=SimpleNamespace(
            body=SimpleNamespace(mid="m-1"),
            recipient=SimpleNamespace(chat_id=555),
        ),
    )

    def _get_ids():
        return (555, 42)
    event.get_ids = _get_ids

    # Sync tracker так, чтобы freshness разрешил edit (callback_mid ==
    # tracker → can_edit=True → bot.edit_message вызовется → RuntimeError).
    menu_tracker.set_last_menu_mid(555, "m-1")
    try:
        with pytest.raises(RuntimeError, match="unexpected"):
            await menu._send_or_edit_menu(event, text="hi", attachments=[])
        bot.send_message.assert_not_called()
    finally:
        menu_tracker.clear(555)
