"""Anti-drift тест: каждый cron-job из `cron_registry.JOB_REGISTRY`
должен быть упомянут в каноничных docs.

Решение проблемы, выявленной в Codex PR 1 — `pulse-workhours/offhours/
sunday` оставались в 6 docs-файлах через 1+ день после рефакторинга
cron'а. Этот тест валит CI на любом будущем рассинхроне: добавил cron
в код, не дописал в docs → не пройдёт.

Workflow:
1. Добавить запись в `services/cron_registry.JOB_REGISTRY`.
2. Зарегистрировать в `services/cron.py::build_scheduler`.
3. Этот тест укажет, в каких docs отсутствует `id`.
4. Дописать в docs → CI зелёный.

Покрываемые docs (минимум — где есть таблицы cron):
- `docs/HOW_IT_WORKS.md` — раздел «Регулярные задачи»
- `docs/RUNBOOK.md` — таблица расписания
- `docs/SYSADMIN.md` — таблица расписания
"""
from __future__ import annotations

import pathlib


from aemr_bot.services.cron_registry import JOB_REGISTRY, all_ids


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"

# Каноничные документы, в которых КАЖДЫЙ id из реестра должен встречаться.
# Если cron upper-tier (например, специфический backup), и его смысла
# нет в operator-facing docs — можно убрать оттуда явно.
CANONICAL_DOCS: tuple[pathlib.Path, ...] = (
    DOCS / "HOW_IT_WORKS.md",
    DOCS / "RUNBOOK.md",
    DOCS / "SYSADMIN.md",
)


def _read_doc_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def test_all_canonical_docs_exist() -> None:
    """Sanity-check: все docs-файлы существуют."""
    for path in CANONICAL_DOCS:
        assert path.is_file(), f"Канонический doc отсутствует: {path}"


def test_every_cron_id_appears_in_every_canonical_doc() -> None:
    """Каждый job_id из JOB_REGISTRY упомянут в каждом docs-файле."""
    missing: list[str] = []
    for doc_path in CANONICAL_DOCS:
        text = _read_doc_text(doc_path)
        for entry in JOB_REGISTRY:
            job_id = entry["id"]
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
