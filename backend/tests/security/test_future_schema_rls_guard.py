"""The future-schema FORCE-RLS guard is non-vacuous (BillShield Slice 0A).

THE HOLE THIS PINS CLOSED. Three guards bounded FORCE-RLS coverage with
hand-written schema lists, so a user-derived table in a schema none of them
named — the shape a future `billshield` schema takes — passed every gate with
ENABLE and no FORCE. Measured before the fix: such a table, carrying a real
policy, classified in LIFECYCLE with rls=False and absent from NON_RLS,
survived 211 security and privacy tests. The table owner could read every
tenant while the table looked protected.

These tests plant defective states and prove the corrected guards catch each
one. A guard nobody has seen fail is not evidence, so the planting is
permanent — and every plant observes the guard's own verdict, never merely a
re-statement of its boolean.

FIXTURE HYGIENE MATTERS AS MUCH AS THE ASSERTIONS. Probe schemas are created
in the live catalogue that every catalogue-derived test reads, and the
security gate reruns this suite around twenty-three times against ONE
database. So: a unique schema name per run, a `finally` that drops it however
the test exits, an assertion that it is gone from `pg_namespace`, and a
whole-suite sweep for anything a crashed earlier run might have left. Plants
against the REAL sealed table are transactional and rolled back, with the
clean state re-asserted afterwards.
"""
from __future__ import annotations

import contextlib
import uuid

import psycopg2
import pytest

from app.privacy import classification as C
from tests.conftest import owner_dsn
from tests.security.rls_protection import (
    SEALED_DEFAULT_DENY,
    coherent_waivers,
    forced_rls_violations,
    non_owner_acl_grants,
    protected_tables,
    rls_states,
    sealed_register_violations,
)

#: Every probe schema this file ever creates matches this prefix, so the sweep
#: below can find a leak from any run, not only its own.
_PROBE_PREFIX = "bs_guard_"

#: The schema lists the OLD guards were bounded by, kept verbatim as history.
#: The non-vacuity test asserts the probe schema falls outside both — which is
#: WHY the old guards passed — so if someone re-introduces a list bound, the
#: proof of what that costs is still executable.
_OLD_PRIVILEGE_GUARD_SCHEMAS = frozenset(
    {"ioe", "finance", "wealth", "analysis", "reco", "profile", "docs",
     "billing"})
_OLD_PD1_GUARD_SCHEMAS = frozenset(
    {"ai", "analysis", "billing", "docs", "finance", "ioe", "profile", "reco",
     "wealth"})

#: The one approved sealed table; several plants below target it by name so a
#: register rename fails loudly here rather than silently un-planting a test.
_SEALED = "identity.account_subject"


def _owner(autocommit: bool = True):
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = autocommit
    return conn


@contextlib.contextmanager
def _probe_schema(*, enable: bool = True, force: bool, policy: bool = True):
    """A user-derived table in a schema no guard list names.

    Three independently defective states are expressible: ENABLE off entirely,
    ENABLE without FORCE, and ENABLE+FORCE without a policy — plus the fully
    correct state as the control. Dropped in `finally` whatever happens, and
    its absence asserted, because a stray schema here fails every
    catalogue-derived test in later reordered runs for reasons unrelated to
    the code under test.
    """
    schema = f"{_PROBE_PREFIX}{uuid.uuid4().hex[:12]}"
    table = f"{schema}.bill"
    conn = _owner()
    cur = conn.cursor()
    try:
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(f"""
            CREATE TABLE {table} (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id uuid NOT NULL REFERENCES identity.user_account(id)
                    ON DELETE CASCADE,
                amount numeric NOT NULL
            )
        """)
        if enable:
            cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        if force:
            cur.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        if policy:
            cur.execute(f"""
                CREATE POLICY probe_tenant ON {table} FOR ALL
                    USING (user_id = ref.current_app_user())
                    WITH CHECK (user_id = ref.current_app_user())
            """)
        yield schema, table
    finally:
        cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        cur.execute("SELECT count(*) FROM pg_namespace WHERE nspname = %s",
                    (schema,))
        gone = cur.fetchone()[0] == 0
        conn.close()
        assert gone, f"probe schema {schema} survived its own cleanup"


