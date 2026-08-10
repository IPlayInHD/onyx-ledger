"""0055_lifecycle_worker_acl — applies backend/db/sql/49_lifecycle_worker_acl.sql

Removes EXECUTE on the three account-lifecycle worker keyholes from
`onyx_app_rw`. Reproduced from a genuine application LOGIN before the fix: the
request-path role could claim another tenant's lifecycle — receiving that
tenant's user id, state and claim token — and then advance that account to
ACCESS_DISABLED.

PD-16's remediation revoked a role MEMBERSHIP; these were direct grants on a
different function family, written under the pre-11B5E assumption that the
application role also ran the privacy worker. Since 11B5E5 the worker
authenticates as its own LOGIN, so nothing in the application needs them.

DOWNGRADE RESTORES THE ESCALATION and is a DEVELOPMENT/TEST ROLLBACK ONLY. It
exists so a rollback past this revision leaves a working lifecycle worker for
code that predates the dedicated privacy runtime. Rolling back here reopens the
cross-tenant lifecycle capability.
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0055_lifecycle_worker_acl"
down_revision = "0054_freshness_claim_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("49_lifecycle_worker_acl.sql")


def downgrade() -> None:
    # Reinstates the cross-tenant lifecycle capability. See the module docstring.
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "identity.claim_account_lifecycle(integer, text) TO onyx_app_rw"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "identity.advance_account_lifecycle(uuid, uuid, text, text) TO onyx_app_rw"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "identity.fail_account_lifecycle(uuid, uuid, text, text) TO onyx_app_rw"
    )
