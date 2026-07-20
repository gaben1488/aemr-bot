"""Anti-drift тест: ключевые ЧИСЛА из `docs/site/_kb/_LEDGER.md`
запиннены к реальному коду, чтобы супер-канон не мог тихо устареть.

**Зачем.** `_LEDGER.md` объявлен «выверенным реестром фактов» — на него
опираются Регламент v8, вики `_kb2/` и Политика ПДн. Каждый факт привязан
к коду через `file:line`. Но `file:line` дрейфует молча: в самом леджере
уже зафиксированы два таких дрейфа (миграции 17→18, `config.py:129→180`).
Реестр cron'а закрыт `test_cron_docs_sync.py`, реестр callback'ов —
`test_callback_coverage_contract.py`; этот тест закрывает оставшиеся
ключевые числовые факты Части I.

Здесь пиннятся не строки-номера (они подсказка, истина — значение), а
сами ЗНАЧЕНИЯ: если код меняет любое из них, тест падает и заставляет
синхронно поправить И код, И `_LEDGER.md`. Источник истины — КОД
(`aemr_bot`), импорт где возможно; миграции считаются по файлам
`alembic/versions/*.py`.

**Workflow при падении.** Сообщение каждого assert говорит, какой факт
разошёлся. Поправить нужно ОБА: реальное значение в коде (если менялось
оно) и соответствующую строку в `_LEDGER.md` (значение + при желании
`file:line`).

**Покрываемые факты `_LEDGER.md` (Часть I):**
- Оператор §3 / Сводка: `SLA_RESPONSE_HOURS == 4` (`config.py`).
- Разработчик-MLP §B / config: `answer_max_chars == 800`.
- Разработчик §4 / РЕШЕНИЕ audit_log: `audit_log_retention_days == 365`,
  диапазон `ge=30, le=3650`.
- Администратор §4 / Сводка: `len(settings_store.SCHEMA) == 23` (2026-07-09:
  +6 модульных тумблеров `admin_notify_*`, было 17).
- Оператор §2: `OperatorRole` = 4 значения (coordinator/aemr/egp/it).
- Разработчик §1: число файлов миграций `alembic/versions/*.py == 19`,
  последняя ревизия `0019`, нумерация непрерывна 0001..0019.
- Разработчик §2: число ORM-таблиц (классов `(Base)` в `models.py`) `== 11`.
- Администратор §6: `infra/Dockerfile` ставит из `uv.lock` с `--require-hashes`
  (content-check, не число — ловит откат образа на pip-из-диапазонов).
"""
from __future__ import annotations

import inspect
import pathlib
import re

from aemr_bot.config import Settings, settings
from aemr_bot.db.models import Base, OperatorRole
from aemr_bot.services import settings_store


# `bot/tests/<this>` → parents[2] == repo root (тот же приём, что в
# test_cron_docs_sync.py). Отсюда дотягиваемся до леджера и до миграций.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
LEDGER = REPO_ROOT / "docs" / "site" / "_kb" / "_LEDGER.md"
MIGRATIONS_DIR = (
    REPO_ROOT / "bot" / "aemr_bot" / "db" / "alembic" / "versions"
)
MODELS_PATH = REPO_ROOT / "bot" / "aemr_bot" / "db" / "models.py"

# Общий хвост сообщений: при любом падении правим И код, И леджер.
_FIX = (
    "\n\n→ Если упало: значение в коде изменилось. Обнови И КОД, И "
    "`docs/site/_kb/_LEDGER.md` (значение, и при необходимости file:line). "
    "КОД = истина №1; леджер обязан следовать за ним."
)


def _migration_numbers() -> list[int]:
    """Числовые префиксы файлов миграций `NNNN_*.py` (без `__init__`).

    Имена файлов — самый устойчивый anti-drift сигнал: они zero-padded и
    сортируемы (`0001`..`0018`). Возвращаем отсортированный список int'ов.
    """
    nums: list[int] = []
    for path in MIGRATIONS_DIR.glob("*.py"):
        if path.name == "__init__.py":
            continue
        m = re.match(r"^(\d+)_", path.name)
        assert m, (
            f"Файл миграции с неожиданным именем (не `NNNN_*.py`): "
            f"{path.name}. Конвенция нумерации нарушена." + _FIX
        )
        nums.append(int(m.group(1)))
    return sorted(nums)


# --- §3 Оператор / Сводка: SLA_RESPONSE_HOURS == 4 --------------------


