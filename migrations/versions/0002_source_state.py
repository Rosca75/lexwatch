"""source_state: last fetch per source adapter (skip unchanged downloads)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_state",
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(64)),
        sa.Column("last_modified", sa.DateTime(timezone=True)),
        sa.Column("checksum", sa.String(160)),
        sa.Column("fetched_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
        sa.Column("error", sa.Text()),
        sa.PrimaryKeyConstraint("name", name="pk_source_state"),
        sa.CheckConstraint(
            "status IN ('ok', 'unchanged', 'failed', 'skipped')", name="ck_source_state_status"
        ),
    )


def downgrade() -> None:
    op.drop_table("source_state")
