"""source_snapshots.last_retrieved_at (cache freshness for unchanged re-fetches)

Revision ID: 0002_snapshot_last_retrieved
Revises: 09238e2d643f
Create Date: 2026-11-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_snapshot_last_retrieved"
down_revision: str | None = "09238e2d643f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source_snapshots", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_retrieved_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("source_snapshots", schema=None) as batch_op:
        batch_op.drop_column("last_retrieved_at")
