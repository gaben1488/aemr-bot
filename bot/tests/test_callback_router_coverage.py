"""Регресс-страховка синхронности callback_router и реальных handler'ов.

callback_router.py — реестр payload-маршрутов. Сейчас он используется
в handlers/appeal.py как классификатор (`is_admin_callback` решает,
пропускать ли callback в админ-группе) и парсер (`parse_int_tail`),
но НЕ как dispatch-таблица — фактический dispatch всё ещё if-elif.

Пока две вещи живут параллельно (реестр + if-elif), они могут
разъехаться:
  - запись в реестре без обработчика → мёртвая строка, путает чтение;
  - обработчик без записи в реестре → `is_admin_callback` вернёт
    False для admin-кнопки, и она молча не сработает в служебной
    группе (callback просто ack'нется).

Эти тесты ловят оба расхождения по факту исходного кода, без
хардкода списков. Когда dispatch переедет на `route_for()` целиком
(отдельный рефакторинг с TDD), часть проверок станет избыточной —
но до тех пор это единственная страховка консистентности.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("maxapi", reason="callback_router тянет handlers-цепочку")

from aemr_bot.handlers import callback_router  # noqa: E402

# Файлы, где реально обрабатываются callback-payload'ы.
_HANDLER_DIR = Path(__file__).resolve().parents[1] / "aemr_bot" / "handlers"
_HANDLER_FILES = ("appeal.py", "menu.py", "broadcast.py", "admin_commands.py")


def _handler_source() -> str:
    """Склеенный исходник всех callback-обрабатывающих handler'ов."""
    chunks = []
    for name in _HANDLER_FILES:
        path = _HANDLER_DIR / name
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


class TestNoDeadRoutes:
    """Каждый pattern из реестра должен реально упоминаться в коде
    handler'ов — иначе это мёртвая запись."""

    def test_every_exact_route_referenced_in_handlers(self) -> None:
        src = _handler_source()
        dead = [
            route.pattern
            for route in callback_router.EXACT_ROUTES
            if f'"{route.pattern}"' not in src
            and f"'{route.pattern}'" not in src
        ]
        assert not dead, (
            f"EXACT_ROUTES содержит pattern'ы, которых нет ни в одном "
            f"handler-файле — мёртвые записи реестра: {dead}"
        )

    def test_every_prefix_route_referenced_in_handlers(self) -> None:
        src = _handler_source()
        dead = [
            route.pattern
            for route in callback_router.PREFIX_ROUTES
            if f'"{route.pattern}' not in src
            and f"'{route.pattern}" not in src
        ]
        assert not dead, (
            f"PREFIX_ROUTES содержит pattern'ы, которых нет ни в одном "
            f"handler-файле — мёртвые записи реестра: {dead}"
        )


class TestAdminCallbackClassification:
    """is_admin_callback — контракт чат-контекста: admin-кнопки
    разрешены в служебной группе, жительские — нет."""

    @pytest.mark.parametrize(
        "payload",
        [
            "op:menu", "op:diag", "op:backup", "op:broadcast",
            "op:aud:block:42", "op:reply:7", "op:reopen:5", "op:setkey:topics",
            "broadcast:confirm", "broadcast:abort", "broadcast:stop:3",
        ],
    )
    def test_admin_payloads_allowed_in_admin_chat(self, payload: str) -> None:
        assert callback_router.is_admin_callback(payload) is True

    @pytest.mark.parametrize(
        "payload",
        [
            "menu:new_appeal", "consent:yes", "consent:no", "cancel",
            "addr:reuse", "addr:new", "locality:0", "topic:2",
            "geo:confirm", "appeal:submit", "broadcast:unsubscribe",
        ],
    )
    def test_citizen_payloads_not_admin(self, payload: str) -> None:
        # Жительские payload'ы (включая broadcast:unsubscribe — это
        # кнопка под рассылкой у жителя, не админ-действие) не должны
        # классифицироваться как admin.
        assert callback_router.is_admin_callback(payload) is False

    def test_unknown_payload_is_not_admin(self) -> None:
        # Незнакомый payload → MENU_FALLBACK → не admin. Безопасный
        # дефолт: чужое в админ-группе не выполняется.
        assert callback_router.is_admin_callback("totally:unknown") is False
