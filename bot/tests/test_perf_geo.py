"""Perf-кластер «Geo hot-path: to_thread + typing».

Проблема: `geo_service.find_address` (CPU-bound shapely + холодная
ленивая загрузка 2.6 МБ GeoJSON) исторически вызывался СИНХРОННО прямо
на единственном event-loop'е. При `Dispatcher(use_create_task=True)`
любой синхронный блок в одном handler'е морозит ВСЕХ жителей и
операторов — первая геолокация после рестарта подвешивала бота на
~0.3–0.5 с.

Фикс в `handlers/appeal_geo.handle_location_for_locality`:
1. `await asyncio.to_thread(geo_service.find_address, lat, lon)` —
   CPU-bound работа уходит в worker-поток, loop остаётся отзывчивым.
   `find_address` thread-safe: только чтение lru_cache-структур, без
   БД/сессии, ловит свои исключения.
2. `await mark_typing(event)` ПЕРЕД геокодингом — житель видит
   индикатор «бот печатает», а не «мёртвый» экран. Best-effort.

Эти тесты — защита от регрессии: доказывают, что (а) геокодинг идёт
через `asyncio.to_thread` (не блокирует loop), (б) `mark_typing`
вызван строго ДО геокодинга, (в) результат не изменился (то же
содержимое подтверждающего экрана и dialog_data).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _make_event() -> SimpleNamespace:
    """MAX-event с минимально достаточной поверхностью для handler'а:
    `.message.answer` (корутина, отдаёт mid) + `.bot`/`.get_ids` для
    `mark_typing`."""
    return SimpleNamespace(
        bot=SimpleNamespace(send_action=AsyncMock()),
        get_ids=lambda: (777, 42),
        message=SimpleNamespace(
            answer=AsyncMock(
                return_value=SimpleNamespace(
                    message=SimpleNamespace(
                        body=SimpleNamespace(mid="m-geo-confirm")
                    )
                )
            )
        ),
    )


def _patch_db():
    """Контекст-менеджеры, заглушающие session_scope + users_service,
    чтобы handler не ходил в БД. Возвращает список patcher'ов."""
    scope = patch("aemr_bot.handlers.appeal_geo.session_scope")
    upd = patch(
        "aemr_bot.handlers.appeal_geo.users_service.update_dialog_data",
        AsyncMock(),
    )
    sst = patch(
        "aemr_bot.handlers.appeal_geo.users_service.set_state", AsyncMock()
    )
    return scope, upd, sst


@pytest.mark.asyncio
async def test_find_address_runs_via_to_thread_not_on_loop() -> None:
    """find_address должен уезжать в `asyncio.to_thread` — иначе CPU-bound
    shapely морозит единственный event-loop (use_create_task=True)."""
    from aemr_bot.handlers import appeal_geo

    event = _make_event()
    geo_result = SimpleNamespace(
        locality="Елизовское ГП",
        street="Ленина",
        house_number="5",
        confidence="high",
    )

    real_to_thread = asyncio.to_thread
    seen: dict = {}

    async def spy_to_thread(func, /, *args, **kwargs):
        # Записываем, ЧТО именно уехало в поток, и форвардим на реальный
        # to_thread — поведение сохраняется, результат настоящий.
        seen["func"] = func
        seen["args"] = args
        return await real_to_thread(func, *args, **kwargs)

    scope, upd, sst = _patch_db()
    with patch("aemr_bot.handlers.appeal_geo.mark_typing", AsyncMock()), \
         patch(
             "aemr_bot.services.geo.find_address", return_value=geo_result
         ) as find_address, \
         patch.object(appeal_geo.asyncio, "to_thread", spy_to_thread), \
         scope as scope_cm, upd, sst:
        scope_cm.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        scope_cm.return_value.__aexit__ = AsyncMock(return_value=None)

        await appeal_geo.handle_location_for_locality(
            event, max_user_id=42, location=(53.184, 158.385)
        )

    # to_thread получил именно geo_service.find_address и координаты.
    assert seen.get("func") is find_address
    assert seen.get("args") == (53.184, 158.385)
    # find_address всё же был выполнен (внутри потока).
    find_address.assert_called_once_with(53.184, 158.385)
    # Подтверждающий экран ушёл — результат потока использован дальше.
    assert event.message.answer.await_count >= 1


