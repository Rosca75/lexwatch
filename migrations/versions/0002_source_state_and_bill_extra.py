"""source_state (last fetch per source adapter) and bills.extra (source extras)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

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

    op.add_column(
        "bills",
        sa.Column(
            "extra",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("bills", "extra")
    op.drop_table("source_state")
