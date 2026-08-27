"""The protected set: which tables owe an enforced tenant boundary.

ONE DEFINITION, USED BY EVERY GUARD (BillShield Slice 0A). Until this module
existed there were three separate answers to "which tables must have FORCE row
level security", each bounded by a hand-written schema list, and a user-derived
table in a schema none of the lists named — the shape a future `billshield`
schema takes — passed every gate. Reproduced before this was written: a table
with ENABLE but not FORCE, classified in LIFECYCLE with rls=False and absent
from NON_RLS, survived the full security and privacy suites (211 tests).

THE ALGORITHM, exactly and deliberately subtractive:

    protected = { all catalogue-derived user-derived tables }
                MINUS
                { tables whose NON_RLS entry is a COHERENT reviewed waiver }

"User-derived" is computed by `test_privacy_inventory._user_derived` — seeded
on every `user_id` column plus `identity.user_account`, propagated along
foreign keys, unioned with `MANUALLY_DECLARED_USER_DERIVED` — imported rather
than reimplemented, so the two can never disagree. That derivation is what
puts foreign-key-derived CHILD tables inside the set without naming them, and
what makes a table in a schema created next year a member the day its
migration runs.

THE SUBTRACTION FAILS CLOSED. Mere presence in NON_RLS is not enough: an entry
only waives protection when it is explicitly marked `user_derived=True`, is
not a recorded defect, and carries a non-empty note and access model. An entry
that claims the table holds no user data (`user_derived=False`) while the
catalogue derivation says it does, an entry classified
PRIVACY_DEFECT_REQUIRES_REMEDIATION, or an entry with a hollow justification
leaves the table PROTECTED — a half-written waiver must widen nothing.

WHAT IS NOT AN EXEMPTION. `LIFECYCLE.entry.rls` is recorded evidence of what
the database does — `test_the_recorded_rls_state_matches_the_database` keeps
it honest — and it carries no authority to waive tenant protection: a guard
that honoured it could be defeated by editing a Python file. The ONLY reviewed
waiver is a coherent `NON_RLS` entry as defined above.

THE RULE. Every table in the protected set must have ENABLE and FORCE. It
normally must also carry at least one policy. A deliberately keyhole-only
table may INSTEAD appear in `SEALED_DEFAULT_DENY` below — zero policies, zero
non-owner ACL grants — which is stricter than a policy, not weaker. Membership
there waives ONLY the policy count; ENABLE, FORCE, catalogue existence and
privilege closure are never waived.
"""
from __future__ import annotations

import psycopg2

from app.privacy.classification import NON_RLS
from tests.conftest import owner_dsn
from tests.security.test_privacy_inventory import _user_derived

#: Tables that are deliberately ENABLE + FORCE with ZERO policies: default-deny
#: for every role (short of a PostgreSQL superuser or BYPASSRLS, which no
#: application identity holds), reachable only through SECURITY DEFINER
#: keyholes. That is MORE protection than a policy, not less — a policy grants
#: scoped access; no policy grants none — so requiring "at least one policy"
#: here would flag the strongest state in the catalogue as a defect.
#:
#: The claim an entry makes is exact: zero policies AND zero non-owner ACL
#: grants, table- and column-level, PUBLIC included. `sealed_register_
#: violations` proves it from `pg_catalog` on every run, so this register
#: cannot quietly become an exemption for a table that gained access. It is
#: NOT a shortcut for ordinary tenant tables — BillShield operational tables
#: carry real tenant policies — and it says nothing about superusers, whom no
#: ACL can restrain.
SEALED_DEFAULT_DENY: dict[str, str] = {
    "identity.account_subject": (
        "Entry 11B6 (PD-15). The live subject-key mapping; resolving it is the "
        "only way audit history stays attributable, and removal of one row is "
        "the de-identification mechanism. 50_audit_auth_deidentification.sql "
        "revokes ALL from the app roles and grants no policy on purpose: "
        "access is exclusively through the subject_key_for / "
        "count_attributable_audit_auth / deidentify_audit_auth keyholes."
    ),
}


def coherent_waivers() -> set[str]:
    """NON_RLS entries that actually carry the authority to waive protection.

    Fail-closed on every axis: the entry must say, on the record, that the
    table is user-derived AND justified — not merely exist. A defect entry is
    the opposite of a waiver; an empty note or access model is a waiver nobody
    finished writing.
    """
    return {
        table for table, entry in NON_RLS.items()
        if entry.user_derived
        and not entry.is_defect
        and entry.note.strip()
        and entry.access_model.strip()
    }


def protected_tables() -> set[str]:
    """The tables that owe an enforced tenant boundary, derived not listed."""
    return _user_derived() - coherent_waivers()


def rls_states() -> dict[str, tuple[bool, bool, int]]:
    """{table: (enable, force, policy_count)} for every non-system table."""
    conn = psycopg2.connect(owner_dsn())
    try:
        return _rls_states(conn.cursor())
    finally:
        conn.close()


