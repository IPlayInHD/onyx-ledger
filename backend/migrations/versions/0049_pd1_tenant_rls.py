"""0049_pd1_tenant_rls — applies backend/db/sql/43_pd1_tenant_rls.sql

Entry 11B1. PD-1: 16 tenant-owned child tables had no row-level security, so
the tenant boundary on them was every service remembering to scope through the
parent rather than anything the database enforced.

VISIBILITY CHANGE, NOT A DATA CHANGE. No row is written, no column is added, no
ownership is inferred or backfilled. The migration enables and forces RLS,
creates one policy per table, and adds eight indexes supporting the policy
predicates.

GRANTS ARE UNCHANGED. `onyx_app_rw` keeps SELECT/INSERT/UPDATE/DELETE and
`onyx_app_ro` keeps SELECT, matching these tables' parents. RLS decides which
rows; the grant decides whether the command exists at all. Removing a privilege
the product uses today on the theory that a future privacy worker will want its
own boundary is not this entry's job.

DOWNGRADE drops the policies and disables RLS, which REINSTATES PD-1 — the
cross-tenant reads, writes and ownership forgery reproduced in
`tests/security/test_pd1_tenant_isolation.py` all become possible again. That is
the correct inverse of this migration and is recorded here so the choice is
visible rather than discovered. Test environments only.
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0049_pd1_tenant_rls"
down_revision = "0048_audit_payload_minimization"
branch_labels = None
depends_on = None

#: The 16 tables and the policy name each one carries.
_TABLES = (
    ("ai.ai_message", "p_self_ai_message"),
    ("ai.ai_message_citation", "p_self_ai_message_citation"),
    ("ai.ai_prompt_context", "p_self_ai_prompt_context"),
    ("analysis.analysis_assumption", "p_self_analysis_assumption"),
    ("analysis.analysis_input_snapshot", "p_self_analysis_input_snapshot"),
    ("analysis.analysis_line_item", "p_self_analysis_line_item"),
    ("analysis.reconciliation_check", "p_self_reconciliation_check"),
    ("billing.invoice", "p_self_invoice"),
    ("docs.document_extraction", "p_self_document_extraction"),
    ("docs.document_link", "p_self_document_link"),
    ("docs.extraction_field", "p_self_extraction_field"),
    ("ioe.run_rule_snapshot", "p_self_run_rule_snapshot"),
    ("reco.recommendation_status_event", "p_self_recommendation_status_event"),
    ("wealth.asset_valuation", "p_self_asset_valuation"),
    ("wealth.liability_balance", "p_self_liability_balance"),
    ("wealth.registered_account_detail", "p_self_registered_account_detail"),
)


def upgrade() -> None:
    apply_sql_file("43_pd1_tenant_rls.sql")


def downgrade() -> None:
    # Reinstates PD-1. See the module docstring.
    for table, policy in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
