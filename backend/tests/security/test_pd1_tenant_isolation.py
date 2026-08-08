"""PD-1 — tenant isolation at the database boundary (Entry 11B1).

THE DEFECT, as Entry 11A recorded it (§23, gap register):

    16 tenant-owned child tables have no RLS, including the frozen snapshot,
    extraction fields, AI messages and AI prompt context. (27 user-derived
    tables lack RLS in total; 11 of those — `identity` and `audit` — correctly
    cannot have it.)

Entry 11A was careful to say this is *not* a demonstrated leak: every service
scopes through the parent, and no API path reaches these tables unscoped. It is
the absence of the boundary the repository elsewhere treats as load-bearing —
"RLS is the tenant-correctness boundary, not the query access path" — so one
careless future query, or one privileged deletion phase, crosses tenants
silently.

WHY THESE TESTS DO NOT GO THROUGH THE API
An API test proves the service remembered to scope its query. That is exactly
the property PD-1 says we should stop relying on. These tests become the runtime
role, set the tenant GUC the application sets, and issue SQL directly at the
table — which is the boundary PD-1 is about.

EVERY table is covered rather than one per policy family. The families differ
(depth-1 parent, depth-2 grandparent, two-branch ownership) and a table left out
is a table with no boundary.
"""
from __future__ import annotations

import contextlib
import uuid

import psycopg2
import pytest

from tests.conftest import owner_dsn

#: The 16 tables PD-1 names, with the parent chain that establishes ownership.
#: Ordered so a reader can see the three shapes: direct parent, grandparent,
#: and the two-branch case.
PD1_TABLES = (
    "ai.ai_message",
    "ai.ai_message_citation",
    "ai.ai_prompt_context",
    "analysis.analysis_assumption",
    "analysis.analysis_input_snapshot",
    "analysis.analysis_line_item",
    "analysis.reconciliation_check",
    "billing.invoice",
    "docs.document_extraction",
    "docs.document_link",
    "docs.extraction_field",
    "ioe.run_rule_snapshot",
    "reco.recommendation_status_event",
    "wealth.asset_valuation",
    "wealth.liability_balance",
    "wealth.registered_account_detail",
)


#: Not every table is keyed on `id`. Two are one-row-per-parent and keyed on
#: the parent itself, which also means a forged INSERT aimed at a parent that
#: already has its row hits the PRIMARY KEY before it ever reaches the policy —
#: so the forgery tests aim at a spare parent instead.
PK_COLUMN = {
    "analysis.analysis_input_snapshot": "analysis_id",
    "wealth.registered_account_detail": "asset_id",
}


def pk(table: str) -> str:
    return PK_COLUMN.get(table, "id")


@contextlib.contextmanager
def owner_cursor():
    """Seeds data. The owner is a superuser here, so it can write both tenants'
    rows without the policies it is about to test getting in the way."""
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    try:
        yield conn.cursor()
    finally:
        conn.close()


@contextlib.contextmanager
def runtime_cursor(user_id: uuid.UUID | None):
    """A cursor that IS the runtime role, with the tenant GUC the app sets.

    `onyx_app_rw` is NOLOGIN, so `SET ROLE` is how a test can act as it. The
    GUC is set the same way `unit_of_work` sets it — transaction-local — so the
    policies see exactly what they see in production.
    """
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = False
    try:
        cur = conn.cursor()
        if user_id is not None:
            cur.execute("SELECT set_config('app.user_id', %s, true)",
                        (str(user_id),))
        cur.execute("SET ROLE onyx_app_rw")
        yield cur
    finally:
        conn.rollback()
        conn.close()


