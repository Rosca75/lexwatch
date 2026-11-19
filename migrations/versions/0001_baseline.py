"""baseline schema

Revision ID: 0001
Revises:
Create Date: 2026-11-19

Hand-written to match ``lexwatch.models``. ``alembic check`` must report no drift.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEARCH_VECTOR_SQL = (
    "to_tsvector('french'::regconfig, "
    "number || ' ' || coalesce(title, '') || ' ' || coalesce(summary, ''))"
)


def _tz() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("oidc_sub", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320)),
        sa.Column("name", sa.String(255)),
        sa.Column("role", sa.String(16), nullable=False, server_default="user"),
        sa.Column("language", sa.String(2), nullable=False, server_default="fr"),
        sa.Column("last_visit_at", _tz()),
        sa.Column("previous_visit_at", _tz()),
        sa.Column("created_at", _tz(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("oidc_sub", name="uq_users_oidc_sub"),
        sa.CheckConstraint("role IN ('admin', 'user')", name="ck_users_role"),
        sa.CheckConstraint("language IN ('fr', 'en')", name="ck_users_language"),
    )

    op.create_table(
        "bills",
        sa.Column("number", sa.String(32), nullable=False),
        sa.Column("type", sa.String(64)),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text()),
        sa.Column("filing_date", sa.Date()),
        sa.Column("closing_date", sa.Date()),
        sa.Column("author", sa.Text()),
        sa.Column("dossier_url", sa.Text()),
        sa.Column("summary", sa.Text()),
        sa.Column("scored_text", sa.Text()),
        sa.Column("score_state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("first_seen_at", _tz(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_changed_at", _tz(), nullable=False, server_default=sa.func.now()),
        sa.Column("source_updated_at", _tz()),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(SEARCH_VECTOR_SQL, persisted=True),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("number", name="pk_bills"),
        sa.CheckConstraint(
            "score_state IN ('pending', 'scored', 'unscored')", name="ck_bills_score_state"
        ),
    )
    op.create_index(
        "ix_bills_search_vector", "bills", ["search_vector"], postgresql_using="gin"
    )
    op.create_index("ix_bills_filing_date", "bills", ["filing_date"])
    op.create_index("ix_bills_score_state", "bills", ["score_state"])

    op.create_table(
        "bill_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("bill_number", sa.String(32), nullable=False),
        sa.Column("field", sa.String(64), nullable=False),
        sa.Column("old_value", sa.Text()),
        sa.Column("new_value", sa.Text()),
        sa.Column("changed_at", _tz(), nullable=False, server_default=sa.func.now()),
        sa.Column("source", sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_bill_events"),
        sa.ForeignKeyConstraint(
            ["bill_number"],
            ["bills.number"],
            name="fk_bill_events_bill_number_bills",
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
    )
    op.create_index(
        "ix_bill_events_bill_number_changed_at", "bill_events", ["bill_number", "changed_at"]
    )

    op.create_table(
        "profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("threshold", sa.Numeric(4, 3)),
        sa.Column("recipients", sa.Text(), nullable=False, server_default=""),
        sa.Column("rescore_status", sa.String(16), nullable=False, server_default="idle"),
        sa.Column("last_digest_at", _tz()),
        sa.Column("created_at", _tz(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", _tz(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_profiles"),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_profiles_owner_id_users", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "rescore_status IN ('idle', 'pending', 'running')",
            name="ck_profiles_rescore_status",
        ),
        sa.CheckConstraint(
            "threshold IS NULL OR (threshold >= 0 AND threshold <= 1)",
            name="ck_profiles_threshold",
        ),
    )
    op.create_index("ix_profiles_owner_id", "profiles", ["owner_id"])

    op.create_table(
        "rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id", name="pk_rules"),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["profiles.id"], name="fk_rules_profile_id_profiles", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_rules_profile_id", "rules", ["profile_id"])

    op.create_table(
        "rule_scores",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("bill_number", sa.String(32), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("scored_at", _tz(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_rule_scores"),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["rules.id"], name="fk_rule_scores_rule_id_rules", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["bill_number"],
            ["bills.number"],
            name="fk_rule_scores_bill_number_bills",
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.UniqueConstraint("rule_id", "bill_number", name="uq_rule_scores_rule_bill"),
    )
    op.create_index("ix_rule_scores_bill_number", "rule_scores", ["bill_number"])

    op.create_table(
        "matches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("bill_number", sa.String(32), nullable=False),
        sa.Column("profile_score", sa.Float(), nullable=False),
        sa.Column("matched", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("computed_at", _tz(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_matches"),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["profiles.id"],
            name="fk_matches_profile_id_profiles",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["bill_number"],
            ["bills.number"],
            name="fk_matches_bill_number_bills",
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        sa.UniqueConstraint("profile_id", "bill_number", name="uq_matches_profile_bill"),
    )
    op.create_index("ix_matches_bill_number", "matches", ["bill_number"])
    op.create_index("ix_matches_profile_matched", "matches", ["profile_id", "matched"])

    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("requested_by", sa.Integer()),
        sa.Column("created_at", _tz(), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", _tz()),
        sa.Column("finished_at", _tz()),
        sa.Column("bills_added", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bills_changed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bills_scored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bills_unscored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("billed_units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_runs"),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name="fk_runs_requested_by_users",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("kind IN ('daily', 'manual', 'rescore')", name="ck_runs_kind"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'success', 'partial', 'failed')",
            name="ck_runs_status",
        ),
    )
    op.create_index("ix_runs_status", "runs", ["status"])
    op.create_index("ix_runs_started_at", "runs", ["started_at"])


def downgrade() -> None:
    for table in (
        "runs",
        "matches",
        "rule_scores",
        "rules",
        "profiles",
        "bill_events",
        "bills",
        "users",
    ):
        op.drop_table(table)
