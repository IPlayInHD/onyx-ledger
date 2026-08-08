"""PD-1 — what the new policies must not break (Entry 11B1).

Three things were closed before this entry and have to stay closed: the audit
payload minimization from 11B0, the account-deletion orchestration from 11B2,
and the relational integrity the policies now depend on.

The last one is the subtle one. Every policy resolves ownership through a parent
pointer, so "this child has exactly one owner" stopped being a modelling nicety
and became the thing tenant isolation rests on. An orphaned child would be
invisible to everyone — including a future purge — rather than dangerous, but a
child whose parent chain does not resolve to exactly one account is a hole.
"""
from __future__ import annotations

import contextlib
import uuid

import psycopg2
import pytest

from tests.conftest import owner_dsn
from tests.security.test_pd1_tenant_isolation import Tenant, runtime_cursor

PASSWORD = "supersecret1"


@contextlib.contextmanager
def owner_cursor():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    try:
        yield conn.cursor()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# relational integrity the policies now depend on (§29, §30)
# ---------------------------------------------------------------------------
#: child, its parent pointer, parent table. Every one of these FKs is NOT NULL
#: and enforced, which is *why* the policies can be written this way — the
#: check exists so that stops being an assumption.
_OWNERSHIP = (
    ("ai.ai_message", "conversation_id", "ai.ai_conversation"),
    ("ai.ai_message_citation", "message_id", "ai.ai_message"),
    ("ai.ai_prompt_context", "message_id", "ai.ai_message"),
    ("analysis.analysis_assumption", "analysis_id", "analysis.analysis_run"),
    ("analysis.analysis_input_snapshot", "analysis_id", "analysis.analysis_run"),
    ("analysis.analysis_line_item", "analysis_id", "analysis.analysis_run"),
    ("analysis.reconciliation_check", "analysis_id", "analysis.analysis_run"),
    ("billing.invoice", "subscription_id", "billing.subscription"),
    ("docs.document_extraction", "document_id", "docs.document"),
    ("docs.document_link", "document_id", "docs.document"),
    ("docs.extraction_field", "extraction_id", "docs.document_extraction"),
    ("reco.recommendation_status_event", "recommendation_id",
     "reco.recommendation"),
    ("wealth.asset_valuation", "asset_id", "wealth.asset"),
    ("wealth.liability_balance", "liability_id", "wealth.liability"),
    ("wealth.registered_account_detail", "asset_id", "wealth.asset"),
)


@pytest.mark.parametrize("child,column,parent", _OWNERSHIP)
def test_the_ownership_pointer_is_not_nullable(child, column, parent):
    """§30 — relational integrity, not runtime RLS, is what keeps a child
    attached to exactly one owner.

    A nullable parent pointer would make the policy predicate silently false
    for those rows: invisible to their owner, invisible to a purge, and
    reachable only by a superuser. Enforced by the column, so this asserts the
    column rather than adding a policy to compensate.
    """
    with owner_cursor() as cur:
        cur.execute("""
            SELECT a.attnotnull FROM pg_attribute a
             WHERE a.attrelid = %s::regclass AND a.attname = %s
        """, (child, column))
        assert cur.fetchone()[0] is True, (
            f"{child}.{column} became nullable; rows with a NULL owner would "
            "be invisible to their owner and to any future purge"
        )


@pytest.mark.parametrize("child,column,parent", _OWNERSHIP)
def test_no_child_row_is_orphaned_from_its_owner(child, column, parent):
    """Counts only — never a value. Zero is the expected answer because every
    one of these FKs is enforced; the check exists so a future schema change
    that weakens one is noticed here rather than during a purge."""
    with owner_cursor() as cur:
        cur.execute(f"""
            SELECT count(*) FROM {child} c
              LEFT JOIN {parent} p ON p.id = c.{column}
             WHERE p.id IS NULL
        """)  # noqa: S608
        assert cur.fetchone()[0] == 0, f"{child} has rows with no {parent}"


