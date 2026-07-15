"""appeals.latitude/longitude/geo_confidence — сохранение координат обращения.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-16

Житель делится геолокацией, appeal_geo (services/geo.py, point-in-polygon
по посёлкам + ближайшее здание) распознаёт населённый пункт и адрес — но
сама точка после подтверждения выбрасывалась. Теперь сохраняем её:
разблокирует пин-точный слой «Обращения граждан» на карте округа (без
координат потолок — гранулярность посёлка), останавливает необратимую
потерю пинов.

Три nullable double-precision поля:
- latitude / longitude — координаты WGS-84 (None при ручном вводе адреса
  без геолокации);
- geo_confidence — уверенность локального reverse-geocoding (0..1 или None).

Неразрушающая аддитивная миграция: существующие обращения получают NULL.

Downgrade: drop columns.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("appeals", sa.Column("latitude", sa.Float(), nullable=True))
    op.add_column("appeals", sa.Column("longitude", sa.Float(), nullable=True))
    op.add_column("appeals", sa.Column("geo_confidence", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("appeals", "geo_confidence")
    op.drop_column("appeals", "longitude")
    op.drop_column("appeals", "latitude")
