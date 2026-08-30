"""Who may do what to the BillShield schema, proved two different ways.

TWO PROOFS, AND WHY IT MUST BE TWO
----------------------------------
1. **Direct ACL enumeration.** Read `pg_class.relacl` and `pg_attribute.attacl`
   through `aclexplode`, PUBLIC included, and fail on any grantee or verb
   outside the allowlist. This is the only way to see a COLUMN grant and the
   only way to see a grant nobody exercises.

2. **Effective behaviour.** Become the governed role and attempt the real
   operation, expecting to be allowed or refused by PostgreSQL.

Neither alone is enough. A catalogue read cannot tell you the grant works; a
behavioural test cannot tell you an EXTRA grant exists that nothing happens to
use. And an effective-privilege sweep of every row in `pg_roles` would be worse
than useless here: the test harness's login roles are MEMBERS of the group
roles, so `has_table_privilege` reports the inherited privilege for each of
them and the sweep manufactures findings that name the wrong principal. Direct
ACL enumeration answers "who was granted this", which is the question.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest

from tests.security.billshield.conftest import (
    GLOBAL_TABLES,
    TENANT_TABLES,
    as_role,
    owner_cursor,
)

#: The complete intended privilege map, table -> grantee -> verbs, with a
#: reason for every entry. An allowlist nobody prunes becomes a deny-list with
#: extra steps, so the reason column is part of the test rather than a comment
#: about it.
INTENDED_TABLE_ACL: dict[str, dict[str, set[str]]] = {
    "billshield.provider": {
        "onyx_app_rw": {"SELECT"},          # runtime read-only catalogue
    },
    "billshield.provider_category": {
        "onyx_app_rw": {"SELECT"},          # runtime read-only catalogue
    },
    "billshield.bill": {
        # SELECT only at table level, and only for the API: its create is a
        # COLUMN grant on `user_id` alone, which is why it appears in the column
        # map instead. The WORKER holds nothing here at table level — every one
        # of its verbs is column-scoped, which is the whole design.
        "onyx_app_rw": {"SELECT"},
    },
    "billshield.extraction_run": {
        "onyx_app_rw": {"SELECT"},          # review screens read
    },
    "billshield.charge_candidate": {
        "onyx_app_rw": {"SELECT"},          # review screens read
    },
    "billshield.promotion_candidate": {
        "onyx_app_rw": {"SELECT"},          # review screens read
    },
    "billshield.job_outbox": {},            # column-scoped only; see below
}

#: The four tables the BillShield worker may touch, and the verbs it holds on
#: each — every one of them COLUMN-scoped, so none appears in the table map
#: above. Kept beside it rather than derived from it, so a widening has to be
#: written twice: once as an intention and once as a grant.
WORKER_TABLE_VERBS = {
    "billshield.bill": {"SELECT", "UPDATE"},
    "billshield.extraction_run": {"SELECT", "INSERT", "UPDATE"},
    "billshield.charge_candidate": {"INSERT"},
    "billshield.promotion_candidate": {"INSERT"},
}

#: The worker's complete column map, table -> verb -> columns. Duplicated from
#: the runtime-identity suite ON PURPOSE: that file proves the grants behave,
#: this one proves the schema's whole ACL surface is exactly what was reviewed,
#: and a single shared constant would let one edit satisfy both.
WORKER_COLUMN_ACL: dict[str, dict[str, set[str]]] = {
    "billshield.bill": {
        "SELECT": {"id", "user_id", "status", "storage_key", "file_sha256",
                   "byte_size", "artifact_format", "page_count", "deleted_at",
                   "erased_at", "row_version"},
        "UPDATE": {"status", "row_version"},
    },
    "billshield.extraction_run": {
        "SELECT": {"id", "bill_id"},
        "INSERT": {"bill_id", "input_sha256"},
        "UPDATE": {
            "status", "completed_at", "refusal_code", "failure_code",
            "adapter_code", "model_version", "prompt_version",
            "extraction_schema_version", "currency", "response_hash",
            "issuer_name_value", "issuer_name_confidence", "issuer_name_evidence",
            "service_category_value", "service_category_confidence",
            "service_category_evidence",
            "statement_date_value", "statement_date_confidence",
            "statement_date_evidence",
            "billing_period_start", "billing_period_end",
            "billing_period_confidence", "billing_period_evidence",
            "amount_due_value", "amount_due_confidence", "amount_due_evidence",
            "previous_balance_value", "previous_balance_confidence",
            "previous_balance_evidence",
            "payments_applied_value", "payments_applied_confidence",
            "payments_applied_evidence",
            "subtotal_before_tax_value", "subtotal_before_tax_confidence",
            "subtotal_before_tax_evidence",
            "total_tax_value", "total_tax_confidence", "total_tax_evidence",
        },
    },
    "billshield.charge_candidate": {
        "INSERT": {"extraction_run_id", "position",
                   "label_text", "label_confidence", "label_evidence",
                   "amount", "amount_confidence", "amount_evidence",
                   "kind", "kind_confidence",
                   "cadence_value", "cadence_confidence", "cadence_evidence",
                   "service_period_start", "service_period_end",
                   "service_period_confidence", "service_period_evidence"},
    },
    "billshield.promotion_candidate": {
        "INSERT": {"extraction_run_id", "position", "charge_position",
                   "expiry_date", "expiry_confidence", "expiry_evidence"},
    },
}

#: Column-level grants, table -> grantee -> verb -> columns. This is where the
#: enqueue boundary actually lives, and a table-level view of the schema cannot
#: see it at all.
INTENDED_COLUMN_ACL: dict[str, dict[str, dict[str, set[str]]]] = {
    "billshield.bill": {
        "onyx_app_rw": {
            # A bill is created by naming its owner and nothing else: `id` and
            # `storage_key` are generated, `status` defaults, and every artifact
            # fact arrives later at upload completion.
            "INSERT": {"user_id"},
            # PRESENT because upload completion moves the four artifact facts
            # from unset to finalized exactly once — the trigger, not the grant,
            # is what stops them changing afterwards. ABSENT: ownership and the
            # generated storage key (immutable identity), and `erased_at`, which
            # is the privacy worker's claim that erasure happened, not the API's.
            # `updated_at` is ABSENT: the ref.set_updated_at() trigger owns it,
            # and a runtime that could write it could backdate its own edit.
            "UPDATE": {"status", "file_sha256", "byte_size", "artifact_format",
                       "page_count", "deleted_at", "row_version"},
        },
        "onyx_billshield_worker": {
            # The worker's whole read set — eleven named columns, so the two it
            # is denied (`created_at`, `updated_at`) are denied by the ACL and
            # a column added later is denied by default.
            "SELECT": WORKER_COLUMN_ACL["billshield.bill"]["SELECT"],
            # The worker-owned lifecycle transitions and their optimistic
            # concurrency stamp, and nothing else on the row. ABSENT and each
            # belonging to somebody else: `deleted_at` is the customer's
            # request, `erased_at` is the privacy worker's claim that physical
            # erasure happened, the four artifact facts are finalized once at
            # upload completion, and `updated_at` is the trigger's.
            "UPDATE": WORKER_COLUMN_ACL["billshield.bill"]["UPDATE"],
        },
    },
    "billshield.extraction_run": {
        "onyx_billshield_worker": WORKER_COLUMN_ACL["billshield.extraction_run"],
    },
    "billshield.charge_candidate": {
        "onyx_billshield_worker": WORKER_COLUMN_ACL["billshield.charge_candidate"],
    },
    "billshield.promotion_candidate": {
        "onyx_billshield_worker":
            WORKER_COLUMN_ACL["billshield.promotion_candidate"],
    },
    "billshield.job_outbox": {
        "onyx_app_rw": {
            # Enqueue authority: the four intent columns. Every operational
            # column is owned by its server default.
            "INSERT": {"user_id", "bill_id", "task_code", "dedupe_key"},
            # Deliberately narrow: SQLAlchemy fetches the server-generated key
            # with RETURNING, which is a SELECT on the returned column.
            "SELECT": {"id"},
        },
    },
}

ALL_TABLES = TENANT_TABLES + GLOBAL_TABLES


def _table_acl(cur) -> dict[str, dict[str, set[str]]]:
    """Every DIRECT table-level grantee and verb in the schema, PUBLIC included."""
    cur.execute(
        """
        SELECT n.nspname || '.' || c.relname,
               COALESCE(pg_get_userbyid(a.grantee), 'PUBLIC'),
               a.privilege_type
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,
                       acldefault('r', c.relowner))) AS a
        WHERE n.nspname = 'billshield' AND c.relkind = 'r'
        """
    )
    out: dict[str, dict[str, set[str]]] = {}
    for table, grantee, verb in cur.fetchall():
        # grantee oid 0 renders as an empty name through pg_get_userbyid.
        out.setdefault(table, {}).setdefault(grantee or "PUBLIC", set()).add(verb)
    return out


def _column_acl(cur) -> dict[str, dict[str, dict[str, set[str]]]]:
    """Every DIRECT column-level grantee, verb and column in the schema."""
    cur.execute(
        """
        SELECT n.nspname || '.' || c.relname,
               COALESCE(pg_get_userbyid(a.grantee), 'PUBLIC'),
               a.privilege_type,
               att.attname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute att ON att.attrelid = c.oid AND att.attnum > 0
        CROSS JOIN LATERAL aclexplode(att.attacl) AS a
        WHERE n.nspname = 'billshield' AND c.relkind = 'r'
          AND att.attacl IS NOT NULL
        """
    )
    out: dict[str, dict[str, dict[str, set[str]]]] = {}
    for table, grantee, verb, column in cur.fetchall():
        out.setdefault(table, {}).setdefault(grantee or "PUBLIC", {}) \
           .setdefault(verb, set()).add(column)
    return out


def _owner(cur) -> str:
    cur.execute(
        "SELECT pg_get_userbyid(c.relowner) FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'billshield' AND c.relname = 'bill'"
    )
    return cur.fetchone()[0]


# ------------------------------------------------------- proof 1: direct ACLs

def test_the_direct_table_privileges_are_exactly_intended():
    """Discovered from the catalogue, compared to the allowlist, both ways."""
    with owner_cursor() as cur:
        actual = _table_acl(cur)
        owner = _owner(cur)

    assert set(actual) == set(ALL_TABLES), (
        f"billshield tables in the catalogue: {sorted(actual)}")

    unexpected: list[str] = []
    for table, grants in actual.items():
        for grantee, verbs in grants.items():
            if grantee == owner:
                continue  # the owner's implicit full ACL, not a grant
            intended = INTENDED_TABLE_ACL[table].get(grantee, set())
            for verb in sorted(verbs - intended):
                unexpected.append(f"{table}: {grantee} holds {verb}")
    assert not unexpected, "unexpected direct table privileges:\n  " + \
        "\n  ".join(unexpected)

    missing: list[str] = []
    for table, grants in INTENDED_TABLE_ACL.items():
        for grantee, verbs in grants.items():
            held = actual.get(table, {}).get(grantee, set())
            for verb in sorted(verbs - held):
                missing.append(f"{table}: {grantee} is missing {verb}")
    assert not missing, "intended privileges that were never granted:\n  " + \
        "\n  ".join(missing)


def test_the_direct_column_privileges_are_exactly_intended():
    """The enqueue boundary lives here, so it is enumerated column by column."""
    with owner_cursor() as cur:
        actual = _column_acl(cur)

    assert set(actual) == set(INTENDED_COLUMN_ACL), (
        f"column grants exist on {sorted(actual)}, expected "
        f"{sorted(INTENDED_COLUMN_ACL)}")
    for table, grants in actual.items():
        for grantee, verbs in grants.items():
            intended = INTENDED_COLUMN_ACL[table].get(grantee, {})
            assert set(verbs) == set(intended), (
                f"{table}: {grantee} holds column verbs {sorted(verbs)}, "
                f"expected {sorted(intended)}")
            for verb, columns in verbs.items():
                assert columns == intended[verb], (
                    f"{table}: {grantee} {verb} covers {sorted(columns)}, "
                    f"expected {sorted(intended[verb])}")


def test_public_holds_nothing_anywhere_in_the_schema():
    """Including the schema itself and every function."""
    with owner_cursor() as cur:
        table_acl = _table_acl(cur)
        column_acl = _column_acl(cur)
        cur.execute(
            "SELECT COALESCE(pg_get_userbyid(a.grantee), 'PUBLIC'), a.privilege_type"
            " FROM pg_namespace n"
            " CROSS JOIN LATERAL aclexplode(n.nspacl) AS a"
            " WHERE n.nspname = 'billshield'"
        )
        schema_acl = {(g or "PUBLIC", v) for g, v in cur.fetchall()}
        cur.execute(
            "SELECT p.proname, COALESCE(pg_get_userbyid(a.grantee), 'PUBLIC'),"
            "       a.privilege_type"
            " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,"
            "        acldefault('f', p.proowner))) AS a"
            " WHERE n.nspname = 'billshield'"
        )
        function_acl = [(fn, g or "PUBLIC", v) for fn, g, v in cur.fetchall()]

    assert not [t for t, g in table_acl.items() if "PUBLIC" in g], \
        "PUBLIC holds a table privilege in billshield"
    assert not [t for t, g in column_acl.items() if "PUBLIC" in g], \
        "PUBLIC holds a column privilege in billshield"
    assert not [x for x in schema_acl if x[0] == "PUBLIC"], \
        f"PUBLIC holds schema privileges: {schema_acl}"
    assert not [x for x in function_acl if x[1] == "PUBLIC"], \
        f"PUBLIC can execute billshield functions: {x_list(function_acl)}"


def x_list(rows):  # small helper so the assertion message stays readable
    return sorted({f"{fn}:{g}:{v}" for fn, g, v in rows if g == "PUBLIC"})


def test_no_runtime_holds_delete_truncate_or_references():
    """Withheld by default, everywhere, for every non-owner grantee.

    DELETE because BillShield's deletion model is a tombstone plus
    privacy-worker erasure; TRUNCATE because a policy cannot filter a
    whole-table wipe; REFERENCES because nothing outside this schema should be
    able to pin its rows.
    """
    with owner_cursor() as cur:
        actual = _table_acl(cur)
        owner = _owner(cur)
    offenders = [
        f"{table}: {grantee} holds {verb}"
        for table, grants in actual.items()
        for grantee, verbs in grants.items() if grantee != owner
        for verb in sorted(verbs & {"DELETE", "TRUNCATE", "REFERENCES"})
    ]
    assert not offenders, "\n  ".join(offenders)


def test_the_worker_group_role_is_confined_to_its_enumerated_grants():
    """Slice 2 created the role EMPTY; Slice 3A gave it exactly one allowlist.

    THIS TEST USED TO ASSERT AN EMPTY SET, and the change is deliberate rather
    than a relaxation: every claim below is still an equality, so a grant added
    later fails here instead of being absorbed by a subset check. What the
    empty-set version bought was that the posture was assertable before anything
    was granted; what this version buys is that the thing granted is exactly the
    thing that was reviewed.

    Still zero, and stated separately because each is its own failure mode: no
    login, no superuser, no BYPASSRLS, no privilege on any table outside
    BillShield, no sequence privilege, and nothing outside the schemas the
    worker's own tables and its two name-resolution needs account for.
    """
    with owner_cursor() as cur:
        cur.execute(
            "SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles"
            " WHERE rolname = 'onyx_billshield_worker'"
        )
        row = cur.fetchone()
        assert row is not None, "onyx_billshield_worker was not created"
        can_login, is_super, bypass_rls = row
        assert not can_login, "the group role can log in"
        assert not is_super and not bypass_rls

        cur.execute(
            """
            SELECT n.nspname || '.' || c.relname, a.privilege_type
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,
                           acldefault('r', c.relowner))) AS a
            WHERE c.relkind IN ('r', 'p')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND pg_get_userbyid(a.grantee) = 'onyx_billshield_worker'
            """
        )
        held: dict[str, set[str]] = {}
        for table, verb in cur.fetchall():
            held.setdefault(table, set()).add(verb)
        assert held == {}, (
            f"the worker holds TABLE-level privileges {held}; every one of its "
            "verbs is column-scoped, because a table-level verb authorizes "
            "every column the table has and every column added later")

        cur.execute(
            "SELECT n.nspname FROM pg_namespace n"
            " CROSS JOIN LATERAL aclexplode(n.nspacl) AS a"
            " WHERE pg_get_userbyid(a.grantee) = 'onyx_billshield_worker'"
            "   AND a.privilege_type = 'USAGE'"
        )
        usage = {r[0] for r in cur.fetchall()}
        assert usage == {"billshield", "ref", "identity"}, (
            f"worker schema USAGE is {sorted(usage)}; `ref` is name resolution "
            "for ref.current_app_user() and `identity` for the deletion-cutoff "
            "function. Anything else means the shared fourteen-schema grant was "
            "edited")

        cur.execute(
            "SELECT count(*) FROM pg_namespace n"
            " CROSS JOIN LATERAL aclexplode(n.nspacl) AS a"
            " WHERE pg_get_userbyid(a.grantee) = 'onyx_billshield_worker'"
            "   AND a.privilege_type = 'CREATE'"
        )
        assert cur.fetchone()[0] == 0, "the worker group role can create objects"

        # Column ACLs: invisible to a table-level query, and the worker's ENTIRE
        # capability lives here. Compared as one set so an extra column, a
        # missing one, and an extra table all fail the same assertion.
        cur.execute(
            """
            SELECT n.nspname || '.' || c.relname, att.attname, a.privilege_type
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute att ON att.attrelid = c.oid AND att.attnum > 0
            CROSS JOIN LATERAL aclexplode(att.attacl) AS a
            WHERE att.attacl IS NOT NULL
              AND pg_get_userbyid(a.grantee) = 'onyx_billshield_worker'
            """
        )
        actual_columns = {(t, c, v) for t, c, v in cur.fetchall()}
        expected_columns = {
            (table, column, verb)
            for table, verbs in WORKER_COLUMN_ACL.items()
            for verb, columns in verbs.items()
            for column in columns
        }
        assert actual_columns == expected_columns, (
            f"unexpected: {sorted(actual_columns - expected_columns)}; "
            f"missing: {sorted(expected_columns - actual_columns)}")
        assert {t for t, _, _ in actual_columns} == set(WORKER_TABLE_VERBS), (
            "the worker's column grants reach a table outside the four it may "
            "touch")

        # Sequences: none exist in this schema today (UUID keys), but a future
        # serial column must not quietly arrive pre-granted.
        cur.execute(
            """
            SELECT n.nspname || '.' || c.relname, a.privilege_type
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,
                           acldefault('S', c.relowner))) AS a
            WHERE c.relkind = 'S'
              AND pg_get_userbyid(a.grantee) = 'onyx_billshield_worker'
            """
        )
        assert cur.fetchall() == [], "the worker group role holds sequence privileges"

        # Function EXECUTE, direct only. One entry, and it is the deletion
        # cutoff: the claim unit of work sets no tenant context, so a DIRECT
        # read of the lifecycle table would be hidden by its policy and would
        # let every task through. The BillShield keyholes are still Slice 3C's.
        cur.execute(
            """
            SELECT n.nspname || '.' || p.proname, a.privilege_type
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,
                           acldefault('f', p.proowner))) AS a
            WHERE pg_get_userbyid(a.grantee) = 'onyx_billshield_worker'
            """
        )
        assert cur.fetchall() == [
            ("identity.account_deletion_state", "EXECUTE")
        ], "the worker's direct EXECUTE set is not exactly the deletion cutoff"

        # Role membership. It still inherits NOTHING — a capability role that
        # inherited another would launder authority in one GRANT. What it may
        # now carry is members: the login that assumes it. None of them may be
        # a request-path or other-worker principal, which would be PD-16.
        cur.execute(
            "SELECT pg_get_userbyid(m.roleid) FROM pg_auth_members m"
            " WHERE pg_get_userbyid(m.member) = 'onyx_billshield_worker'"
        )
        assert cur.fetchall() == [], (
            "the worker group role inherits another role's privileges")
        cur.execute(
            "SELECT pg_get_userbyid(m.member) FROM pg_auth_members m"
            " WHERE pg_get_userbyid(m.roleid) = 'onyx_billshield_worker'"
        )
        members = {r[0] for r in cur.fetchall()}
        assert not (members & {"onyx_app_rw", "onyx_app_ro", "onyx_privacy_worker",
                               "onyx_freshness_worker", "onyx_kb_admin",
                               "onyx_audit_writer"}), (
            f"the worker capability was granted to {sorted(members)}; granting "
            "it to the API principal is PD-16 by name")


def test_no_other_role_was_granted_anything_in_the_schema():
    """`onyx_app_ro`, `onyx_kb_admin`, `onyx_audit_writer` and everyone else.

    Discovered dynamically: the assertion is over the set of grantees found in
    the catalogue, not over a list of roles somebody remembered to check.
    """
    with owner_cursor() as cur:
        actual = _table_acl(cur)
        columns = _column_acl(cur)
        owner = _owner(cur)
    grantees = {g for grants in actual.values() for g in grants}
    grantees |= {g for grants in columns.values() for g in grants}
    assert grantees - {owner} == {"onyx_app_rw", "onyx_billshield_worker"}, (
        f"unexpected grantees in the billshield schema: {sorted(grantees - {owner})}")


# --------------------------------------------------- proof 2: real behaviour

def test_the_api_can_enqueue_a_valid_pending_intent(tenants):
    """The positive control for the whole enqueue boundary."""
    a, _ = tenants
    with as_role("onyx_app_rw", a.user_id) as cur:
        cur.execute(
            "INSERT INTO billshield.job_outbox (user_id, bill_id, task_code,"
            " dedupe_key) VALUES (%s, %s, 'EXTRACT_BILL', %s) RETURNING id",
            (a.user_id, a.spare_bill_id, str(uuid.uuid4())),
        )
        new_id = cur.fetchone()[0]
        assert new_id is not None
    with owner_cursor() as cur:
        cur.execute(
            "SELECT claim_state, attempts, claimed_by, claim_token, processed_at"
            " FROM billshield.job_outbox WHERE id = %s", (str(new_id),))
        # Rolled back with the transaction — the point is that the INSERT was
        # ACCEPTED, which the RETURNING above already proves.
        assert cur.fetchone() is None


@pytest.mark.parametrize(
    "column,value",
    [
        ("claim_state", "'claimed'"),
        ("claimed_by", "'worker-1'"),
        ("claim_token", "gen_random_uuid()"),
        ("claimed_at", "now()"),
        ("processed_at", "now()"),
        ("attempts", "3"),
    ],
)
def test_the_api_cannot_supply_any_operational_outbox_column(tenants, column, value):
    """Enqueue authority is not claim authority, proved column by column."""
    a, _ = tenants
    with as_role("onyx_app_rw", a.user_id) as cur:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege) as excinfo:
            cur.execute(
                f"INSERT INTO billshield.job_outbox (user_id, bill_id, task_code,"
                f" dedupe_key, {column}) VALUES (%s, %s, 'EXTRACT_BILL', %s, {value})",
                (a.user_id, a.spare_bill_id, str(uuid.uuid4())),
            )
        assert "permission denied" in str(excinfo.value).lower()


def test_the_api_cannot_update_or_delete_outbox_rows(tenants):
    """No claiming by UPDATE, and no un-queueing by DELETE."""
    a, _ = tenants
    for statement in (
        "UPDATE billshield.job_outbox SET claim_state = 'completed'",
        "DELETE FROM billshield.job_outbox",
    ):
        with as_role("onyx_app_rw", a.user_id) as cur:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(statement)


def test_the_api_cannot_read_the_outbox_beyond_its_own_id_grant(tenants):
    """`SELECT (id)` is the whole read grant, so a claim-state peek is refused."""
    a, _ = tenants
    with as_role("onyx_app_rw", a.user_id) as cur:
        cur.execute("SELECT id FROM billshield.job_outbox")   # allowed
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute("SELECT claim_state FROM billshield.job_outbox")


def test_the_global_catalogue_is_read_only_to_the_runtime(tenants):
    """Proved by traffic: SELECT works, every write is refused."""
    a, _ = tenants
    with as_role("onyx_app_rw", a.user_id) as cur:
        cur.execute("SELECT count(*) FROM billshield.provider")
        cur.execute("SELECT count(*) FROM billshield.provider_category")
    for statement in (
        "INSERT INTO billshield.provider (code, name) VALUES ('ACME', 'Acme')",
        "UPDATE billshield.provider SET name = 'x'",
        "DELETE FROM billshield.provider",
        "INSERT INTO billshield.provider_category (provider_id, category)"
        " VALUES (gen_random_uuid(), 'MOBILE')",
    ):
        with as_role("onyx_app_rw", a.user_id) as cur:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(statement)


@pytest.mark.parametrize("table", [
    "billshield.extraction_run",
    "billshield.charge_candidate",
    "billshield.promotion_candidate",
])
def test_the_api_cannot_write_extraction_results(tenants, table):
    """Those rows are the worker's to write, and the worker is Slice 3."""
    a, _ = tenants
    with as_role("onyx_app_rw", a.user_id) as cur:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute(f"UPDATE {table} SET created_at = now()")


