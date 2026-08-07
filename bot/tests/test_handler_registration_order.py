"""Порядок регистрации обработчиков: команды раньше «ловца всего».

`appeal.register` ставит `@dp.message_created()` без фильтров — он
принимает ЛЮБОЕ сообщение. Если зарегистрировать его раньше `start`,
все команды жителя (`/start`, `/export`, `/forget`, `/policy`) начнут
проваливаться в воронку обращения: житель наберёт команду, а бот примет
её текст как описание проблемы.

Ломается это молча — ни ошибки, ни лога, и локально не воспроизводится,
если тестировать команды поштучно. Поэтому порядок закреплён тестом:
любая перестановка строк в `register_handlers` покраснеет здесь.

Аудит покрытия по графу (graphify, 2026-08-08) показал, что
`handlers/__init__.py` не сторожил ни один тест.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

pytest.importorskip("maxapi", reason="регистрация требует maxapi")

from aemr_bot.handlers import register_handlers


def _registration_calls() -> list[str]:
    """Имена модулей в порядке вызова `<модуль>.register(dp)`.

    Читаем исходник, а не рантайм: важен именно порядок строк, а
    подменять Dispatcher ради этого — лишняя механика.
    """
    tree = ast.parse(inspect.getsource(register_handlers))
    calls: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register"
            and isinstance(node.func.value, ast.Name)
        ):
            calls.append(node.func.value.id)
    return calls


def test_catch_all_registered_last() -> None:
    """`appeal` — последний: он забирает любое сообщение.

    Поставь его раньше — и `/export`, `/forget`, `/policy` попадут в
    воронку как текст обращения. Житель наберёт команду и получит
    «опишите проблему» вместо своих данных.
    """
    calls = _registration_calls()
    assert "appeal" in calls, f"appeal вообще не регистрируется: {calls}"
    assert calls[-1] == "appeal", (
        f"«ловец всего» обязан быть последним, порядок сейчас: {calls}"
    )


def test_command_handlers_registered_before_catch_all() -> None:
    """Команды жителя и оператора идут до «ловца всего».

    Проверяем не только соседство, а именно то, что каждый модуль с
    командами стоит раньше — перестановка внутри группы допустима,
    перестановка через appeal — нет.
    """
    calls = _registration_calls()
    appeal_at = calls.index("appeal")
    for module in ("start", "admin_commands", "broadcast"):
        assert module in calls, f"{module} не регистрируется: {calls}"
        assert calls.index(module) < appeal_at, (
            f"{module} зарегистрирован после «ловца всего» — его команды "
            f"будут проваливаться в воронку. Порядок: {calls}"
        )


def test_every_registered_module_exists_and_is_callable() -> None:
    """Каждый вызванный `register` действительно существует.

    Опечатка в имени модуля даёт ошибку импорта на старте, а не тихую
    потерю обработчиков — но лучше поймать её тестом, чем падением бота
    при развёртывании.
    """
    import importlib

    for module_name in _registration_calls():
        mod = importlib.import_module(f"aemr_bot.handlers.{module_name}")
        assert callable(getattr(mod, "register", None)), (
            f"aemr_bot.handlers.{module_name}.register не вызывается"
        )


def test_source_documents_why_order_matters() -> None:
    """В коде объяснено, почему порядок такой.

    Порядок строк выглядит произвольным; без объяснения его переставят
    при первой же уборке импортов. Тест держит объяснение на месте.
    """
    source = inspect.getsource(register_handlers)
    assert "без фильтров" in source or "catch-all" in source.lower(), (
        "в register_handlers нет объяснения, почему appeal последний"
    )


def test_middleware_order_is_documented_and_stable() -> None:
    """Дедупликация стоит раньше отметки активности.

    Повтор события от мессенджера обязан отсеяться до того, как сдвинет
    указатель на карточку: иначе один и тот же апдейт двигает его
    дважды, и карточка «уезжает» от оператора.
    """
    path = Path(inspect.getsourcefile(register_handlers) or "")
    text = path.read_text(encoding="utf-8")
    idem = text.find("idempotency")
    activity = text.find("activity")
    assert idem != -1, "middleware дедупликации не найдена"
    if activity != -1:
        assert idem < activity, (
            "дедупликация обязана стоять раньше отметки активности"
        )
