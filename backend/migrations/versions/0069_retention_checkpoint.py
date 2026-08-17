"""0069_retention_checkpoint — applies backend/db/sql/63_retention_checkpoint.sql

Entry: Retention / "What Changed?" Engine. One table recording the product
state a user explicitly ACKNOWLEDGED, which is the baseline every "what changed
since you last looked?" answer is measured from.

It is deliberately not a last-seen marker. Only an explicit acknowledgement
writes here; a GET never does. That distinction is the whole point — a baseline
advanced by reading would let a background refresh erase changes the user never
saw.

The acknowledged snapshot is STORED rather than recomputed, and carries its own
`snapshot_schema_version` independent of every product contract, because
comparing against a reconstruction of "what the state probably was" would lose
exactly the changes worth reporting. Those bytes are evidence: later code
accommodates them and never rewrites them.

Concurrency is enforced in the database, not only in the service. Each row names
the checkpoint it supersedes, and (user_id, tax_year, supersedes_checkpoint_id)
is UNIQUE NULLS NOT DISTINCT — so two clients that both read baseline C1 cannot
both acknowledge from it, and NULLS NOT DISTINCT extends that same guarantee to
the very first baseline, where the superseded id is NULL.

Append-only by REVOKING UPDATE/DELETE from the application role rather than by a
`trg_immutable` trigger, following 0068's precedent and for the same reason:
these rows are LIVE_USER_DATA_DELETE and die through the `identity.user_account`
cascade fired by terminal removal, which does not set the evidence-purge GUC. A
GUC-gated DELETE trigger here would abort the purge it is meant to permit.

This widens the certified privacy deletion universe by one cascade-reachable
table (depth 1). The classification registry, the account-delete registry, the
RLS inventory and the cascade-universe document gain matching entries in the
same change — the enforcement tests hold them in lockstep.

DOWNGRADE drops the table. The loss is acknowledged retention baselines created
after this migration; nothing sealed or replay-bearing references it, and the
next acknowledgement re-establishes a baseline from current state.
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0069_retention_checkpoint"
down_revision = "0068_decision_journal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("63_retention_checkpoint.sql")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ioe.retention_checkpoint")