def test_sla_response_hours_is_4() -> None:
    """`_LEDGER.md` Оператор §3: `SLA_RESPONSE_HOURS = 4` (config.py)."""
    assert settings.sla_response_hours == 4, (
        f"_LEDGER.md фиксирует SLA_RESPONSE_HOURS=4, в коде "
        f"{settings.sla_response_hours} (config.py, alias SLA_RESPONSE_HOURS)."
        + _FIX
    )


# --- §B Разработчик-MLP / config: answer_max_chars == 800 -------------


def test_answer_max_chars_is_800() -> None:
    """`_LEDGER.md` (Этап B, спецификация): `answer_max_chars = 800`."""
    assert settings.answer_max_chars == 800, (
        f"_LEDGER.md фиксирует answer_max_chars=800, в коде "
        f"{settings.answer_max_chars} (config.py, alias ANSWER_MAX_CHARS)."
        + _FIX
    )


# --- §4 Разработчик / РЕШЕНИЕ audit_log: 365 + диапазон 30..3650 ------


def test_audit_log_retention_days_is_365() -> None:
    """`_LEDGER.md` Разработчик §4: дефолт `audit_log_retention_days=365`."""
    assert settings.audit_log_retention_days == 365, (
        f"_LEDGER.md фиксирует AUDIT_LOG_RETENTION_DAYS=365 (год аудита), "
        f"в коде {settings.audit_log_retention_days} (config.py)." + _FIX
    )


def test_audit_log_retention_range_is_30_to_3650() -> None:
    """`_LEDGER.md` Разработчик §4: диапазон `ge=30, le=3650`.

    Проверяем pydantic-метаданные поля (Ge/Le), а не только дефолт —
    именно диапазон 30..3650 зафиксирован в каноне и в обосновании
    решения по 152-ФЗ.
    """
    field = Settings.model_fields["audit_log_retention_days"]
    bounds: dict[str, int] = {}
    for meta in field.metadata:
        # annotated_types.Ge / Le имеют атрибуты .ge / .le.
        if hasattr(meta, "ge"):
            bounds["ge"] = meta.ge
        if hasattr(meta, "le"):
            bounds["le"] = meta.le
    assert bounds.get("ge") == 30 and bounds.get("le") == 3650, (
        f"_LEDGER.md фиксирует диапазон audit_log_retention_days "
        f"ge=30, le=3650; в коде {bounds} (config.py Field constraints)."
        + _FIX
    )


# --- §4 Администратор / Сводка: len(settings_store.SCHEMA) == 23 ------


def test_settings_schema_has_23_keys() -> None:
    """`_LEDGER.md` Администратор §4: `settings_store.SCHEMA` — 23 ключа.

    2026-07-09: было 17, +6 модульных тумблеров служебных уведомлений
    (`admin_notify_pulse/consent/subscriptions/open_reminder/
    overdue_reminder/monthly_stats`, см. `services/notify_toggles.py`).
    """
    assert len(settings_store.SCHEMA) == 23, (
        f"_LEDGER.md фиксирует len(SCHEMA)=23, в коде "
        f"{len(settings_store.SCHEMA)} (services/settings_store.py). "
        f"Ключи: {sorted(settings_store.SCHEMA)}." + _FIX
    )


# --- §2 Оператор: OperatorRole = 4 значения --------------------------


def test_operator_role_has_four_values() -> None:
    """`_LEDGER.md` Оператор §2: `OperatorRole` — coordinator/aemr/egp/it."""
    actual = {r.value for r in OperatorRole}
    expected = {"coordinator", "aemr", "egp", "it"}
    assert actual == expected, (
        f"_LEDGER.md фиксирует OperatorRole={sorted(expected)} (4 значения), "
        f"в коде {sorted(actual)} (db/models.py)." + _FIX
    )


# --- §1 Разработчик: 22 миграции, последняя 0022 ---------------------


def test_migrations_count_is_22() -> None:
    """`_LEDGER.md` Разработчик §1: ровно 22 файла миграций.

    Считаем файлы `alembic/versions/*.py` (импортировать нечего —
    миграции это файлы). В леджере уже были дрейфы 17→18→19→20→21→22 — этот
    тест ловит следующий.
    """
    nums = _migration_numbers()
    assert len(nums) == 22, (
        f"_LEDGER.md фиксирует 22 миграции (0001..0022), на диске "
        f"{len(nums)}: {nums}." + _FIX
    )


def test_latest_migration_is_0022_and_contiguous() -> None:
    """`_LEDGER.md` Разработчик §1: последняя ревизия `0022`, без дыр.

    Нумерация обязана быть непрерывной 1..22 — иначе пропущена/удалена
    миграция, и «последняя 0022» в леджере вводит в заблуждение.
    """
    nums = _migration_numbers()
    assert nums[-1] == 22, (
        f"_LEDGER.md фиксирует последнюю миграцию 0022, максимальная на "
        f"диске {nums[-1]:04d}." + _FIX
    )
    assert nums == list(range(1, 23)), (
        f"_LEDGER.md подразумевает непрерывную нумерацию 0001..0022, на "
        f"диске пропуски/дубли: {nums}." + _FIX
    )


