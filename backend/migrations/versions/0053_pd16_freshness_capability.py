"""0053_pd16_freshness_capability — applies backend/db/sql/47_pd16_freshness_capability.sql

PD-16. Removes the membership that let any HTTP request-path session assume the
freshness capability and invoke the privileged cross-tenant outbox keyholes.

DOWNGRADE RESTORES THE ESCALATION and is a DEVELOPMENT/TEST ROLLBACK ONLY, not
a secure production target. It exists so a rollback past this revision leaves a
working relay for code predating the dedicated freshness runtime. Rolling back
here reopens PD-16.
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0053_pd16_freshness_capability"
down_revision = "0052_lifecycle_claim_purge_states"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("47_pd16_freshness_capability.sql")


def downgrade() -> None:
    # Reinstates PD-16. See the module docstring.
    op.execute("GRANT onyx_freshness_worker TO onyx_app_rw")