def test_the_worker_group_role_is_refused_every_table_outside_its_allowlist(tenants):
    """Not just "holds no grant" — actually try, and be refused.

    The complement is computed rather than listed: every BillShield table the
    allowlist does not give the worker a SELECT on. So a later slice that adds a
    read has to add it to `WORKER_TABLE_VERBS`, where a reviewer sees it, rather
    than to a denial list where its absence is invisible.
    """
    a, _ = tenants
    readable = {t for t, verbs in WORKER_TABLE_VERBS.items() if "SELECT" in verbs}
    unreadable = [t for t in TENANT_TABLES + GLOBAL_TABLES if t not in readable]
    assert unreadable, "the complement is empty; this test would prove nothing"
    for table in unreadable:
        with as_role("onyx_billshield_worker", a.user_id) as cur:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(f"SELECT count(*) FROM {table}")


def test_the_worker_group_role_reads_exactly_the_columns_it_was_granted(tenants):
    """The positive control, and the column boundary in one test.

    Without the first half every refusal above would be equally consistent with
    a role whose grants were dropped entirely. Without the second half the
    grants could have been table-wide and every assertion here would still pass.
    """
    a, _ = tenants
    for table, verbs in WORKER_COLUMN_ACL.items():
        granted = verbs.get("SELECT")
        if not granted:
            continue
        columns = ", ".join(sorted(granted))
        with as_role("onyx_billshield_worker", a.user_id) as cur:
            cur.execute(f"SELECT {columns} FROM {table}")   # allowed
        with as_role("onyx_billshield_worker", a.user_id) as cur:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(f"SELECT * FROM {table}")       # refused