def test_run_rule_snapshot_has_exactly_one_owner():
    """The two-branch case. The table CHECKs `num_nonnulls(run_id,
    scenario_id) = 1`, so exactly one branch resolves — a row with neither
    would match no policy and a row with both would have two owners."""
    with owner_cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM ioe.run_rule_snapshot
             WHERE num_nonnulls(run_id, scenario_id) <> 1
        """)
        assert cur.fetchone()[0] == 0
        cur.execute("""
            SELECT count(*) FROM ioe.run_rule_snapshot x
              LEFT JOIN ioe.optimization_run r ON r.id = x.run_id
              LEFT JOIN ioe.scenario s ON s.id = x.scenario_id
             WHERE r.id IS NULL AND s.id IS NULL
        """)
        assert cur.fetchone()[0] == 0, "a snapshot resolves to no owner at all"


# ---------------------------------------------------------------------------
# billing.invoice — the one table PD-1 and PD-4 both touch (§11)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def billing_tenants():
    with owner_cursor() as cur:
        return Tenant(cur), Tenant(cur)


def test_an_invoice_is_tenant_isolated_and_still_audited_without_values(
    billing_tenants,
):
    """§11 — Entry 11B0 found that `billing.invoice` is the only table in both
    the audited set and the PD-1 set. The two controls have to hold together:
    RLS keeps one tenant's invoice away from another, and the audit trigger
    must still record the write without copying the amount into a store with no
    owner and no delete path.
    """
    a, b = billing_tenants

    # RLS: B's invoice is not reachable from A's session.
    with runtime_cursor(a.user_id) as cur:
        cur.execute("SELECT count(*) FROM billing.invoice WHERE id = %s",
                    (b.rows["billing.invoice"],))
        assert cur.fetchone()[0] == 0, "an invoice crossed the tenant boundary"

    # PD-4: the audit row for A's own invoice exists, names its columns, and
    # carries no value.
    with owner_cursor() as cur:
        cur.execute(
            "SELECT action, new_value FROM audit.audit_log "
            " WHERE entity_table = 'invoice' AND entity_id = %s",
            (a.rows["billing.invoice"],))
        rows = cur.fetchall()

    assert rows, "the invoice write was not audited"
    for action, payload in rows:
        assert payload is not None, f"the audited {action} lost its payload"
        assert "amount" in payload, "the audited payload lost its column list"
        assert payload["amount"] == "[redacted]", (
            f"the audited {action} carries the invoice amount; PD-4 has "
            "regressed on the one table PD-1 and PD-4 share"
        )
        assert payload.get("subscription_id") not in (None, "[redacted]"), (
            "the ownership pointer was redacted, so a future de-identification "
            "phase could no longer attribute this audit row"
        )


def test_enabling_rls_did_not_change_what_the_audit_log_stores():
    """§25 — the audit trigger is SECURITY DEFINER and runs as the migrator, so
    it is subject to FORCE RLS on the tables it reads from. If a policy had
    broken it, writes would fail rather than silently under-record — this
    asserts the payload shape is unchanged for a table that just gained RLS."""
    with owner_cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM audit.audit_log
             WHERE entity_table = 'invoice'
               AND new_value IS NOT NULL
               AND NOT (new_value ? 'amount')
        """)
        assert cur.fetchone()[0] == 0, (
            "invoice audit rows lost their column list after RLS was enabled"
        )


# ---------------------------------------------------------------------------
# account lifecycle (§12)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_deletion_cutoff_still_applies_after_rls(client):
    """§12 — Entry 11B2 is closed and must stay closed.

    The lifecycle tables are in `identity`, which PD-1 deliberately did not
    touch, but the cutoff is enforced on every authenticated route — including
    the ones that now read RLS-protected children. A policy that broke a read
    path would show up here as the wrong status code.
    """
    email = f"pd1life_{uuid.uuid4().hex[:10]}@example.com"
    assert (await client.post("/api/v1/auth/register",
                              json={"email": email, "password": PASSWORD})
            ).status_code == 201
    login = await client.post("/api/v1/auth/login",
                              json={"email": email, "password": PASSWORD})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Reads that traverse the newly protected children still work while active.
    assert (await client.get("/api/v1/analysis", headers=headers)).status_code == 200
    assert (await client.get("/api/v1/documents", headers=headers)).status_code == 200

    assert (await client.post("/api/v1/account/deletion",
                              headers=headers)).status_code == 202

    # And stop working after the cutoff, for the lifecycle's reason rather than
    # for an RLS one.
    for path in ("/api/v1/analysis", "/api/v1/documents", "/api/v1/users/me"):
        response = await client.get(path, headers=headers)
        assert response.status_code == 403, f"{path} -> {response.status_code}"


def test_the_lifecycle_worker_gained_no_cross_tenant_table_access():
    """§12 — the privacy worker must not have been handed broad access to the
    newly protected tables just because a future purge will want it.

    It keeps exactly what Entry 11B2 gave it: USAGE on `identity` and EXECUTE
    on three lifecycle functions. Its purge boundary gets designed when the
    purge is written.
    """
    with owner_cursor() as cur:
        cur.execute("""
            SELECT c.relnamespace::regnamespace::text || '.' || c.relname
              FROM pg_class c
             WHERE c.relkind IN ('r', 'p')
               AND c.relnamespace::regnamespace::text
                   IN ('ai','analysis','billing','docs','ioe','reco','wealth')
               AND (has_table_privilege('onyx_privacy_worker', c.oid, 'SELECT')
                 OR has_table_privilege('onyx_privacy_worker', c.oid, 'DELETE'))
        """)
        reachable = [r[0] for r in cur.fetchall()]
    assert not reachable, (
        f"the privacy worker can already reach {reachable}; purge authority "
        "should be granted by the entry that implements the purge"
    )
