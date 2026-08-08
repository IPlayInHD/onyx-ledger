"""0050_pd9_durable_deletion_ledger — applies backend/db/sql/44_pd9_durable_deletion_ledger.sql

Entry 11B3. PD-9: the deletion record must outlive the account it records.

CONSTRAINT-ONLY, AND DELIBERATELY SO. No column is added, no row is written, no
value is recomputed. The account's own internal UUID was already the ledger's
primary key and is already a durable subject identifier — immutable, never
reused — so making the record survive is a matter of removing the link that
destroyed it, not of inventing a new identity.

EXISTING ROWS ARE PRESERVED BY CONSTRUCTION. Dropping a foreign key does not
touch the rows that satisfied it: `requested_at`, `state`, `revision`, attempts
and claim metadata are all untouched, because nothing rewrites them. That is why
this is a constraint change rather than a backfill — a migration that recreated
lifecycle rows could reset a cutoff or duplicate a request event, and this one
cannot.

DOWNGRADE RESTORES THE CASCADE, which reinstates PD-9. Worse, it will FAIL
outright if any lifecycle row's account has already been removed — there would
be nothing for the foreign key to point at, which is precisely the state this
migration exists to make possible. Development and test environments only; a
production downgrade past this revision is not a supported operation.
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0050_pd9_durable_deletion_ledger"
down_revision = "0049_pd1_tenant_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("44_pd9_durable_deletion_ledger.sql")


def downgrade() -> None:
    # Reinstates PD-9. See the module docstring: this fails if any ledger row
    # has outlived its account, which is the situation the upgrade allows.
    op.execute("DROP TRIGGER IF EXISTS trg_account_lifecycle_subject_exists "
               "ON identity.account_lifecycle")
    op.execute("DROP FUNCTION IF EXISTS "
               "identity.require_lifecycle_subject_exists()")
    op.execute("""
        ALTER TABLE identity.account_lifecycle
            ADD CONSTRAINT account_lifecycle_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES identity.user_account(id)
            ON DELETE CASCADE
    """)
    op.execute("""
        ALTER TABLE audit.data_deletion_request
            ADD CONSTRAINT data_deletion_request_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES identity.user_account(id)
            ON DELETE CASCADE
    """)
