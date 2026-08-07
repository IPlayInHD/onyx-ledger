"""0043_schema_comment_convergence — applies backend/db/sql/37_schema_comment_convergence.sql

Entry 7 schema-drift governance. The Alembic comparison between the applied
schema and the SQLAlchemy metadata is now a gate, and a gate full of known noise
cannot report an unknown change.

Nearly all of the comment/nullability divergence was closed by making the models
truthful, which changed no database object at all. This revision covers the one
divergence pointing the other way: `ioe.strategy_portfolio.portfolio_result_hash`
was documented in the model and undocumented in the database.

COMMENT-ONLY. No column, constraint, index, trigger, policy, grant, or row is
touched, and nothing is rewritten.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0043_schema_comment_convergence"
down_revision = "0042_freshness_scope_and_reasons"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("37_schema_comment_convergence.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