def test_the_privilege_enumeration_is_non_vacuous(tenants):
    """A planted grant must be caught, or the enumeration proves nothing.

    Granted inside a transaction that is always rolled back, so the plant never
    outlives the test.
    """
    conn = psycopg2.connect(__import__("tests.conftest", fromlist=["owner_dsn"]).owner_dsn())
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute("GRANT DELETE ON billshield.bill TO onyx_app_rw")
        actual = _table_acl(cur)
        assert "DELETE" in actual["billshield.bill"]["onyx_app_rw"], (
            "the enumeration cannot see a grant made in this transaction — it "
            "would not see a real one either")
        # A role that holds NOTHING in this schema at either scope, so the plant
        # is unambiguously a new grantee. `onyx_billshield_worker` would be a
        # weaker choice: it already holds column-scoped SELECT on this table, so
        # a reader could not tell whether the enumeration saw the plant or the
        # real grant.
        cur.execute("GRANT SELECT ON billshield.bill TO onyx_app_ro")
        actual = _table_acl(cur)
        assert "onyx_app_ro" in actual["billshield.bill"], (
            "the enumeration missed a new grantee")
    finally:
        conn.rollback()
        conn.close()


# ------------------------------- proof 2 (continued): the trigger owns updated_at

def test_the_api_cannot_write_updated_at_explicitly(tenants):
    """The column ACL allowlist says the grant is absent; this proves the effect.

    A runtime that could set this column could backdate its own edit, which is
    why `ref.set_updated_at()` owns it. Asserted by attempting the write as the
    API principal and being refused by PostgreSQL, and then by confirming the
    stored value did not move — a refusal that still changed the row would be
    the worst of both.
    """
    a, _ = tenants
    with owner_cursor() as cur:
        cur.execute("SELECT updated_at FROM billshield.bill WHERE id = %s",
                    (str(a.bill_id),))
        before = cur.fetchone()[0]

    with as_role("onyx_app_rw", a.user_id) as cur:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege) as excinfo:
            cur.execute(
                "UPDATE billshield.bill SET updated_at = now() WHERE id = %s",
                (str(a.bill_id),))
        assert "permission denied" in str(excinfo.value).lower()

    with owner_cursor() as cur:
        cur.execute("SELECT updated_at FROM billshield.bill WHERE id = %s",
                    (str(a.bill_id),))
        assert cur.fetchone()[0] == before, "updated_at moved on a refused write"


