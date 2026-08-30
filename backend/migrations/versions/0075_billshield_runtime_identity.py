"""0075_billshield_runtime_identity — applies backend/db/sql/68_billshield_runtime_identity.sql

Entry: BillShield Slice 3A — the restricted runtime's enumerated privileges.

Grants only. `onyx_billshield_worker` was created empty by 0074 so its posture
could be asserted before anything was granted to it; this revision gives it the
operation-level allowlist of the integration plan §5.4.6 — plus `USAGE` on
`billshield`, `ref` and `identity` and `EXECUTE` on the deletion-cutoff
function.

EVERY DML GRANT IS COLUMN-SCOPED. The role receives no table-level `SELECT`,
`INSERT` or `UPDATE` on any of the four tables it can touch, because a
table-level verb authorizes every column the table has and every column a later
migration adds. Making a new column writable is meant to cost a reviewed
migration. No outbox privilege, no catalogue read, no extraction-value read, no
`DELETE`, no `TRUNCATE`/`REFERENCES`, no identity-table privilege.

DOWNGRADE DESTROYS NO DATA. It revokes the same list, which restores the group
role to the empty-privilege state 0074 left it in — the state
`tests/security/billshield/test_billshield_privileges.py` asserts for the
pre-3A tree. That is the whole of the reversal: this revision creates no
object, so there is nothing else to drop.

THE ROLE IS NOT DROPPED, for the reason 0074 records: PostgreSQL roles are
cluster-wide, not database objects.
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0075_billshield_runtime_identity"
down_revision = "0074_billshield_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("68_billshield_runtime_identity.sql")


def downgrade() -> None:
    # The exact inverse of the grant list, in reverse order: column privileges
    # first, then the function, then the schemas — so the role never briefly
    # holds a column grant it cannot resolve a name for.
    #
    # EVERY REVOKE NAMES ITS COLUMNS. A bare `REVOKE UPDATE ON <table>` does
    # remove column-level UPDATE in PostgreSQL, and would be shorter — but then
    # this list would no longer be readable as the inverse of the grants above,
    # and a reviewer could not check the two against each other line by line.
    # The security suite asserts the post-downgrade posture is empty, so a
    # column missed here fails the gate rather than lingering.
    op.execute(
        "REVOKE INSERT (extraction_run_id, position, charge_position,"
        " expiry_date, expiry_confidence, expiry_evidence)"
        " ON billshield.promotion_candidate FROM onyx_billshield_worker")
    op.execute(
        "REVOKE INSERT (extraction_run_id, position,"
        " label_text, label_confidence, label_evidence,"
        " amount, amount_confidence, amount_evidence,"
        " kind, kind_confidence,"
        " cadence_value, cadence_confidence, cadence_evidence,"
        " service_period_start, service_period_end,"
        " service_period_confidence, service_period_evidence)"
        " ON billshield.charge_candidate FROM onyx_billshield_worker")
    op.execute(
        "REVOKE UPDATE ("
        " status, completed_at,"
        " refusal_code, failure_code,"
        " adapter_code, model_version, prompt_version,"
        " extraction_schema_version, currency, response_hash,"
        " issuer_name_value, issuer_name_confidence, issuer_name_evidence,"
        " service_category_value, service_category_confidence,"
        " service_category_evidence,"
        " statement_date_value, statement_date_confidence,"
        " statement_date_evidence,"
        " billing_period_start, billing_period_end,"
        " billing_period_confidence, billing_period_evidence,"
        " amount_due_value, amount_due_confidence, amount_due_evidence,"
        " previous_balance_value, previous_balance_confidence,"
        " previous_balance_evidence,"
        " payments_applied_value, payments_applied_confidence,"
        " payments_applied_evidence,"
        " subtotal_before_tax_value, subtotal_before_tax_confidence,"
        " subtotal_before_tax_evidence,"
        " total_tax_value, total_tax_confidence, total_tax_evidence)"
        " ON billshield.extraction_run FROM onyx_billshield_worker")
    op.execute(
        "REVOKE INSERT (bill_id, input_sha256) ON billshield.extraction_run"
        " FROM onyx_billshield_worker")
    op.execute(
        "REVOKE SELECT (id, bill_id) ON billshield.extraction_run"
        " FROM onyx_billshield_worker")
    op.execute(
        "REVOKE UPDATE (status, row_version) ON billshield.bill"
        " FROM onyx_billshield_worker")
    op.execute(
        "REVOKE SELECT (id, user_id, status, storage_key, file_sha256,"
        " byte_size, artifact_format, page_count, deleted_at, erased_at,"
        " row_version) ON billshield.bill FROM onyx_billshield_worker")
    op.execute(
        "REVOKE EXECUTE ON FUNCTION identity.account_deletion_state(uuid)"
        " FROM onyx_billshield_worker")
    op.execute("REVOKE USAGE ON SCHEMA identity FROM onyx_billshield_worker")
    op.execute("REVOKE USAGE ON SCHEMA ref FROM onyx_billshield_worker")
    op.execute("REVOKE USAGE ON SCHEMA billshield FROM onyx_billshield_worker")
