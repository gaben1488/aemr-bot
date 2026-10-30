"""Update only the shipped appointment text; preserve customized settings."""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

from aemr_bot.services.settings_store import DEFAULTS


def load_migration():
    path = Path(__file__).resolve().parents[1] / 'aemr_bot/db/alembic/versions/0028_appointment_head.py'
    spec = importlib.util.spec_from_file_location('migration_0028', path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_identifies_the_elected_head():
    value = DEFAULTS['appointment_text']
    assert 'временно исполняющим' not in value
    assert 'Гончаров' in value and 'Андре' in value and 'Сергеевич' in value
    assert 'Глав' in value or 'глав' in value


def test_migration_only_replaces_exact_unmodified_default():
    migration = load_migration()
    previous_path = Path(__file__).resolve().parents[1] / 'aemr_bot/db/alembic/versions/0025_appointment_text_okrug.py'
    spec = importlib.util.spec_from_file_location('migration_0025', previous_path)
    assert spec and spec.loader
    previous = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(previous)
    assert migration._OLD == previous._NEW
    assert migration._NEW == DEFAULTS['appointment_text']
    conn = MagicMock()
    migration.op = MagicMock()
    migration.op.get_bind.return_value = conn
    migration.upgrade()
    statement, parameters = conn.execute.call_args.args
    assert "value #>> '{}' = :before" in str(statement)
    assert parameters == {'before': migration._OLD, 'after': migration._NEW}
