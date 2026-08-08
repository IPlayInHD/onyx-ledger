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
from app.privacy.classification import (
    MANUALLY_DECLARED_USER_DERIVED,
    NON_RLS,
    STORAGE_SURFACES,
    StorageKind,
)
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

    # Tables that hold user data with no FK to the account. Reachability cannot
    # find them by construction, so they are declared — and the declaration is
    # checked: naming a table that does not exist fails here rather than
    # quietly padding the coverage count.
    missing = sorted(MANUALLY_DECLARED_USER_DERIVED - set(tables))
    assert not missing, (
        f"MANUALLY_DECLARED_USER_DERIVED names tables that do not exist: "
        f"{missing}"
    )
    return reached | set(MANUALLY_DECLARED_USER_DERIVED)


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


# ---------------------------------------------------------------------------
# Every non-RLS table has a reviewed justification (§7)
# ---------------------------------------------------------------------------
def _non_rls_tables() -> set[str]:
    tables, _ = _catalogue()
    conn = psycopg2.connect(owner_dsn())
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT c.relnamespace::regnamespace::text || '.' || c.relname
              FROM pg_class c
             WHERE c.relkind IN ('r', 'p') AND NOT c.relrowsecurity
               AND c.relnamespace::regnamespace::text
                   NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
        """)
        return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


def test_every_non_rls_table_has_an_explicit_justification():
    """The statement this entry wants to be able to make is:

        every non-RLS table has an explicit reviewed privacy/security
        justification

    which is only worth saying if something checks it. A table that appears
    without RLS and without an entry fails here rather than being absorbed.
    """
    actual = _non_rls_tables()
    assert actual, "no non-RLS tables found — the query is wrong"

    unjustified = sorted(actual - set(NON_RLS))
    assert not unjustified, (
        "these tables have no row-level security and no recorded reason:\n  "
        + "\n  ".join(unjustified)
        + "\nClassify each in app/privacy/classification.py:NON_RLS."
    )


def test_the_non_rls_registry_describes_no_table_that_gained_rls():
    """A stale exception claims a hole that has since been closed — which would
    make the defect count wrong in the safe direction, and the justification
    count wrong in the misleading one."""
    actual = _non_rls_tables()
    stale = sorted(set(NON_RLS) - actual)
    assert not stale, (
        f"these tables now HAVE RLS but are still listed as exceptions: {stale}"
    )


def test_the_recorded_defects_are_exactly_the_known_gap():
    """PD-1 is a bounded, named set. If it grows, someone added a tenant-owned
    table without RLS; if it shrinks, 11B1 made progress. Either way it should
    be a deliberate edit rather than a drift."""
    defects = sorted(t for t, e in NON_RLS.items() if e.is_defect)
    assert len(defects) == 16, (
        f"the PD-1 set changed to {len(defects)} tables: {defects}"
    )
    # None of them may be in a schema whose whole point is that it holds no
    # tenant data — that would mean the classification is wrong, not the count.
    for table in defects:
        assert not table.startswith(("ref.", "rules.", "tax_kb.", "tkms.", "admin.")), (
            f"{table} is classed as a tenant-data defect but lives in a "
            "non-tenant schema"
        )


@pytest.mark.parametrize("table,entry", sorted(NON_RLS.items()))
def test_every_non_rls_entry_is_coherent(table, entry):
    assert entry.note.strip(), f"{table}: no reason given"
    assert entry.access_model.strip(), f"{table}: no access model given"

    if entry.direct_identifier:
        assert entry.user_derived, (
            f"{table}: holds a direct identifier but is not marked user-derived"
        )
    if entry.is_defect:
        assert entry.user_derived, (
            f"{table}: classed as a privacy defect but holds no user data — "
            "the classification, not the table, is wrong"
        )
    else:
        assert "PD-1" not in entry.note, (
            f"{table}: cites PD-1 without being classified as a defect"
        )


def test_the_admission_schema_carries_no_identifying_material():
    """§8 — Entry 10's global tables, re-checked from the privacy side.

    They are deliberately non-RLS because a policy keyed on `app.user_id` would
    make the platform-wide capacity count return only the caller's rows. That
    argument only holds if the tables carry nothing worth protecting with RLS.
    """
    conn = psycopg2.connect(owner_dsn())
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod)
              FROM pg_class c
              JOIN pg_attribute a ON a.attrelid = c.oid
             WHERE c.relnamespace::regnamespace::text = 'admission'
               AND a.attnum > 0 AND NOT a.attisdropped
        """)
        columns = cur.fetchall()

        # No plaintext identifier may ever be stored: assert on the DATA, not
        # only on the column names.
        cur.execute("""
            SELECT count(*) FROM admission.rate_counter
             WHERE scope_id LIKE '%@%' OR scope_id ~ '^[0-9]{1,3}(\\.[0-9]{1,3}){3}$'
        """)
        plaintext = cur.fetchone()[0]
    finally:
        conn.close()

    assert columns, "no admission columns found"
    forbidden = {"email", "username", "amount", "value", "content", "text",
                 "document", "filename", "notes"}
    named = sorted(
        f"{table}.{column}" for table, column, _ in columns
        if column.lower() in forbidden
    )
    assert not named, f"admission carries identifying/financial columns: {named}"
    assert plaintext == 0, (
        "a plaintext email address or IP reached admission.rate_counter; the "
        "pre-authentication scopes must store a keyed digest only"
    )