class Tenant:
    """One user's row in every PD-1 table, plus the parents that own them."""

    def __init__(self, cur) -> None:
        self.user_id = uuid.uuid4()
        cur.execute(
            "INSERT INTO identity.user_account (id, email, status) "
            "VALUES (%s, %s, 'active')",
            (str(self.user_id), f"pd1_{uuid.uuid4().hex[:10]}@example.com"))
        self.rows: dict[str, str] = {}
        self._seed(cur)

    def _one(self, cur, sql: str, params: tuple, returning: str = "id") -> str:
        cur.execute(f"{sql} RETURNING {returning}", params)
        return cur.fetchone()[0]

    def _seed(self, cur) -> None:
        uid = str(self.user_id)

        # --- ai: conversation → message → citation / prompt_context ---------
        conversation = self._one(
            cur, "INSERT INTO ai.ai_conversation (user_id) VALUES (%s)", (uid,))
        message = self._one(
            cur, "INSERT INTO ai.ai_message (conversation_id, role, content) "
                 "VALUES (%s, 'user', %s)", (conversation, "marker question"))
        self.rows["ai.ai_message"] = message
        # A citation must point at something it cites — the table CHECKs that
        # at least one of tax_rule_version_id / analysis_id / recommendation_id
        # is set. The analysis is created below, so this row is filled in after
        # it exists; see `_seed_citation`.
        self._citation_message = message
        self.rows["ai.ai_prompt_context"] = self._one(
            cur, "INSERT INTO ai.ai_prompt_context (message_id, context) "
                 "VALUES (%s, '{\"marker\": true}'::jsonb)", (message,))

        # --- analysis: run → four children ----------------------------------
        run = self._one(
            cur, "INSERT INTO analysis.analysis_run "
                 "(user_id, tax_year, engine_version) VALUES (%s, 2025, '1.0')",
            (uid,))
        self.analysis_id = run
        self.rows["analysis.analysis_assumption"] = self._one(
            cur, "INSERT INTO analysis.analysis_assumption (analysis_id, text) "
                 "VALUES (%s, 'marker')", (run,))
        self.rows["analysis.analysis_input_snapshot"] = self._one(
            cur, "INSERT INTO analysis.analysis_input_snapshot "
                 "(analysis_id, snapshot, snapshot_hash) "
                 "VALUES (%s, '{\"marker\": 1}'::jsonb, %s)",
            (run, uuid.uuid4().hex), returning="analysis_id")

        # A second run with NO snapshot, so the forgery test has somewhere to
        # aim that the primary key does not already occupy.
        self.spare_analysis_id = self._one(
            cur, "INSERT INTO analysis.analysis_run "
                 "(user_id, tax_year, engine_version) VALUES (%s, 2024, '1.0')",
            (uid,))
        self.rows["analysis.analysis_line_item"] = self._one(
            cur, "INSERT INTO analysis.analysis_line_item "
                 "(analysis_id, kind, label, amount) "
                 "VALUES (%s, 'income', 'marker', 1)", (run,))
        self.rows["analysis.reconciliation_check"] = self._one(
            cur, "INSERT INTO analysis.reconciliation_check "
                 "(analysis_id, check_code, status, label) "
                 "VALUES (%s, 'MARKER', 'pass', 'marker')", (run,))

        self.rows["ai.ai_message_citation"] = self._one(
            cur, "INSERT INTO ai.ai_message_citation (message_id, analysis_id) "
                 "VALUES (%s, %s)", (self._citation_message, run))

        # --- billing: subscription → invoice --------------------------------
        cur.execute("SELECT id FROM billing.plan LIMIT 1")
        plan = cur.fetchone()
        if plan is None:
            cur.execute(
                "INSERT INTO billing.plan (code, name) "
                "VALUES (%s, 'Marker') RETURNING id",
                (f"marker_{uuid.uuid4().hex[:8]}",))
            plan = cur.fetchone()
        subscription = self._one(
            cur, "INSERT INTO billing.subscription (user_id, plan_id) "
                 "VALUES (%s, %s)", (uid, plan[0]))
        self.rows["billing.invoice"] = self._one(
            cur, "INSERT INTO billing.invoice (subscription_id, amount) "
                 "VALUES (%s, 1)", (subscription,))

        # --- docs: document → extraction → field, and document_link ---------
        document = self._one(
            cur, "INSERT INTO docs.document (user_id, bucket, object_key) "
                 "VALUES (%s, 'marker', %s)", (uid, uuid.uuid4().hex))
        extraction = self._one(
            cur, "INSERT INTO docs.document_extraction (document_id, engine) "
                 "VALUES (%s, 'marker')", (document,))
        self.rows["docs.document_extraction"] = extraction
        # A link must point at the financial row it evidences — the table
        # CHECKs that an income or expense reference and its tax year are both
        # present. `document_id` is still the authoritative ownership path;
        # this is the row that makes the link legal.
        cur.execute("SELECT id FROM ref.income_type LIMIT 1")
        income_type = cur.fetchone()[0]
        self.income_id = self._one(
            cur, "INSERT INTO finance.income_source "
                 "(user_id, tax_year, income_type_id, amount) "
                 "VALUES (%s, 2025, %s, 1)", (uid, income_type))
        self.rows["docs.document_link"] = self._one(
            cur, "INSERT INTO docs.document_link "
                 "(document_id, income_source_id, income_tax_year) "
                 "VALUES (%s, %s, 2025)", (document, self.income_id))
        self.rows["docs.extraction_field"] = self._one(
            cur, "INSERT INTO docs.extraction_field (extraction_id, field_name) "
                 "VALUES (%s, 'marker')", (extraction,))
        self.document_id = document

        # --- ioe: optimization_run → run_rule_snapshot ----------------------
        snapshot = self._one(
            cur, "INSERT INTO ioe.rule_snapshot (snapshot_hash) VALUES (%s)",
            (uuid.uuid4().hex,))
        optimization = self._one(
            cur, "INSERT INTO ioe.optimization_run "
                 "(user_id, analysis_id, tax_year) VALUES (%s, %s, 2025)",
            (uid, run))
        self.optimization_id = optimization
        self.rows["ioe.run_rule_snapshot"] = self._one(
            cur, "INSERT INTO ioe.run_rule_snapshot (run_id, snapshot_id) "
                 "VALUES (%s, %s)", (optimization, snapshot))

        # --- reco: recommendation → status_event ----------------------------
        recommendation = self._one(
            cur, "INSERT INTO reco.recommendation "
                 "(analysis_id, user_id, opportunity_code, title) "
                 "VALUES (%s, %s, 'MARKER', 'marker')", (run, uid))
        self.recommendation_id = recommendation
        self.rows["reco.recommendation_status_event"] = self._one(
            cur, "INSERT INTO reco.recommendation_status_event "
                 "(recommendation_id, status) VALUES (%s, 'new')",
            (recommendation,))

        # --- wealth: asset → valuation / registered detail, liability → balance
        cur.execute("SELECT id FROM ref.asset_category LIMIT 1")
        asset_category = cur.fetchone()[0]
        asset = self._one(
            cur, "INSERT INTO wealth.asset (user_id, asset_category_id, label) "
                 "VALUES (%s, %s, 'marker')", (uid, asset_category))
        self.asset_id = asset
        self.rows["wealth.asset_valuation"] = self._one(
            cur, "INSERT INTO wealth.asset_valuation "
                 "(asset_id, as_of_date, value) VALUES (%s, current_date, 1)",
            (asset,))
        cur.execute("SELECT code FROM ref.account_registered_type LIMIT 1")
        registered_type = cur.fetchone()[0]
        self.rows["wealth.registered_account_detail"] = self._one(
            cur, "INSERT INTO wealth.registered_account_detail "
                 "(asset_id, registered_type, tax_year) VALUES (%s, %s, 2025)",
            (asset, registered_type), returning="asset_id")
        self.registered_type = registered_type

        # A second asset with no registered detail, for the same reason.
        self.spare_asset_id = self._one(
            cur, "INSERT INTO wealth.asset (user_id, asset_category_id, label) "
                 "VALUES (%s, %s, 'marker spare')", (uid, asset_category))

        cur.execute("SELECT id FROM ref.liability_category LIMIT 1")
        liability_category = cur.fetchone()[0]
        liability = self._one(
            cur, "INSERT INTO wealth.liability "
                 "(user_id, liability_category_id) VALUES (%s, %s)",
            (uid, liability_category))
        self.liability_id = liability
        self.rows["wealth.liability_balance"] = self._one(
            cur, "INSERT INTO wealth.liability_balance "
                 "(liability_id, as_of_date, balance) "
                 "VALUES (%s, current_date, 1)", (liability,))