def test_an_allowed_update_advances_updated_at_through_the_trigger(tenants):
    """The other half: withholding the grant must not stop the column working.

    Read, write an ALLOWED column, read again — all in one transaction that is
    rolled back, so the fixture survives and no sleep is involved.

    `ref.set_updated_at()` assigns `now()`, which is TRANSACTION start time, not
    statement time (`db/sql/00_extensions_roles.sql:81-89`). So the honest
    assertion is not merely "later than before" — it is that the stored value
    now EQUALS this transaction's timestamp, which it can only do if the trigger
    fired during this transaction. Had the trigger not fired, the column would
    still hold the seed transaction's earlier value, and both assertions below
    would fail.
    """
    a, _ = tenants
    with as_role("onyx_app_rw", a.user_id) as cur:
        cur.execute("SELECT updated_at, row_version, transaction_timestamp()"
                    " FROM billshield.bill WHERE id = %s", (str(a.bill_id),))
        before, row_version, txn_time = cur.fetchone()
        assert before < txn_time, (
            "the fixture row was written in this transaction; the test cannot "
            "distinguish a trigger stamp from the seed value")

        cur.execute(
            "UPDATE billshield.bill SET row_version = %s WHERE id = %s",
            (row_version + 1, str(a.bill_id)))
        assert cur.rowcount == 1, "the allowed update did not reach the row"

        cur.execute("SELECT updated_at, row_version FROM billshield.bill"
                    " WHERE id = %s", (str(a.bill_id),))
        after, new_version = cur.fetchone()

    assert new_version == row_version + 1, "the allowed column did not change"
    assert after > before, (
        f"updated_at did not advance ({before} -> {after}); the trigger is not "
        "stamping the column the API is forbidden to write")
    assert after == txn_time, (
        f"updated_at is {after}, not this transaction's timestamp {txn_time} — "
        "something other than ref.set_updated_at() wrote it")