@contextlib.contextmanager
def _lifecycle_entry_saying_rls_false(table: str):
    """A LIFECYCLE entry recording, accurately, that FORCE is absent.

    This is the half of the reproduction that makes the old suite's acceptance
    damning rather than merely incomplete: the table was not forgotten — it
    was classified, its rls=False was truthful, the privacy inventory accepted
    all of it, and no gate asked whether the absence of FORCE was *allowed*.
    The new guard must treat this entry as evidence, never as a waiver.
    """
    entry = C.TableLifecycle(
        table=table,
        classes=(C.PrivacyClass.FINANCIAL_SOURCE_DATA,),
        source=C.SourceKind.SOURCE,
        retention=C.RetentionClass.WHILE_ACCOUNT_ACTIVE,
        on_account_deletion=C.DeletionAction.CASCADE_DELETE,
        rls=False,
        exportable=True,
        on_user_deletion=C.UserDeletionAction.HARD_DELETE,
        purge=C.PurgeParticipation.SET_BASED_DELETE,
        completion_predicate=(
            f"SELECT count(*) FROM {table} WHERE user_id = :subject"),
        notes="Slice 0A non-vacuity fixture; never present outside one test.",
    )
    C.LIFECYCLE[table] = entry
    try:
        yield
    finally:
        C.LIFECYCLE.pop(table, None)


@contextlib.contextmanager
def _non_rls_entry(table: str, *, user_derived: bool, defect: bool = False,
                   note: str = "planted", access: str = "planted"):
    """A NON_RLS entry of chosen coherence, patched in and always removed."""
    reason = (C.NonRlsReason.PRIVACY_DEFECT_REQUIRES_REMEDIATION if defect
              else C.NonRlsReason.CROSS_TENANT_OPERATIONAL_STATE)
    C.NON_RLS[table] = C.NonRlsTable(
        table=table, reason=reason, user_derived=user_derived,
        direct_identifier=False, pseudonymous_identifier=False,
        access_model=access, note=note,
    )
    try:
        yield
    finally:
        C.NON_RLS.pop(table, None)


# ---------------------------------------------------------------------------
# The original hole, planted and caught
# ---------------------------------------------------------------------------
def test_an_unforced_user_table_in_a_new_schema_fails_the_guard():
    """The exact §4.1 case: user-derived, ENABLE, not FORCE, a real policy,
    LIFECYCLE rls=False, absent from NON_RLS. Old guards passed it; the
    protected-set guard must not."""
    with _probe_schema(force=False) as (schema, table):
        # Why the OLD guards passed: the schema is outside both lists. Kept
        # executable so a re-introduced list bound re-proves its own cost.
        assert schema not in _OLD_PRIVILEGE_GUARD_SCHEMAS
        assert schema not in _OLD_PD1_GUARD_SCHEMAS

        with _lifecycle_entry_saying_rls_false(table):
            assert table in protected_tables(), (
                "the derivation missed a user_id-bearing FK child; the guard "
                "is vacuous for exactly the case it exists for"
            )
            assert table not in C.NON_RLS

            violations = forced_rls_violations()
            assert any(v.startswith(table) for v in violations), (
                f"{table} has ENABLE without FORCE and a LIFECYCLE entry "
                f"saying rls=False, and the guard accepted it: {violations}"
            )


def test_a_forced_and_policied_table_in_a_new_schema_satisfies_the_guard():
    """The control. A guard that flags every new-schema table is a different
    defect with the same green history — prove the corrected state passes."""
    with _probe_schema(force=True) as (_, table):
        assert table in protected_tables()
        violations = forced_rls_violations()
        assert not any(v.startswith(table) for v in violations), (
            f"a table with ENABLE+FORCE and a policy was flagged: {violations}"
        )


def test_lifecycle_rls_false_is_not_consulted_as_an_exemption():
    """The registry field records; it must not waive.

    Same planted table, two registry states: no LIFECYCLE entry at all, and a
    LIFECYCLE entry with rls=False. The guard's verdict must be identical —
    flagged — in both, or editing a Python file has become a way to strip a
    table's tenant boundary.
    """
    with _probe_schema(force=False) as (_, table):
        without_entry = any(
            v.startswith(table) for v in forced_rls_violations())
        with _lifecycle_entry_saying_rls_false(table):
            with_entry = any(
                v.startswith(table) for v in forced_rls_violations())
    assert without_entry and with_entry, (
        "the guard's verdict changed with LIFECYCLE content "
        f"(no entry: {without_entry}, rls=False entry: {with_entry}); "
        "the registry is evidence, not authority"
    )


