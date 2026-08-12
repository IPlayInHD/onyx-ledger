"""0066_terminal_account_removal — applies backend/db/sql/60_terminal_account_removal.sql

Entry 11B6J. Adds `identity.terminal_remove_account`, the keyhole that
physically removes one account after all five privacy phases report COMPLETE
and all five completion guards independently read zero.

PRODUCTION ENABLEMENT IS OFF. No worker calls this function; the dispatcher
still stops at the five phases. The capability exists and is certified, and
invoking it is a deliberate act.

The function is fail-closed on six independently re-read facts: the claim
token, the PURGING state, five phase records, five zero counts, the account's
existence, and the transition trigger from 41_account_lifecycle.

It never touches `audit.audit_log`. PD-15's residue — a deleted customer's UUID
in immutable audit history — is left exactly as it is, to be decided on
measured evidence after the Actor Attribution Decision Gate rather than by
rewriting history to make a number look better.

DOWNGRADE drops the function. Safe at any time: removing a capability that
nothing calls destroys nothing, and accounts already removed stay removed —
which is why this migration cannot and does not try to be reversible in the
data sense.
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0066_terminal_account_removal"
down_revision = "0065_detail_write_cutoff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("60_terminal_account_removal.sql")


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "identity.terminal_remove_account(uuid, uuid, text)"
    )