@pytest.fixture(scope="module")
def tenants():
    """Two tenants, seeded once. Read-only for the isolation tests, which never
    successfully mutate anything."""
    with owner_cursor() as cur:
        return Tenant(cur), Tenant(cur)


def test_the_fixture_covers_every_pd1_table(tenants):
    """Guards every parametrized test below: a fixture that silently failed to
    seed a table would make its isolation test pass against nothing."""
    a, _ = tenants
    missing = set(PD1_TABLES) - set(a.rows)
    assert not missing, f"the fixture seeds no row for {sorted(missing)}"
    assert len(a.rows) == 16


# ---------------------------------------------------------------------------
# cross-tenant read
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("table", PD1_TABLES)
def test_one_tenant_cannot_read_anothers_rows(tenants, table):
    """§17 — SELECT B's row as A, directly at the table."""
    a, b = tenants
    with runtime_cursor(a.user_id) as cur:
        cur.execute(f"SELECT count(*) FROM {table} WHERE {pk(table)} = %s",  # noqa: S608
                    (b.rows[table],))
        visible = cur.fetchone()[0]
    assert visible == 0, (
        f"{table}: tenant A can read tenant B's row directly. RLS is the "
        "tenant-correctness boundary; a service remembering to scope its "
        "query is not."
    )


