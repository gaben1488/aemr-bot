"""Anti-drift тест: собранная вика `docs/site/index.html` обязана точно
совпадать со сборкой из фрагментов `docs/site/_kb2/*.html`.

Зачем. Источник истины вики — десять фрагментов в `_kb2/`. Единый
`index.html` НЕ правится руками: его собирает `docs/site/_kb/_assemble.py`
(каркас `_shell.html` + фрагменты). Раньше сборка запускалась вручную и
ничем не проверялась, поэтому `index.html` однажды разошёлся с `_kb2`
(деплоенная вика показывала старый текст). Этот тест делает дрейф
невозможным: правка фрагмента без пересборки ИЛИ прямая правка
`index.html` валит CI.

Как починить красный тест:
    python docs/site/_kb/_assemble.py
и закоммитить обновлённый `docs/site/index.html`.

Заодно проверяем целостность сборки: все десять фрагментов на месте и ни
одна внутренняя ссылка (`data-go`/`href="#..."`) не висит в пустоту.
"""

from __future__ import annotations

import importlib.util
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ASSEMBLE = REPO_ROOT / "docs" / "site" / "_kb" / "_assemble.py"
INDEX_HTML = REPO_ROOT / "docs" / "site" / "index.html"


def _load_assemble():
    """Импортировать `_assemble.py` как модуль (он не пакет, лежит в docs/)."""
    spec = importlib.util.spec_from_file_location("_aemr_assemble", ASSEMBLE)
    assert spec and spec.loader, f"не удалось загрузить {ASSEMBLE}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_assembler_finds_all_fragments() -> None:
    """Все десять фрагментов `_kb2/*.html` на месте — сборка не «дырявая»."""
    _, missing, _ = _load_assemble().build()
    assert not missing, (
        "Отсутствуют фрагменты вики: " + ", ".join(missing) + ". Проверь docs/site/_kb2/."
    )


def test_assembler_has_no_broken_anchors() -> None:
    """Ни одна внутренняя ссылка вики не ведёт в несуществующий id."""
    _, _, broken = _load_assemble().build()
    assert not broken, "Битые якоря в собранной вике (data-go/href без такого id): " + ", ".join(
        broken
    )


def test_index_html_matches_assembled_fragments() -> None:
    """`index.html` на диске = сборка из текущих `_kb2`. Ловит дрейф."""
    expected, _, _ = _load_assemble().build()
    actual = INDEX_HTML.read_text(encoding="utf-8")
    assert actual == expected, (
        "docs/site/index.html рассинхронен с фрагментами docs/site/_kb2/*.\n"
        "Источник истины — фрагменты; index.html собирается из них.\n"
        "Почини так:\n"
        "    python docs/site/_kb/_assemble.py\n"
        "и закоммить обновлённый docs/site/index.html."
    )
