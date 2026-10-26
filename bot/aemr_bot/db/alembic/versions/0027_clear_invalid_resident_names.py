"""Clear invalid resident names without deleting users, appeals or dialogue data."""

import sqlalchemy as sa
from alembic import op

from aemr_bot.data.resident_names_v1 import normalize_resident_name

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    users = sa.table(
        "users",
        sa.column("id", sa.Integer),
        sa.column("max_user_id", sa.BigInteger),
        sa.column("first_name", sa.String),
    )
    last_id = 0
    while True:
        rows = conn.execute(
            sa.select(users.c.id, users.c.first_name)
            .where(
                users.c.id > last_id,
                users.c.max_user_id != -1,
                users.c.first_name.isnot(None),
            )
            .order_by(users.c.id)
            .limit(500)
        ).all()
        if not rows:
            break
        invalid = [
            {"uid": row.id, "old_name": row.first_name}
            for row in rows
            if normalize_resident_name(row.first_name) is None
        ]
        if invalid:
            conn.execute(
                sa.update(users)
                .where(
                    users.c.id == sa.bindparam("uid"),
                    users.c.first_name == sa.bindparam("old_name"),
                )
                .values(first_name=None),
                invalid,
            )
        last_id = rows[-1].id


def downgrade() -> None:
    # Intentionally irreversible: never restore rejected nicknames or copy PII
    # into a secondary archive. Relationships/content were never modified.
    pass
