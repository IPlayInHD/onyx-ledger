"""0070_tax_source_registry — applies backend/db/sql/64_tax_source_registry.sql

Entry: Tax Knowledge Source Registry + Provenance Foundation. Four tables that
answer, deterministically, which authoritative source and which part of it
supports a given Onyx rule, formula or reference-data value.

WHY NEW TABLES RATHER THAN EXTENDING THE EXISTING STUBS. `tax_kb.gov_source` is
(name, url, created_at) and `tax_kb.legislation_reference` is a single free-form
`citation` text column; a rule version points at one of each through nullable
FKs and the loader returns a one-tuple. Neither carries a version, an issuer, a
jurisdiction, an effective period, a content fingerprint or a supersession edge,
so there is nothing on which provenance could hang — and because a `gov_source`
row is shared and mutable, editing it silently rewrites what every rule citing
it claims to rest on. The stubs are left untouched; nothing in this migration
reads, writes or migrates them.

IMMUTABILITY WITHOUT A MUTABLE STATUS. A newer edition never updates the older
row: the SUCCESSOR names its predecessor through `supersedes_version_id`, so
"superseded" is derived from the successor's existence and historical
provenance is never rewritten. The retention checkpoint chain uses the same
shape for the same reason.

SECURITY IS THE POINT OF THE GRANT BLOCK. `tax_kb` carries DEFAULT PRIVILEGES
granting `onyx_app_rw` arwd, so creating a table here would silently hand the
customer runtime role the ability to insert, rewrite and delete authoritative
tax law. The SQL revokes that and leaves ordinary customers with SELECT only.
The KB authoring role receives INSERT and SELECT but not UPDATE or DELETE:
corrections happen by registering a new version and superseding it.

NO RLS, deliberately — tax law is global reference knowledge, not tenant data,
and every existing tax_kb table has row security disabled. This adds no
cascade-reachable table to the account-deletion universe, because none of these
rows belong to a user.

DOWNGRADE drops the four tables. The loss is registered source provenance;
nothing sealed or replay-bearing references them, and no tax result depends on
them.
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0070_tax_source_registry"
down_revision = "0069_retention_checkpoint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("64_tax_source_registry.sql")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tax_kb.knowledge_citation")
    op.execute("DROP TABLE IF EXISTS tax_kb.source_citation")
    op.execute("DROP TABLE IF EXISTS tax_kb.tax_source_version")
    op.execute("DROP TABLE IF EXISTS tax_kb.tax_source")
