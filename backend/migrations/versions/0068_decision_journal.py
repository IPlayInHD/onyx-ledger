"""0068_decision_journal — applies backend/db/sql/62_decision_journal.sql

Entry: Tax Decision Journal. Two tables recording what a user DECIDED and what
they REPORTED doing — the user-authority record the Assurance Map deliberately
left blank when it declined to invent a COMPLETE status.

`ioe.decision_journal` is one immutable decision thread pinning the sealed
artifact identities the user was shown; `ioe.decision_journal_event` is its
append-only history, ordered by a per-thread sequence assigned under the
thread's row lock. Append-only is enforced by REVOKING UPDATE/DELETE from the
application role rather than by a `trg_immutable` trigger, because these rows
are LIVE_USER_DATA_DELETE: they die through the `identity.user_account`
cascade fired by terminal removal, which does not set the evidence-purge GUC —
a GUC-gated DELETE trigger here would abort the purge it is supposed to allow.

This widens the certified privacy deletion universe by two cascade-reachable
tables (depths 1 and 2). The classification registry, the account-delete
registry, and the cascade-universe document gain matching entries in the same
change — the enforcement tests hold them in lockstep.

DOWNGRADE drops both tables. The loss is user decision history created after
this migration; nothing sealed or replay-bearing references it.
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0068_decision_journal"
down_revision = "0067_counterfactual_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("62_decision_journal.sql")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ioe.decision_journal_event")
    op.execute("DROP TABLE IF EXISTS ioe.decision_journal")
