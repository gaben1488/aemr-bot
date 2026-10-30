"""Compare the active ledger's explicit facts with code and migration files.

Values come from the reviewed document, not from a second stale constant in
this test. New fields/keys/tables/revisions must update the ledger too.
"""
from __future__ import annotations

import ast
import json
import pathlib
import re

import pytest

from aemr_bot.config import Settings
from aemr_bot.db.models import Base, OperatorRole
from aemr_bot.services import settings_store

ROOT = pathlib.Path(__file__).resolve().parents[2]
LEDGER = ROOT / "docs/site/_kb/_LEDGER.md"


def _facts() -> dict:
    source = LEDGER.read_text(encoding="utf-8")
    match = re.search(r"<!-- facts:start -->\s*```json\s*(.*?)\s*```\s*<!-- facts:end -->", source, re.DOTALL)
    assert match, "Active ledger must contain a reviewed machine-readable fact block"
    return json.loads(match.group(1))


@pytest.mark.parametrize("field", ["sla_response_hours", "sla_followup_hours", "answer_max_chars",
                                   "summary_max_chars", "attachments_max_per_appeal",
                                   "max_api_retries", "audit_log_retention_days"])
def test_documented_defaults_match_model_fields(field: str) -> None:
    assert _facts()["defaults"][field] == Settings.model_fields[field].default, (
        f"Update active ledger: {field} default changed; installed environment overrides are separate."
    )


def test_documented_settings_keys_match_complete_schema() -> None:
    assert _facts()["settings_keys"] == sorted(settings_store.SCHEMA), "Update ledger for added/removed settings"


def test_documented_operator_roles_match_enum() -> None:
    assert _facts()["operator_roles"] == sorted(r.value for r in OperatorRole)


def test_migrations_match_documented_head_count_and_contiguous_chain() -> None:
    versions = ROOT / "bot/aemr_bot/db/alembic/versions"
    files = [p for p in versions.glob("*.py") if p.name != "__init__.py"]
    numbers = []
    revisions = {}
    for path in files:
        match = re.match(r"^(\d{4})_", path.name)
        assert match, f"Unexpected migration filename: {path.name}"
        numbers.append(int(match.group(1)))
        tree = ast.parse(path.read_text())
        values = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                        values[target.id] = ast.literal_eval(node.value)
            elif (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                  and node.target.id in {"revision", "down_revision"}):
                values[node.target.id] = ast.literal_eval(node.value)
        revisions[values["revision"]] = values["down_revision"]
    facts = _facts()
    assert len(files) == facts["migration_count"]
    assert sorted(numbers) == list(range(1, facts["migration_count"] + 1))
    heads = set(revisions) - set(revisions.values())
    assert heads == {facts["migration_head"]}
    current = facts["migration_head"]
    visited = set()
    while current is not None:
        assert current not in visited, "Migration cycle"
        visited.add(current)
        current = revisions[current]
    assert visited == set(revisions), "Detached migration branch"


def test_documented_orm_tables_match_runtime_and_source() -> None:
    expected = _facts()["orm_tables"]
    assert expected == sorted(Base.metadata.tables)
    tree = ast.parse((ROOT / "bot/aemr_bot/db/models.py").read_text())
    table_classes = [node for node in tree.body if isinstance(node, ast.ClassDef)
                     and any(isinstance(base, ast.Name) and base.id == "Base" for base in node.bases)]
    assert len(table_classes) == len(expected)


def test_audit_retention_constraints_match_documented_range() -> None:
    field = Settings.model_fields["audit_log_retention_days"]
    bounds = {key: getattr(meta, key) for meta in field.metadata for key in ("ge", "le") if hasattr(meta, key)}
    assert bounds == _facts()["audit_retention_bounds"]


def test_image_dependency_install_remains_locked_and_hash_checked() -> None:
    source = (ROOT / "infra/Dockerfile").read_text()
    assert all(value in source for value in ("uv.lock", "uv export", "--require-hashes"))
