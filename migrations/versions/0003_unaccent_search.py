"""accent-insensitive bill search

Revision ID: 0003
Revises: 0001
Create Date: 2026-11-19

Adds the ``unaccent`` extension and a ``lexwatch_fr`` text search configuration (French
stemming after unaccent), and rebuilds the generated ``bills.search_vector`` with it. The web
search uses ``websearch_to_tsquery('lexwatch_fr', q)``, so "securite" finds "sécurité".
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _vector_sql(config: str) -> str:
    return (
        f"to_tsvector('{config}'::regconfig, "
        "number || ' ' || coalesce(title, '') || ' ' || coalesce(summary, ''))"
    )


def _rebuild_search_vector(config: str) -> None:
    op.drop_index("ix_bills_search_vector", table_name="bills")
    op.drop_column("bills", "search_vector")
    op.add_column(
        "bills",
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(_vector_sql(config), persisted=True),
            nullable=True,
        ),
    )
    op.create_index("ix_bills_search_vector", "bills", ["search_vector"], postgresql_using="gin")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'lexwatch_fr') THEN
            CREATE TEXT SEARCH CONFIGURATION lexwatch_fr (COPY = french);
            ALTER TEXT SEARCH CONFIGURATION lexwatch_fr
              ALTER MAPPING FOR hword, hword_part, word WITH unaccent, french_stem;
          END IF;
        END
        $$;
        """
    )
    _rebuild_search_vector("lexwatch_fr")


def downgrade() -> None:
    _rebuild_search_vector("french")
    op.execute("DROP TEXT SEARCH CONFIGURATION IF EXISTS lexwatch_fr")