# --- §2 Разработчик: 11 ORM-таблиц -----------------------------------


def test_orm_table_count_is_11() -> None:
    """`_LEDGER.md` Разработчик §2: ровно 11 ORM-таблиц.

    Проверяем двумя независимыми путями, оба должны дать 11:
    * рантайм — `len(Base.metadata.tables)` (живые таблицы);
    * исходник — число `class X(Base):` в `models.py`.
    Если значения разойдутся между собой — это тоже сигнал (например,
    класс-таблица не подхватился metadata). Оба сверяются с каноном 11.
    """
    runtime_tables = sorted(Base.metadata.tables.keys())
    runtime_count = len(runtime_tables)

    source = MODELS_PATH.read_text(encoding="utf-8")
    source_count = len(re.findall(r"^class \w+\(Base\):", source, re.MULTILINE))

    assert runtime_count == source_count, (
        f"Рассинхрон внутри models.py: metadata={runtime_count} таблиц "
        f"{runtime_tables}, а текстом `class X(Base):` найдено "
        f"{source_count}. Проверь, что все табличные классы наследуют Base "
        f"и имеют __tablename__." + _FIX
    )
    assert runtime_count == 11, (
        f"_LEDGER.md фиксирует 11 ORM-таблиц, в коде {runtime_count}: "
        f"{runtime_tables} (db/models.py)." + _FIX
    )


# --- Sanity: канон и миграции на месте --------------------------------


def test_ledger_file_exists() -> None:
    """Канон `_LEDGER.md` существует по ожидаемому пути."""
    assert LEDGER.is_file(), (
        f"Супер-канон не найден: {LEDGER}. Если переехал — обнови путь "
        f"LEDGER в этом тесте."
    )


def test_migrations_dir_exists() -> None:
    """Каталог миграций существует по ожидаемому пути."""
    assert MIGRATIONS_DIR.is_dir(), (
        f"Каталог миграций не найден: {MIGRATIONS_DIR}. Если переехал — "
        f"обнови MIGRATIONS_DIR в этом тесте."
    )


# --- Self-check: helper не «ослеп» -----------------------------------


def test_migration_numbers_helper_self_check() -> None:
    """Канарейка: `_migration_numbers` реально что-то находит и сортирует.

    Пустой/несортированный результат означал бы, что glob сломан и
    числовые тесты выше прошли бы ложно-зелёными.
    """
    nums = _migration_numbers()
    assert nums, "_migration_numbers() вернул пусто — glob по versions/ сломан."
    assert nums == sorted(nums), "_migration_numbers() обязан быть отсортирован."
    assert nums[0] == 1, f"Первая миграция должна быть 0001, получено {nums[0]:04d}."


def test_imported_symbols_are_live() -> None:
    """Канарейка: импортируемые из `aemr_bot` символы существуют и того
    типа, что ожидаем (защита от молчаливого рефактора имён)."""
    assert hasattr(settings, "sla_response_hours")
    assert hasattr(settings, "answer_max_chars")
    assert hasattr(settings, "audit_log_retention_days")
    assert isinstance(settings_store.SCHEMA, dict)
    assert inspect.isclass(OperatorRole)
    assert "audit_log_retention_days" in Settings.model_fields


# --- §6 Администратор / infra: Dockerfile ставит из uv.lock с хешами ---


def test_dockerfile_uses_uv_lock_with_hashes() -> None:
    """`_LEDGER.md` Администратор §6: образ ставит зависимости из `uv.lock`
    с хеш-проверкой, а не `pip install -e` из диапазонов.

    Content-дрейф (не числовой): Dockerfile уже мигрировал на
    uv.lock + `--require-hashes`, и §6 это фиксирует. Если кто-то откатит
    образ на `pip install -e` из pyproject-диапазонов — lock снова станет
    декоративным, а §6 (и DEPS.md) начнут врать. Тест ловит откат.
    """
    dockerfile = (REPO_ROOT / "infra" / "Dockerfile").read_text(encoding="utf-8")
    for needle in ("uv.lock", "uv export", "--require-hashes"):
        assert needle in dockerfile, (
            f"_LEDGER.md §6 фиксирует установку из uv.lock с хеш-проверкой, "
            f"но в infra/Dockerfile нет «{needle}». Похоже на откат образа "
            f"с lock+hash на pip-из-диапазонов." + _FIX
        )