# ---------------------------------------------------------------------------
# Storage surfaces beyond PostgreSQL (§16)
# ---------------------------------------------------------------------------
def test_the_storage_surface_registry_covers_both_applications():
    """The table registry is derived from `pg_catalog`, so it is structurally
    blind to object storage, brokers, logs and a SECOND APPLICATION'S datastore.

    The closeout pass found exactly that blind spot: this repository deploys
    `server/` (Node/Express, Netlify Blobs), not `backend/`, and the first
    inventory pass covered only the latter. This asserts that both applications
    are represented, so the next omission is loud.
    """
    applications = {s.application for s in STORAGE_SURFACES.values()}
    assert "backend" in applications
    assert any(a.startswith("server") for a in applications), (
        "the Node application's storage is missing from the surface registry"
    )


def test_the_deployed_node_store_is_classified():
    """PD-14. It holds email, name, a bcrypt hash, a tax profile and documents,
    and it is what `netlify.toml` actually deploys."""
    blobs = STORAGE_SURFACES["Netlify Blobs"]
    assert blobs.live_user_data and blobs.direct_identifiers
    assert blobs.kind is StorageKind.EXTERNAL_MANAGED_STORE
    assert not blobs.retention_in_repo, (
        "retention for an externally managed store cannot be decided in this "
        "repository"
    )

    local = STORAGE_SURFACES["server/data/db.json"]
    assert local.kind is StorageKind.LOCAL_FILE
    assert not local.live_user_data, (
        "the local FileStore is dev/demo only; if that changes it is no longer "
        "a local file, it is a production datastore"
    )


def test_the_local_node_store_is_not_tracked_by_git():
    """It contains emails and bcrypt hashes. A privacy specification that let
    that file be committed would be self-defeating."""
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "server/data/db.json"],
        cwd=root, capture_output=True, text=True,
    )
    assert tracked.returncode != 0, (
        "server/data/db.json is tracked by git; it holds email addresses and "
        "password hashes"
    )

    ignored = subprocess.run(
        ["git", "check-ignore", "server/data/db.json"],
        cwd=root, capture_output=True, text=True,
    )
    assert ignored.returncode == 0, (
        "server/data/db.json is not gitignored, so it can be committed by "
        "accident"
    )


@pytest.mark.parametrize("name,surface", sorted(STORAGE_SURFACES.items()))
def test_every_surface_states_what_it_holds_and_who_controls_it(name, surface):
    assert surface.note.strip(), f"{name}: no description"
    assert surface.application.strip(), f"{name}: no owning application"

    if surface.kind is StorageKind.NOT_IMPLEMENTED:
        assert not surface.live_user_data, (
            f"{name}: marked not-implemented but holds live user data"
        )
    if surface.live_user_data and not surface.deletion_exists:
        assert "PD-" in surface.note or "not implemented" in surface.note.lower(), (
            f"{name}: holds live user data with no deletion mechanism and no "
            "recorded gap — that combination must be a named defect"
        )