def _rls_states(cur) -> dict[str, tuple[bool, bool, int]]:
    cur.execute("""
        SELECT c.relnamespace::regnamespace::text || '.' || c.relname,
               c.relrowsecurity, c.relforcerowsecurity,
               (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid)
          FROM pg_class c
         WHERE c.relkind IN ('r', 'p')
           AND c.relnamespace::regnamespace::text
               NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
    """)
    return {name: (enable, force, policies)
            for name, enable, force, policies in cur.fetchall()}


def forced_rls_violations() -> list[str]:
    """Every protected table that lacks its enforced boundary. Empty is green.

    A violation string carries the observed state so the failure says what to
    fix, not merely that something is wrong.
    """
    states = rls_states()
    violations: list[str] = []
    for table in sorted(protected_tables()):
        if table not in states:
            violations.append(
                f"{table}: user-derived but not a table in the live catalogue")
            continue
        enable, force, policies = states[table]
        needs_policy = table not in SEALED_DEFAULT_DENY
        if not (enable and force and (policies >= 1 or not needs_policy)):
            violations.append(
                f"{table}: enable={enable} force={force} policies={policies}")
    return violations


def non_owner_acl_grants(cur, table: str) -> list[str]:
    """Every table- OR column-level ACL grant on `table` to a non-owner.

    Read from `pg_class.relacl` and `pg_attribute.attacl` via `aclexplode`,
    which covers EVERY grantable privilege (SELECT, INSERT, UPDATE, DELETE,
    TRUNCATE, REFERENCES, TRIGGER) and every grantee — group roles by their
    own ACL entry, PUBLIC as grantee oid 0, rendered as PUBLIC. Deliberately
    NOT information_schema.role_table_grants, which flattens PUBLIC and role
    coverage exactly where this check needs them.

    Runs through the CALLER'S cursor, so an uncommitted GRANT planted inside
    the caller's transaction is visible — that is what makes the non-vacuity
    tests fully transactional, leaving nothing behind on rollback.

    A NULL relacl means owner-only default privileges; `acldefault` expands it
    so the owner filter still applies. A NULL attacl means no column grants at
    all, and `aclexplode` (strict) yields zero rows for it — measured, not
    assumed.

    What this does NOT claim: anything about superusers or BYPASSRLS roles,
    whom no ACL restrains. The sealed guarantee is zero non-owner ACL grants
    plus ENABLE+FORCE+zero policies, and nothing more.
    """
    cur.execute("""
        SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC'
                    ELSE pg_get_userbyid(a.grantee) END,
               a.privilege_type,
               NULL::text
          FROM pg_class c
          CROSS JOIN LATERAL aclexplode(
                   coalesce(c.relacl, acldefault('r', c.relowner))) a
         WHERE c.oid = %s::regclass
           AND a.grantee <> c.relowner
        UNION ALL
        SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC'
                    ELSE pg_get_userbyid(a.grantee) END,
               a.privilege_type,
               att.attname
          FROM pg_class c
          JOIN pg_attribute att
            ON att.attrelid = c.oid AND att.attnum > 0 AND NOT att.attisdropped
          CROSS JOIN LATERAL aclexplode(att.attacl) a
         WHERE c.oid = %s::regclass
           AND a.grantee <> c.relowner
         ORDER BY 1, 2, 3
    """, (table, table))
    return [
        f"{grantee}: {privilege}" + (f" ON COLUMN {column}" if column else "")
        for grantee, privilege, column in cur.fetchall()
    ]


def sealed_register_violations(cur) -> list[str]:
    """Everything wrong with the sealed register, proven from pg_catalog.

    Runs through the caller's cursor for the same reason as
    `non_owner_acl_grants`: a planted policy, ALTER, or GRANT inside the
    caller's transaction must be visible to the check that exists to refuse
    it. Empty means every entry still earns its place.
    """
    violations: list[str] = []
    if not SEALED_DEFAULT_DENY:
        return ["the sealed register is empty; remove this machinery with it"]
    states = _rls_states(cur)
    protected = protected_tables()
    for table, reason in SEALED_DEFAULT_DENY.items():
        if not reason.strip():
            violations.append(f"{table}: sealed with no recorded reason")
        if table not in states:
            violations.append(f"{table}: sealed register names a ghost")
            continue
        if table not in protected:
            violations.append(
                f"{table}: sealed but not in the protected set; the register "
                "only refines the protected rule and cannot reach past it")
        if table in NON_RLS:
            violations.append(
                f"{table}: sealed AND in NON_RLS; a table has one waiver "
                "mechanism, never two")
        enable, force, policies = states[table]
        if not (enable and force):
            violations.append(
                f"{table}: sealed but enable={enable} force={force}; "
                "membership never waives ENABLE or FORCE")
        if policies != 0:
            violations.append(
                f"{table}: gained {policies} policy(ies); it is no longer "
                "default-deny and must leave the sealed register and satisfy "
                "the ordinary policy requirement")
        grants = non_owner_acl_grants(cur, table)
        if grants:
            violations.append(
                f"{table}: non-owner ACL grants on a keyhole-only table: "
                + "; ".join(grants))
    return violations