@pytest.mark.parametrize("table", PD1_TABLES)
def test_a_tenant_can_still_read_its_own_rows(tenants, table):
    """The other half. A policy that denies everything is not isolation, it is
    an outage — and would pass every test above."""
    a, _ = tenants
    with runtime_cursor(a.user_id) as cur:
        cur.execute(f"SELECT count(*) FROM {table} WHERE {pk(table)} = %s",  # noqa: S608
                    (a.rows[table],))
        visible = cur.fetchone()[0]
    assert visible == 1, f"{table}: tenant A cannot see its own row"


@pytest.mark.parametrize("table", PD1_TABLES)
def test_an_anonymous_session_sees_nothing(tenants, table):
    """`app.user_id` unset — the shape of a login or worker session.
    `ref.current_app_user()` returns NULL, and NULL = anything is never true,
    so deny-by-default falls out of the predicate rather than needing a rule."""
    a, _ = tenants
    with runtime_cursor(None) as cur:
        cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608
        assert cur.fetchone()[0] == 0, (
            f"{table}: a session with no tenant context can read rows"
        )


# ---------------------------------------------------------------------------
# cross-tenant write
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("table", PD1_TABLES)
def test_one_tenant_cannot_update_anothers_rows(tenants, table):
    """§17 — UPDATE must not reach across the boundary either. Asserted on rows
    AFFECTED: a policy that filters the UPDATE reports zero, which is the
    intended outcome for a table the role legitimately holds UPDATE on."""
    a, b = tenants
    with runtime_cursor(a.user_id) as cur:
        cur.execute(
            f"UPDATE {table} SET created_at = created_at "  # noqa: S608
            f"WHERE {pk(table)} = %s",
            (b.rows[table],))
        assert cur.rowcount == 0, (
            f"{table}: tenant A updated tenant B's row"
        )


