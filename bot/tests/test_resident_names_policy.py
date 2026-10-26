import importlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import sqlalchemy as sa

from aemr_bot.data.resident_names_v1 import given_names
from aemr_bot.utils.resident_names import normalize_resident_name


@pytest.mark.parametrize(
    "name, expected",
    [
        ("сергей", "Сергей"),
        ("Беспаленко Лариса", "Беспаленко Лариса"),
        ("Лариса Беспаленко", "Лариса Беспаленко"),
        ("Беспаленко Лариса Ивановна", "Беспаленко Лариса Ивановна"),
        ("Лариса Ивановна Беспаленко", "Лариса Ивановна Беспаленко"),
        ("Иванов Иван", "Иванов Иван"),
        ("беспаленко   ЛАРИСА", "Беспаленко Лариса"),
        ("Беспаленко Анна-Мария", "Беспаленко Анна-Мария"),
        ("  ИВАН   ИВАНОВ ", "Иван Иванов"),
        ("Анна Марковна Иванова", "Анна Марковна Иванова"),
        ("Алёна", "Алёна"),
        ("Алена", "Алена"),
        ("Фёдор", "Фёдор"),
        ("Мухаммад", "Мухаммад"),
        ("Рустам", "Рустам"),
        ("Анна-Мария", "Анна-Мария"),
        ("Сергей Петров-Сидоров", "Сергей Петров-Сидоров"),
        ("Анна\u00a0Иванова", "Анна Иванова"),
    ],
)
def test_known_cyrillic_names_and_optional_parts(name, expected):
    assert normalize_resident_name(name) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        " ",
        123,
        "Котик",
        "Администратор",
        "Удалено",
        "user",
        "Ивaн",
        "Сергей77",
        "@Сергей",
        "Сергей 😎",
        "Сергей Иванов1",
        "Сергей Иванов X",
        "Сергей\nИванов",
        "Сергей\tИванов",
        "Сер\u200bгей",
        "Сергей\u202eИванов",
        "Сергей Иванов Иванович Лишнее",
        "Беспаленко Никнейм Лариса",
        "Беспаленко Лaриса",
        "Беспаленко\nЛариса",
        "Беспаленко Лариса77",
        "Беспаленко Никнейм",
        "Иван-Никнейм",
        "-Иван",
        "Иван-",
        "Иван--Пётр",
        "Иван.",
        "Иван " + "Я" * 120,
    ],
)
def test_nicknames_mixed_scripts_and_malformed_input_rejected(value):
    assert normalize_resident_name(value) is None


def test_limits_and_dictionary_are_offline_and_stable():
    assert len(given_names()) > 12000
    assert normalize_resident_name("Сергей", max_chars=3) is None
    assert normalize_resident_name("Иван " + "Я" * 116, max_chars=9999) is None


def test_migration_clears_only_invalid_names_preserving_all_relationships():
    migration = importlib.import_module(
        "aemr_bot.db.alembic.versions.0027_clear_invalid_resident_names"
    )
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, max_user_id BIGINT, first_name TEXT, dialog_data TEXT)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE appeals (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), summary TEXT)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE messages (id INTEGER PRIMARY KEY, appeal_id INTEGER REFERENCES appeals(id), text TEXT)"
            )
        )
        conn.execute(
            sa.text("INSERT INTO users VALUES (:id,:uid,:name,:data)"),
            [
                {
                    "id": i,
                    "uid": i,
                    "name": "Котик" if i % 2 else "Сергей Иванов",
                    "data": "saved-draft",
                }
                for i in range(1, 1004)
            ]
            + [
                {"id": 1004, "uid": -1, "name": "Удалено", "data": "sentinel"},
                {"id": 1005, "uid": 1005, "name": "Беспаленко Лариса", "data": "draft"},
                {"id": 1006, "uid": 1006, "name": "Лариса Беспаленко", "data": "draft"},
            ],
        )
        conn.execute(sa.text("INSERT INTO appeals VALUES (1,1,'existing appeal')"))
        conn.execute(sa.text("INSERT INTO messages VALUES (1,1,'existing message')"))
        with patch.object(migration, "op", SimpleNamespace(get_bind=lambda: conn)):
            migration.upgrade()
            migration.upgrade()
            migration.downgrade()
        assert conn.scalar(sa.text("SELECT count(*) FROM users")) == 1006
        assert (
            conn.scalar(sa.text("SELECT first_name FROM users WHERE id=1005"))
            == "Беспаленко Лариса"
        )
        assert (
            conn.scalar(sa.text("SELECT first_name FROM users WHERE id=1006"))
            == "Лариса Беспаленко"
        )
        assert conn.scalar(sa.text("SELECT first_name FROM users WHERE id=1")) is None
        assert conn.scalar(sa.text("SELECT first_name FROM users WHERE id=2")) == "Сергей Иванов"
        assert (
            conn.scalar(sa.text("SELECT first_name FROM users WHERE max_user_id=-1")) == "Удалено"
        )
        assert conn.scalar(sa.text("SELECT count(*) FROM users WHERE first_name IS NULL")) == 502
        assert conn.execute(sa.text("SELECT user_id,summary FROM appeals")).one() == (
            1,
            "existing appeal",
        )
        assert conn.scalar(sa.text("SELECT text FROM messages")) == "existing message"
        assert conn.scalar(sa.text("SELECT dialog_data FROM users WHERE id=1")) == "saved-draft"
    engine.dispose()
