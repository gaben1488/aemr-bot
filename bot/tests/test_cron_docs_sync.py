"""Anti-drift тест: каждый cron-job, который реально регистрирует
`cron.build_scheduler`, должен быть упомянут в каноничных docs.

Решение проблемы, выявленной в Codex PR 1 — `pulse-workhours/offhours/
sunday` оставались в 6 docs-файлах через 1+ день после рефакторинга
cron'а. Этот тест валит CI на любом будущем рассинхроне: добавил cron
в код, не дописал в docs → не пройдёт.

**Источник истины — планировщик, а не реестр.** Раньше тест читал
`cron_registry.JOB_REGISTRY` — ручную копию списка задач. Копия могла
разойтись с `build_scheduler` молча: задача, добавленная только в
`cron.py`, не проверялась вообще ни на docs, ни на реестр. Теперь id'ы
берутся из собранного планировщика (`{j.id for j in sched.get_jobs()}`),
а реестр сверяется с ним отдельным тестом — он остаётся ради
человекочитаемых `schedule_human` / `purpose`, на которые ссылается вика.

Workflow:
1. Зарегистрировать задачу в `services/cron.py::build_scheduler`.
2. Тест `test_registry_covers_exactly_scheduler_jobs` скажет, что не
   хватает записи в `services/cron_registry.JOB_REGISTRY` (описание для
   вики).
3. `test_every_cron_id_appears_in_every_canonical_doc` укажет, в каких
   docs отсутствует `id`.
4. Дописать в docs → CI зелёный.

Покрываемые docs:
- `docs/site/index.html` — единая вики (разделы «Администратору» и
  «Справочник» содержат таблицу фоновых задач со всеми cron-id).
"""
from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("apscheduler", reason="сверка тянет build_scheduler")

from aemr_bot.services import cron  # noqa: E402
from aemr_bot.services.cron_registry import JOB_REGISTRY, all_ids  # noqa: E402


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"

# Канон документации консолидирован в единую вику `docs/site/index.html`
# (поглощённые ролевые .md удалены при сборке базы знаний). Anti-drift теперь
# проверяет вику: каждый cron-id из планировщика обязан встречаться в ней
# (разделы «Администратору» и «Справочник» — таблица фоновых задач).
CANONICAL_DOCS: tuple[pathlib.Path, ...] = (
    DOCS / "site" / "index.html",
)


@pytest.fixture()
def scheduler_job_ids(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """id'ы всех задач реального планировщика — источник истины.

    `bot` / `send_admin_document` / `send_admin_text` подменены заглушками:
    build_scheduler только раскладывает их по `functools.partial`, ни одна
    job не выполняется (scheduler не стартуем).

    `healthcheck-ping` регистрируется лишь при заданном HEALTHCHECK_URL —
    подставляем значение, иначе задача выпала бы из сверки и «пропала» из
    требований к документации.
    """
    monkeypatch.setattr(
        cron.settings, "healthcheck_url", "https://healthcheck.invalid/ping"
    )
    scheduler = cron.build_scheduler(MagicMock(), AsyncMock(), AsyncMock())
    return {job.id for job in scheduler.get_jobs()}


def test_all_canonical_docs_exist() -> None:
    """Sanity-check: все docs-файлы существуют."""
    for path in CANONICAL_DOCS:
        assert path.is_file(), f"Канонический doc отсутствует: {path}"


def test_every_cron_id_appears_in_every_canonical_doc(
    scheduler_job_ids: set[str],
) -> None:
    """Каждый job_id из планировщика упомянут в каждом docs-файле."""
    missing: list[str] = []
    for doc_path in CANONICAL_DOCS:
        text = doc_path.read_text(encoding="utf-8")
        for job_id in sorted(scheduler_job_ids):
            if job_id not in text:
                missing.append(f"`{job_id}` отсутствует в `{doc_path.name}`")
    assert not missing, (
        "Cron-jobs не задокументированы в active docs (drift):\n  - "
        + "\n  - ".join(missing)
        + "\n\nКак починить:\n"
        + "1. Дополнить таблицу cron в указанном docs-файле строкой с этим job_id.\n"
        + "2. Или, если cron внутренний и не должен фигурировать в operator-facing docs,\n"
        + "   убрать его из CANONICAL_DOCS-списка в этом тесте с обоснованием в комменте."
    )


def test_registry_covers_exactly_scheduler_jobs(
    scheduler_job_ids: set[str],
) -> None:
    """Реестр описаний = множество задач планировщика, без расхождений.

    Реестр — витрина для вики (`schedule_human`, `purpose`), а не второй
    список задач: если он разошёлся с `build_scheduler`, вика описывает
    несуществующие задачи либо умалчивает о реальных.
    """
    registry_ids = all_ids()
    only_in_code = scheduler_job_ids - registry_ids
    only_in_registry = registry_ids - scheduler_job_ids
    assert not only_in_code, (
        "Задачи зарегистрированы в cron.py, но не описаны в JOB_REGISTRY "
        f"(вика о них не знает): {sorted(only_in_code)}"
    )
    assert not only_in_registry, (
        "Задачи описаны в JOB_REGISTRY, но планировщик их не регистрирует "
        f"(мёртвые записи): {sorted(only_in_registry)}"
    )


def test_registry_has_unique_ids() -> None:
    """Защита от опечаток: id'ы в реестре уникальны."""
    ids = [entry["id"] for entry in JOB_REGISTRY]
    duplicates = {x for x in ids if ids.count(x) > 1}
    assert not duplicates, f"Дублированные id в JOB_REGISTRY: {duplicates}"


def test_registry_entries_have_required_fields() -> None:
    """Schema: каждая запись имеет id, schedule_human, purpose."""
    required = {"id", "schedule_human", "purpose"}
    for entry in JOB_REGISTRY:
        missing = required - entry.keys()
        assert not missing, f"Запись {entry.get('id', '?')} missing fields: {missing}"


def test_all_ids_returns_set() -> None:
    """Helper `all_ids()` возвращает set всех id'ов."""
    result = all_ids()
    assert isinstance(result, set)
    assert len(result) == len(JOB_REGISTRY)
