"""0065_detail_write_cutoff — applies backend/db/sql/59_historical_detail_write_cutoff.sql

Entry 11B6I follow-up. Closes the one gap 11B6I left open and documented: after
HISTORICAL_DETAIL_CLEANUP reported COMPLETE, a writer holding INSERT could put
the purged detail straight back.

Statement-level triggers with `REFERENCING NEW TABLE`, so the check costs one
semi-join per statement rather than one lookup per row — which is what made this
affordable on write paths that insert thousands of rows at a time, and why 11B6I
deferred it rather than shipping a row-level version.

The eight tables integrity verification reads are deliberately NOT covered: they
are retained evidence, never purged, so there is nothing to resurrect.

DOWNGRADE drops the triggers and the five functions. It is safe at any time —
the triggers only ever refuse writes, so removing them restores the previous
(permissive) behaviour and destroys nothing.
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0065_detail_write_cutoff"
down_revision = "0064_historical_detail_cleanup"
branch_labels = None
depends_on = None

_TABLES = (
    "ioe.candidate_cost",
    "ioe.candidate_economic_effect",
    "ioe.confidence_component",
    "ioe.score_component",
    "ioe.recommendation_relationship",
    "ioe.multi_year_projection",
    "ioe.optimization_run_event",
    "ioe.portfolio_evaluation_step",
    "ioe.scenario_result",
    "ioe.scenario_input_change",
    "ioe.scenario_confidence_component",
    "ioe.scenario_event",
    "analysis.analysis_line_item",
    "analysis.analysis_assumption",
    "analysis.reconciliation_check",
)

_FUNCTIONS = (
    "ioe.reject_run_detail_after_deletion_request()",
    "ioe.reject_candidate_detail_after_deletion_request()",
    "ioe.reject_portfolio_detail_after_deletion_request()",
    "ioe.reject_scenario_detail_after_deletion_request()",
    "analysis.reject_analysis_detail_after_deletion_request()",
)


def upgrade() -> None:
    apply_sql_file("59_historical_detail_write_cutoff.sql")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_write_cutoff ON {table}")
    for function in _FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {function}")
