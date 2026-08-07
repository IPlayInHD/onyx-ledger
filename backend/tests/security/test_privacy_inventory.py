"""The privacy inventory is enforced against the live schema (Entry 11A §52).

A written inventory is correct on the day it is written. This one is derived:
the set of user-derived tables is computed by walking foreign keys OUT from
`identity.user_account` in the database that the migrations actually built, and
compared against `app.privacy.classification.LIFECYCLE`.

So a table added in a future migration cannot reach production without someone
deciding, on the record, what happens to it when its owner asks to be
forgotten — and an entry here cannot outlive the table it describes.

NON-DESTRUCTIVE BY CONSTRUCTION. Nothing in this file deletes anything; it
reads `pg_catalog` and compares sets. Entry 11A is a specification.
"""
from __future__ import annotations

import psycopg2
import pytest

from app.privacy import LIFECYCLE, DeletionAction, PrivacyClass, SourceKind
from tests.conftest import owner_dsn

#: Schemas that hold no user-derived data by design: published legislation,
#: reference codes, the rule engine's own definitions, and the legislation
#: ingestion pipeline. Every one is identical for every user.
_NON_TENANT_SCHEMAS = frozenset({"ref", "rules", "tax_kb", "tkms", "admission"})


def _catalogue() -> tuple[dict[str, dict], list[tuple[str, str]]]:
    conn = psycopg2.connect(owner_dsn())
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT c.relnamespace::regnamespace::text || '.' || c.relname,
                   c.relrowsecurity, c.relforcerowsecurity,
                   coalesce((SELECT count(*) FROM pg_attribute a
                              WHERE a.attrelid = c.oid AND a.attnum > 0
                                AND NOT a.attisdropped AND a.attname = 'user_id'), 0)
              FROM pg_class c
             WHERE c.relkind IN ('r', 'p')
               AND c.relnamespace::regnamespace::text
                   NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
        """)
        tables = {
            name: {"rls": rls, "force": force, "has_user_id": bool(uid)}
            for name, rls, force, uid in cur.fetchall()
        }
        cur.execute("""
            SELECT ns.nspname || '.' || cl.relname,
                   fns.nspname || '.' || fcl.relname
              FROM pg_constraint con
              JOIN pg_class cl ON cl.oid = con.conrelid
              JOIN pg_namespace ns ON ns.oid = cl.relnamespace
              JOIN pg_class fcl ON fcl.oid = con.confrelid
              JOIN pg_namespace fns ON fns.oid = fcl.relnamespace
             WHERE con.contype = 'f'
        """)
        edges = cur.fetchall()
        return tables, edges
    finally:
        conn.close()


def _user_derived() -> set[str]:
    """Tables reachable from a user-linked table by following foreign keys.

    Seeded from every table carrying a `user_id` column plus the account table
    itself, then propagated to their FK children. Derived rather than listed,
    because a list is exactly the thing that goes stale.
    """
    tables, edges = _catalogue()
    children: dict[str, set[str]] = {}
    for child, parent in edges:
        children.setdefault(parent, set()).add(child)

    seed = {name for name, meta in tables.items() if meta["has_user_id"]}
    seed.add("identity.user_account")

    reached = set(seed)
    frontier = list(seed)
    while frontier:
        current = frontier.pop()
        for child in children.get(current, ()):
            if child in tables and child not in reached:
                reached.add(child)
                frontier.append(child)
    return reached


def test_every_user_derived_table_has_a_lifecycle_classification():
    """The gate. A new user-derived table without a privacy decision fails CI."""
    derived = _user_derived()
    assert derived, "reachability found nothing — the query is wrong"

    unclassified = sorted(derived - set(LIFECYCLE))
    assert not unclassified, (
        "these tables hold user-derived data and have no entry in "
        "app/privacy/classification.py:\n  " + "\n  ".join(unclassified)
        + "\nDecide what happens to each on account deletion and record it."
    )


def test_the_registry_describes_no_table_that_has_vanished():
    """A stale entry is a claim about data that no longer exists."""
    derived = _user_derived()
    stale = sorted(set(LIFECYCLE) - derived)
    assert not stale, (
        "these registry entries name tables that are not user-derived (or no "
        f"longer exist): {stale}"
    )


def test_no_non_tenant_schema_is_classified_as_user_derived():
    """Reference data, legislation and the admission counters must not drift
    into the tenant set.

    If one of them ever does, it means a foreign key now links published or
    operational data to a person — which is a design change worth noticing
    rather than absorbing."""
    leaked = sorted(
        table for table in _user_derived()
        if table.split(".", 1)[0] in _NON_TENANT_SCHEMAS
    )
    assert not leaked, (
        f"non-tenant schemas became FK-reachable from a user: {leaked}"
    )


def test_the_recorded_rls_state_matches_the_database():
    """The registry's `rls` flag is evidence, not an aspiration.

    It is used to describe where the tenant boundary actually is, so a stale
    value would make the specification claim protection the database does not
    provide — the precise failure this entry exists to prevent.
    """
    tables, _ = _catalogue()
    wrong = [
        f"{name}: registry says rls={entry.rls}, database says "
        f"enabled={tables[name]['rls']} forced={tables[name]['force']}"
        for name, entry in LIFECYCLE.items()
        if name in tables
        and entry.rls != bool(tables[name]["rls"] and tables[name]["force"])
    ]
    assert not wrong, "\n  ".join(["registry/database RLS mismatch:", *wrong])


@pytest.mark.parametrize("table,entry", sorted(LIFECYCLE.items()))
def test_every_entry_is_internally_coherent(table, entry):
    """Cheap contradictions, caught at the point they are written."""
    assert entry.classes, f"{table}: no privacy class"

    if entry.replay_dependency:
        assert entry.source in (SourceKind.SEALED_DERIVED, SourceKind.AUDIT), (
            f"{table}: claims replay depends on it but is not sealed or audit "
            "data"
        )
        assert entry.on_account_deletion is not DeletionAction.HARD_DELETE, (
            f"{table}: replay depends on it, so deletion cannot be an "
            "unqualified hard delete — say CUSTOM_WORKFLOW and specify it"
        )

    if PrivacyClass.SEALED_EVIDENCE in entry.classes:
        assert entry.source is SourceKind.SEALED_DERIVED, (
            f"{table}: classed as sealed evidence but not SEALED_DERIVED"
        )

    if entry.on_account_deletion is DeletionAction.DE_IDENTIFY:
        assert entry.notes, (
            f"{table}: de-identification is never obvious — say which columns "
            "carry identity and how it is severed"
        )

    if entry.on_account_deletion is DeletionAction.RETAIN:
        assert entry.notes, (
            f"{table}: retaining a user's data past deletion requires a stated "
            "reason"
        )


def test_the_frozen_snapshot_carries_no_identifier_or_free_text():
    """§10 — the sealed payload must be minimal, asserted against the codec.

    The snapshot is the one place where user financial data is deliberately
    kept beyond the life of its source, so what goes into it is a privacy
    decision and not only a correctness one. Every field must be an input the
    engine consumes; a name, an employer or a filename would be retained
    forever for no computational reason.
    """
    import dataclasses

    from app.services.ioe.frozen.models import canonical_snapshot
    from app.services.tax_engine.core.engine import TaxInput

    payload = canonical_snapshot(TaxInput(), tax_year=2025, jurisdiction="ON")
    sealed = set(payload["inputs"])
    engine_inputs = {f.name for f in dataclasses.fields(TaxInput)}

    assert sealed == engine_inputs, (
        "the sealed payload and the engine's inputs have diverged; a field in "
        "one and not the other is either an un-replayable snapshot or data "
        "retained for no reason"
    )

    forbidden = {
        "name", "display_name", "first_name", "last_name", "email",
        "employer_name", "employer", "filename", "file_name", "object_key",
        "description", "notes", "note", "label", "title", "source_name",
        "address", "sin", "phone",
    }
    leaked = sorted(sealed & forbidden)
    assert not leaked, (
        f"identifying or free-text fields are sealed into every snapshot: "
        f"{leaked}"
    )