@pytest.mark.parametrize("table", PD1_TABLES)
def test_one_tenant_cannot_delete_anothers_rows(tenants, table):
    """§17 — and DELETE."""
    a, b = tenants
    with runtime_cursor(a.user_id) as cur:
        cur.execute(f"DELETE FROM {table} WHERE {pk(table)} = %s",  # noqa: S608
                    (b.rows[table],))
        assert cur.rowcount == 0, (
            f"{table}: tenant A deleted tenant B's row"
        )


# ---------------------------------------------------------------------------
# ownership forgery on write (§18)
# ---------------------------------------------------------------------------
#: How to attempt an INSERT into each table hanging off ANOTHER tenant's parent.
#: This is the case a USING-only policy misses entirely: reads are protected and
#: a caller can still write rows into somebody else's tree.
def _forged_inserts(a: Tenant, b: Tenant) -> dict[str, tuple[str, tuple]]:
    return {
        "ai.ai_message": (
            "INSERT INTO ai.ai_message (conversation_id, role, content) "
            "SELECT conversation_id, 'user', 'forged' FROM ai.ai_message "
            " WHERE id = %s", (b.rows["ai.ai_message"],)),
        # Cites A's OWN analysis but hangs off B's message: the forgery is the
        # parent pointer, not the cited object.
        "ai.ai_message_citation": (
            "INSERT INTO ai.ai_message_citation (message_id, analysis_id) "
            "VALUES (%s, %s)", (b.rows["ai.ai_message"], a.analysis_id)),
        "ai.ai_prompt_context": (
            "INSERT INTO ai.ai_prompt_context (message_id, context) "
            "VALUES (%s, '{}'::jsonb)", (b.rows["ai.ai_message"],)),
        "analysis.analysis_assumption": (
            "INSERT INTO analysis.analysis_assumption (analysis_id, text) "
            "VALUES (%s, 'forged')", (b.analysis_id,)),
        "analysis.analysis_input_snapshot": (
            "INSERT INTO analysis.analysis_input_snapshot "
            "(analysis_id, snapshot, snapshot_hash) "
            "VALUES (%s, '{}'::jsonb, 'forged')", (b.spare_analysis_id,)),
        "analysis.analysis_line_item": (
            "INSERT INTO analysis.analysis_line_item "
            "(analysis_id, kind, label, amount) "
            "VALUES (%s, 'income', 'forged', 1)", (b.analysis_id,)),
        "analysis.reconciliation_check": (
            "INSERT INTO analysis.reconciliation_check "
            "(analysis_id, check_code, status, label) "
            "VALUES (%s, 'FORGED', 'pass', 'forged')", (b.analysis_id,)),
        "billing.invoice": (
            "INSERT INTO billing.invoice (subscription_id, amount) "
            "SELECT subscription_id, 1 FROM billing.invoice WHERE id = %s",
            (b.rows["billing.invoice"],)),
        "docs.document_extraction": (
            "INSERT INTO docs.document_extraction (document_id, engine) "
            "VALUES (%s, 'forged')", (b.document_id,)),
        "docs.document_link": (
            "INSERT INTO docs.document_link "
            "(document_id, income_source_id, income_tax_year) "
            "VALUES (%s, %s, 2025)", (b.document_id, a.income_id)),
        "docs.extraction_field": (
            "INSERT INTO docs.extraction_field (extraction_id, field_name) "
            "VALUES (%s, 'forged')", (b.rows["docs.document_extraction"],)),
        "ioe.run_rule_snapshot": (
            "INSERT INTO ioe.run_rule_snapshot (run_id, snapshot_id) "
            "SELECT %s, snapshot_id FROM ioe.run_rule_snapshot WHERE id = %s",
            (b.optimization_id, b.rows["ioe.run_rule_snapshot"])),
        "reco.recommendation_status_event": (
            "INSERT INTO reco.recommendation_status_event "
            "(recommendation_id, status) VALUES (%s, 'dismissed')",
            (b.recommendation_id,)),
        # A distinct as_of_date, so a unique-constraint collision cannot be
        # mistaken for the policy doing its job.
        "wealth.asset_valuation": (
            "INSERT INTO wealth.asset_valuation (asset_id, as_of_date, value) "
            "VALUES (%s, current_date - 30, 1)", (b.asset_id,)),
        "wealth.liability_balance": (
            "INSERT INTO wealth.liability_balance "
            "(liability_id, as_of_date, balance) "
            "VALUES (%s, current_date - 30, 1)", (b.liability_id,)),
        "wealth.registered_account_detail": (
            "INSERT INTO wealth.registered_account_detail "
            "(asset_id, registered_type, tax_year) VALUES (%s, %s, 2099)",
            (b.spare_asset_id, a.registered_type)),
    }