# ---------------------------------------------------------------------------
# The remaining defective states, each planted and observed
# ---------------------------------------------------------------------------
def test_a_zero_policy_table_outside_the_sealed_register_fails_the_guard():
    """ENABLE+FORCE with no policy is default-deny — but ONLY a reviewed
    sealed-register entry may claim that state. An unregistered one is far
    more likely a migration that forgot its policy, and it must fail."""
    with _probe_schema(force=True, policy=False) as (_, table):
        assert table not in SEALED_DEFAULT_DENY
        assert table in protected_tables()
        violations = forced_rls_violations()
        assert any(v.startswith(table) and "policies=0" in v
                   for v in violations), (
            f"an unregistered zero-policy table was accepted: {violations}"
        )


def test_a_table_with_rls_disabled_entirely_fails_the_guard():
    """No ENABLE at all — the loudest defect, asserted directly rather than
    left to the privacy inventory's separate justification gate."""
    with _probe_schema(enable=False, force=False, policy=False) as (_, table):
        assert table in protected_tables()
        violations = forced_rls_violations()
        assert any(v.startswith(table) and "enable=False" in v
                   for v in violations), (
            f"a table with row level security disabled was accepted: "
            f"{violations}"
        )


# ---------------------------------------------------------------------------
# The NON_RLS subtraction fails closed
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("shape,kwargs", [
    ("user_derived=False", dict(user_derived=False)),
    ("defect entry", dict(user_derived=True, defect=True)),
    ("empty note", dict(user_derived=True, note="   ")),
    ("empty access_model", dict(user_derived=True, access="")),
])
def test_an_incoherent_non_rls_entry_cannot_exempt_a_table(shape, kwargs):
    """Presence in NON_RLS is not authority; coherence is.

    A user-derived table whose NON_RLS entry denies it is user-derived,
    records it as a DEFECT, or carries a hollow justification stays in the
    protected set and keeps failing the guard. Proven directly against the
    guard's own verdict — not left to the inventory coherence test that runs
    elsewhere and later.
    """
    with _probe_schema(force=False) as (_, table):
        with _non_rls_entry(table, **kwargs):
            assert table in C.NON_RLS          # present…
            assert table not in coherent_waivers(), (
                f"an entry with {shape} was accepted as a coherent waiver")
            assert table in protected_tables()  # …but exempts nothing
            violations = forced_rls_violations()
            assert any(v.startswith(table) for v in violations), (
                f"a NON_RLS entry with {shape} exempted a user-derived table "
                f"from the RLS guard: {violations}"
            )


def test_a_coherent_non_rls_entry_does_exempt():
    """The control for fail-closed: the waiver mechanism still works when the
    entry is complete, or NON_RLS has silently stopped meaning anything."""
    with _probe_schema(force=False) as (_, table):
        with _non_rls_entry(table, user_derived=True,
                            note="planted coherent waiver (Slice 0A control)",
                            access="owner-only; planted for one test"):
            assert table in coherent_waivers()
            assert table not in protected_tables()
            assert not any(v.startswith(table)
                           for v in forced_rls_violations())


# ---------------------------------------------------------------------------
# The sealed register cannot rot — proven from pg_catalog, transactionally
# ---------------------------------------------------------------------------
def test_every_sealed_default_deny_entry_is_still_sealed():
    """The register's standing claim, all of it, from the catalogue: a reason,
    a real protected table outside NON_RLS, ENABLE+FORCE, exactly zero
    policies, zero non-owner table/column ACL grants (PUBLIC included).
    Membership waives only the policy count; everything else holds."""
    conn = _owner()
    try:
        violations = sealed_register_violations(conn.cursor())
    finally:
        conn.close()
    assert not violations, "\n  ".join(["the sealed register rotted:",
                                        *violations])


@contextlib.contextmanager
def _sealed_tx():
    """A transaction against the real sealed table, ALWAYS rolled back, with
    the clean state re-proven afterwards so cleanup is evidence, not hope."""
    conn = _owner(autocommit=False)
    try:
        yield conn.cursor()
    finally:
        conn.rollback()
        conn.close()
        check = _owner()
        try:
            cur = check.cursor()
            assert non_owner_acl_grants(cur, _SEALED) == [], (
                "a planted grant on the sealed table survived rollback")
            assert not sealed_register_violations(cur), (
                "the sealed register still reports violations after rollback")
        finally:
            check.close()