@pytest.mark.asyncio
async def test_mark_typing_called_before_geocoding() -> None:
    """Индикатор «печатает» должен сработать ДО геокодинга, иначе житель
    смотрит на «зависший» экран всё время холодной загрузки GeoJSON."""
    from aemr_bot.handlers import appeal_geo

    event = _make_event()
    geo_result = SimpleNamespace(
        locality="Елизовское ГП",
        street="Ленина",
        house_number="5",
        confidence="high",
    )

    order: list[str] = []

    async def rec_mark_typing(*_a, **_k):
        order.append("typing")

    def rec_find_address(*_a, **_k):
        order.append("geocode")
        return geo_result

    scope, upd, sst = _patch_db()
    with patch(
        "aemr_bot.handlers.appeal_geo.mark_typing", rec_mark_typing
    ), patch(
        "aemr_bot.services.geo.find_address", rec_find_address
    ), scope as scope_cm, upd, sst:
        scope_cm.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        scope_cm.return_value.__aexit__ = AsyncMock(return_value=None)

        await appeal_geo.handle_location_for_locality(
            event, max_user_id=42, location=(53.184, 158.385)
        )

    assert order[:2] == ["typing", "geocode"], (
        f"mark_typing должен идти строго перед геокодингом, got {order}"
    )


@pytest.mark.asyncio
async def test_result_preserved_full_address_confirm_screen() -> None:
    """Поведение сохранено: при confidence=high с улицей и домом
    подтверждающий экран и сохранённый detected_* — те же, что и до
    выноса в поток."""
    from aemr_bot import texts
    from aemr_bot.db.models import DialogState
    from aemr_bot.handlers import appeal_geo

    event = _make_event()
    geo_result = SimpleNamespace(
        locality="Елизовское ГП",
        street="Ленина",
        house_number="5",
        confidence="high",
    )
    update_dialog_data = AsyncMock()
    set_state = AsyncMock()

    with patch("aemr_bot.handlers.appeal_geo.mark_typing", AsyncMock()), \
         patch(
             "aemr_bot.services.geo.find_address", return_value=geo_result
         ), \
         patch("aemr_bot.handlers.appeal_geo.session_scope") as scope_cm, \
         patch(
             "aemr_bot.handlers.appeal_geo.users_service.update_dialog_data",
             update_dialog_data,
         ), \
         patch(
             "aemr_bot.handlers.appeal_geo.users_service.set_state", set_state
         ):
        scope_cm.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        scope_cm.return_value.__aexit__ = AsyncMock(return_value=None)

        await appeal_geo.handle_location_for_locality(
            event, max_user_id=42, location=(53.184, 158.385)
        )

    # detected_* записаны как раньше (первый вызов update_dialog_data).
    saved = update_dialog_data.call_args_list[0].args[2]
    assert saved["locality"] == "Елизовское ГП"
    assert saved["detected_street"] == "Ленина"
    assert saved["detected_house_number"] == "5"
    assert saved["detected_confidence"] == "high"
    assert saved["detected_lat"] == 53.184
    assert saved["detected_lon"] == 158.385

    # State перешёл в подтверждение.
    assert set_state.call_args.args[2] == DialogState.AWAITING_GEO_CONFIRM

    # Текст подтверждающего экрана — полный адрес, как и прежде.
    expected_text = texts.GEO_DETECTED_FULL.format(
        locality="Елизовское ГП", address="Ленина, д. 5"
    )
    first_answer_text = event.message.answer.call_args_list[0].args[0]
    assert first_answer_text == expected_text

    # progress_message_id сохранён (последний update_dialog_data).
    assert update_dialog_data.call_args_list[-1].args[2] == {
        "progress_message_id": "m-geo-confirm"
    }


@pytest.mark.asyncio
async def test_outside_emo_short_circuits_without_confirm() -> None:
    """Точка вне ЕМО (locality=None): по-прежнему просим выбрать вручную,
    подтверждающий экран не шлём. Гео всё равно ушло в поток."""
    from aemr_bot.handlers import appeal_geo

    event = _make_event()
    geo_result = SimpleNamespace(
        locality=None, street=None, house_number=None, confidence="none"
    )
    set_state = AsyncMock()

    scope, upd, _ = _patch_db()
    with patch("aemr_bot.handlers.appeal_geo.mark_typing", AsyncMock()), \
         patch(
             "aemr_bot.services.geo.find_address", return_value=geo_result
         ), \
         patch(
             "aemr_bot.handlers.appeal_geo.settings_store.get",
             AsyncMock(return_value=["Елизовское ГП"]),
         ), \
         patch(
             "aemr_bot.handlers.appeal_geo.users_service.set_state", set_state
         ), \
         scope as scope_cm, upd:
        scope_cm.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        scope_cm.return_value.__aexit__ = AsyncMock(return_value=None)

        await appeal_geo.handle_location_for_locality(
            event, max_user_id=42, location=(0.0, 0.0)
        )

    # Никакого перехода в подтверждение — поведение сохранено.
    set_state.assert_not_called()
    event.message.answer.assert_awaited_once()