#: Where a forged row would land if it succeeded, so the test can assert the
#: OUTCOME rather than which mechanism refused it.
_TREE_COUNT = {
    "ai.ai_message":
        ("SELECT count(*) FROM ai.ai_message m JOIN ai.ai_conversation c "
         "  ON c.id = m.conversation_id WHERE c.user_id = %s"),
    "ai.ai_message_citation":
        ("SELECT count(*) FROM ai.ai_message_citation x JOIN ai.ai_message m "
         "  ON m.id = x.message_id JOIN ai.ai_conversation c "
         "  ON c.id = m.conversation_id WHERE c.user_id = %s"),
    "ai.ai_prompt_context":
        ("SELECT count(*) FROM ai.ai_prompt_context x JOIN ai.ai_message m "
         "  ON m.id = x.message_id JOIN ai.ai_conversation c "
         "  ON c.id = m.conversation_id WHERE c.user_id = %s"),
    "analysis.analysis_assumption":
        ("SELECT count(*) FROM analysis.analysis_assumption x "
         "  JOIN analysis.analysis_run r ON r.id = x.analysis_id "
         " WHERE r.user_id = %s"),
    "analysis.analysis_input_snapshot":
        ("SELECT count(*) FROM analysis.analysis_input_snapshot x "
         "  JOIN analysis.analysis_run r ON r.id = x.analysis_id "
         " WHERE r.user_id = %s"),
    "analysis.analysis_line_item":
        ("SELECT count(*) FROM analysis.analysis_line_item x "
         "  JOIN analysis.analysis_run r ON r.id = x.analysis_id "
         " WHERE r.user_id = %s"),
    "analysis.reconciliation_check":
        ("SELECT count(*) FROM analysis.reconciliation_check x "
         "  JOIN analysis.analysis_run r ON r.id = x.analysis_id "
         " WHERE r.user_id = %s"),
    "billing.invoice":
        ("SELECT count(*) FROM billing.invoice x JOIN billing.subscription s "
         "  ON s.id = x.subscription_id WHERE s.user_id = %s"),
    "docs.document_extraction":
        ("SELECT count(*) FROM docs.document_extraction x JOIN docs.document d "
         "  ON d.id = x.document_id WHERE d.user_id = %s"),
    "docs.document_link":
        ("SELECT count(*) FROM docs.document_link x JOIN docs.document d "
         "  ON d.id = x.document_id WHERE d.user_id = %s"),
    "docs.extraction_field":
        ("SELECT count(*) FROM docs.extraction_field x "
         "  JOIN docs.document_extraction e ON e.id = x.extraction_id "
         "  JOIN docs.document d ON d.id = e.document_id WHERE d.user_id = %s"),
    "ioe.run_rule_snapshot":
        ("SELECT count(*) FROM ioe.run_rule_snapshot x "
         "  LEFT JOIN ioe.optimization_run r ON r.id = x.run_id "
         "  LEFT JOIN ioe.scenario sc ON sc.id = x.scenario_id "
         " WHERE r.user_id = %s OR sc.user_id = %s"),
    "reco.recommendation_status_event":
        ("SELECT count(*) FROM reco.recommendation_status_event x "
         "  JOIN reco.recommendation r ON r.id = x.recommendation_id "
         " WHERE r.user_id = %s"),
    "wealth.asset_valuation":
        ("SELECT count(*) FROM wealth.asset_valuation x JOIN wealth.asset a "
         "  ON a.id = x.asset_id WHERE a.user_id = %s"),
    "wealth.liability_balance":
        ("SELECT count(*) FROM wealth.liability_balance x "
         "  JOIN wealth.liability l ON l.id = x.liability_id "
         " WHERE l.user_id = %s"),
    "wealth.registered_account_detail":
        ("SELECT count(*) FROM wealth.registered_account_detail x "
         "  JOIN wealth.asset a ON a.id = x.asset_id WHERE a.user_id = %s"),
}


