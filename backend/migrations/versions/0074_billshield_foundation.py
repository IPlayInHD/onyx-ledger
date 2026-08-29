"""0074_billshield_foundation — applies backend/db/sql/67_billshield_foundation.sql

Entry: BillShield Slice 2 — the seven-table database foundation.

Two global catalogue tables (`provider`, `provider_category`) and five
tenant-derived operational tables (`bill`, `extraction_run`,
`charge_candidate`, `promotion_candidate`, `job_outbox`), with ENABLE + FORCE
row-level security on the five, exact column-scoped API grants, and an empty
`onyx_billshield_worker` group role whose privilege posture the security suite
can assert before any worker exists.

DOWNGRADE DROPS CUSTOMER DATA. Every BillShield bill, extraction and queued
intent goes with the schema; there is no archival step, because at this point
in the integration no customer has been enabled and the tables are empty by
construction.

THE ROLE IS NOT DROPPED. PostgreSQL roles are cluster-wide, not database
objects: dropping `onyx_billshield_worker` here would reach outside this
database and break any other database in the cluster that granted to it. The
canonical teardown leaves ROLES in place for exactly this reason
(`app/database/sql_migrations.py:43-52`).
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0074_billshield_foundation"
down_revision = "0073_legal_acceptance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("67_billshield_foundation.sql")


def downgrade() -> None:
    # Reverse dependency order: children before parents, tables before the
    # domain they use, and the schema last. The triggers and their functions go
    # with the tables and the schema respectively.
    op.execute("DROP TABLE IF EXISTS billshield.promotion_candidate")
    op.execute("DROP TABLE IF EXISTS billshield.charge_candidate")
    op.execute("DROP TABLE IF EXISTS billshield.job_outbox")
    op.execute("DROP TABLE IF EXISTS billshield.extraction_run")
    op.execute("DROP TABLE IF EXISTS billshield.bill")
    op.execute("DROP TABLE IF EXISTS billshield.provider_category")
    op.execute("DROP TABLE IF EXISTS billshield.provider")
    op.execute("DROP FUNCTION IF EXISTS billshield.reject_candidate_mutation()")
    op.execute("DROP FUNCTION IF EXISTS billshield.reject_extraction_run_rewrite()")
    op.execute("DROP FUNCTION IF EXISTS billshield.reject_bill_identity_change()")
    op.execute("DROP DOMAIN IF EXISTS billshield.evidence_locators")
    op.execute("DROP SCHEMA IF EXISTS billshield")
