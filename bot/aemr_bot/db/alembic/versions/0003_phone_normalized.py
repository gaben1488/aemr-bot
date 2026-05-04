"""Add users.phone_normalized + index, backfill existing rows.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-04

A digits-only mirror of users.phone, kept in sync by the application
layer (services/users.py::_normalize_phone). Backed by a btree index
so /erase phone=... lookups don't full-scan users.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("phone_normalized", sa.String(32), nullable=True),
    )
    op.create_index(
        "ix_users_phone_normalized",
        "users",
        ["phone_normalized"],
    )

    # Backfill: digits-only with leading 7/8 stripped from 11-digit
    # numbers. Mirrors services/users.py::_normalize_phone exactly.
    op.execute(
        """
        UPDATE users
        SET phone_normalized = CASE
            WHEN regexp_replace(phone, '\\D', '', 'g') ~ '^[78][0-9]{10}$'
                THEN substr(regexp_replace(phone, '\\D', '', 'g'), 2)
            ELSE regexp_replace(phone, '\\D', '', 'g')
        END
        WHERE phone IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_users_phone_normalized", table_name="users")
    op.drop_column("users", "phone_normalized")