def _rows_in_tree(cur, table: str, user_id: uuid.UUID) -> int:
    sql = _TREE_COUNT[table]
    params = (str(user_id),) * sql.count("%s")
    cur.execute(sql, params)
    return cur.fetchone()[0]


@pytest.mark.parametrize("table", PD1_TABLES)
def test_one_tenant_cannot_insert_a_row_into_anothers_tree(tenants, table):
    """§18 — the case a read-only policy misses.

    Protecting SELECT while leaving INSERT open lets a caller write into
    somebody else's account: a forged invoice, a forged AI message, a forged
    line item on another tenant's sealed analysis. `WITH CHECK` is what stops
    it, and a policy written without one looks complete.

    Asserted on the OUTCOME, because two different correct refusals exist. A
    literal parent id is rejected by `WITH CHECK` and raises. A forged INSERT
    that reads the parent from the table first — the shape a real attacker's
    query would have — now selects nothing, so it inserts nothing and raises
    nothing. Requiring the error would have failed three tables for being
    protected slightly better than expected.
    """
    a, b = tenants
    sql, params = _forged_inserts(a, b)[table]
    with owner_cursor() as owner:
        before = _rows_in_tree(owner, table, b.user_id)

    with runtime_cursor(a.user_id) as cur:
        try:
            cur.execute(sql, params)
            inserted = cur.rowcount
        except psycopg2.errors.InsufficientPrivilege as exc:
            assert "row-level security" in str(exc).lower(), exc
            inserted = 0

    with owner_cursor() as owner:
        after = _rows_in_tree(owner, table, b.user_id)

    assert inserted == 0, f"{table}: tenant A inserted into tenant B's tree"
    assert after == before, (
        f"{table}: tenant B's tree gained a row written by tenant A"
    )


def test_a_tenant_cannot_move_a_row_into_another_tenants_tree(tenants):
    """§18 — reassignment. Changing a child's parent from A's to B's is the
    same forgery as inserting it there, and needs the same `WITH CHECK`.

    Here the row IS visible to A — it is A's — so `USING` admits the update and
    `WITH CHECK` rejects the new value, loudly. That is the better of the two
    refusals: a silent zero-row update would leave the caller believing the
    move simply did not match.
    """
    a, b = tenants
    with runtime_cursor(a.user_id) as cur:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege) as caught:
            cur.execute(
                "UPDATE ai.ai_message_citation SET message_id = %s "
                " WHERE id = %s",
                (b.rows["ai.ai_message"], a.rows["ai.ai_message_citation"]))
        assert "row-level security" in str(caught.value).lower(), caught.value

    with owner_cursor() as owner:
        owner.execute(
            "SELECT message_id FROM ai.ai_message_citation WHERE id = %s",
            (a.rows["ai.ai_message_citation"],))
        assert owner.fetchone()[0] == a.rows["ai.ai_message"], (
            "the citation was reassigned into another tenant's message tree"
        )