def test_a_table_grant_to_an_unlisted_role_fails_the_sealed_check():
    """The case the four-role list missed by construction: onyx_audit_writer
    was never in it. The catalogue-derived check has no list to miss from."""
    with _sealed_tx() as cur:
        cur.execute(f"GRANT SELECT ON {_SEALED} TO onyx_audit_writer")
        grants = non_owner_acl_grants(cur, _SEALED)
        assert grants == ["onyx_audit_writer: SELECT"], grants
        violations = sealed_register_violations(cur)
        assert any(_SEALED in v and "onyx_audit_writer" in v
                   for v in violations), violations


def test_a_grant_to_public_fails_the_sealed_check_and_names_public():
    """PUBLIC is grantee oid 0 in the ACL, not a role row — the reason
    information_schema.role_table_grants was refused for this check. The
    failure must say PUBLIC, not a bare 0."""
    with _sealed_tx() as cur:
        cur.execute(f"GRANT SELECT ON {_SEALED} TO PUBLIC")
        grants = non_owner_acl_grants(cur, _SEALED)
        assert grants == ["PUBLIC: SELECT"], grants
        assert any(_SEALED in v and "PUBLIC" in v
                   for v in sealed_register_violations(cur))


def test_a_column_level_grant_fails_the_sealed_check():
    """Column ACLs live in pg_attribute.attacl, not pg_class.relacl — a check
    that reads only the table ACL calls a column-leaking table sealed."""
    with _sealed_tx() as cur:
        cur.execute(
            f"GRANT UPDATE (user_id) ON {_SEALED} TO onyx_audit_writer")
        grants = non_owner_acl_grants(cur, _SEALED)
        assert grants == ["onyx_audit_writer: UPDATE ON COLUMN user_id"], grants
        assert any(_SEALED in v and "user_id" in v
                   for v in sealed_register_violations(cur))


def test_a_sealed_table_gaining_a_policy_fails_the_sealed_check():
    """A policy on a sealed table means it is no longer default-deny; the
    register entry is then a stale claim and must fail, not stretch."""
    with _sealed_tx() as cur:
        cur.execute(f"""
            CREATE POLICY planted_probe ON {_SEALED} FOR ALL
                USING (false) WITH CHECK (false)
        """)
        violations = sealed_register_violations(cur)
        assert any(_SEALED in v and "polic" in v for v in violations), (
            f"a planted policy on the sealed table went unnoticed: {violations}"
        )


def test_a_sealed_table_losing_force_fails_the_sealed_check():
    """Membership waives the policy count and NOTHING else. Remove FORCE and
    the owner stops being subject to (absent) policies — the register must
    refuse, because ENABLE and FORCE are never waived."""
    with _sealed_tx() as cur:
        cur.execute(f"ALTER TABLE {_SEALED} NO FORCE ROW LEVEL SECURITY")
        violations = sealed_register_violations(cur)
        assert any(_SEALED in v and "force=False" in v for v in violations), (
            f"FORCE removed from the sealed table went unnoticed: {violations}"
        )


# ---------------------------------------------------------------------------
# Hygiene: no probe survives, whichever run created it
# ---------------------------------------------------------------------------
def test_no_probe_schema_survives_in_the_catalogue():
    """The gate reruns this suite ~23 times against one database. A probe
    schema leaked by a crashed earlier RUN — not merely an earlier test —
    would fail every catalogue-derived test after it for unrelated reasons,
    so the leak itself is asserted against, whole-catalogue, every run."""
    conn = _owner()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname LIKE %s",
            (_PROBE_PREFIX + "%",))
        leaked = [r[0] for r in cur.fetchall()]
        # And the live registries carry no probe residue either: a crashed
        # in-process patch would poison every later test in this process.
        registry_residue = [
            t for t in list(C.LIFECYCLE) + list(C.NON_RLS)
            if t.startswith(_PROBE_PREFIX)
        ]
    finally:
        conn.close()
    assert not leaked, (
        f"probe schemas leaked into the shared catalogue: {leaked}; drop them "
        "and find the run that failed to clean up"
    )
    assert not registry_residue, (
        f"planted registry entries survived their tests: {registry_residue}"
    )


def test_rls_states_reads_the_same_catalogue_the_guard_does():
    """Cheap coherence: the sealed table is visible to the shared state reader
    with exactly the shape the register claims, so the two code paths cannot
    drift apart silently."""
    states = rls_states()
    assert _SEALED in states
    enable, force, policies = states[_SEALED]
    assert enable and force and policies == 0
