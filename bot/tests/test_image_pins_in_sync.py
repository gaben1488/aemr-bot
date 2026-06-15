"""Guard: digest Postgres в docker-compose.yml и в Quadlet-юните — в lockstep.

Dependabot НЕ видит образы в Quadlet-юнитах (infra/podman/*.container) — для
Podman/systemd экосистемы у него нет (проверено по supported-ecosystems).
docker-compose постгрес Dependabot бампит, а Quadlet — нет, поэтому при апдейте
легко забыть второй файл и получить разные версии БД в docker- и podman-
развёртываниях. Этот тест краснеет при расхождении пинов. См. вику
«Разработчику» → «Что Dependabot покрыть не может».
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_INFRA = Path(__file__).resolve().parents[2] / "infra"
_COMPOSE = _INFRA / "docker-compose.yml"
_QUADLET = _INFRA / "podman" / "aemr-bot-db.container"

_PG_DIGEST = re.compile(r"postgres:16-alpine@sha256:([0-9a-f]{64})")


def _digest(path: Path) -> str:
    if not path.is_file():
        pytest.skip(f"{path} отсутствует (запуск вне полного чекаута репозитория)")
    match = _PG_DIGEST.search(path.read_text(encoding="utf-8"))
    assert match, f"postgres:16-alpine@sha256:... не найден в {path.name}"
    return match.group(1)


def test_postgres_digest_in_sync_compose_vs_quadlet() -> None:
    compose = _digest(_COMPOSE)
    quadlet = _digest(_QUADLET)
    assert compose == quadlet, (
        "Digest Postgres разошёлся между docker-compose.yml и Quadlet-юнитом "
        "aemr-bot-db.container:\n"
        f"  compose:  sha256:{compose}\n"
        f"  quadlet:  sha256:{quadlet}\n"
        "Dependabot бампит только compose — синхронизируйте Quadlet вручную."
    )
