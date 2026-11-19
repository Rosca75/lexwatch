"""runs.kind 'preview' (billed units of Rules previews)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-19

A Rules preview that sends scoring requests writes one ``runs`` row with ``kind='preview'`` so
its billed units count in the admin page's monthly total.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _drop_kind_check() -> None:
    # 0001 created it as ck_runs_ck_runs_kind (the naming convention prefixed an already
    # prefixed name); the model names it ck_runs_kind. Drop either, recreate as ck_runs_kind.
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_ck_runs_kind")
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_kind")


def upgrade() -> None:
    _drop_kind_check()
    op.create_check_constraint(
        op.f("ck_runs_kind"), "runs", "kind IN ('daily', 'manual', 'rescore', 'preview')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM runs WHERE kind = 'preview'")
    _drop_kind_check()
    op.create_check_constraint(
        op.f("ck_runs_kind"), "runs", "kind IN ('daily', 'manual', 'rescore')"
    )
