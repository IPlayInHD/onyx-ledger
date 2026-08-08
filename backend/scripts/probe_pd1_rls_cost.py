"""What do the PD-1 policies cost a real query? (Entry 11B1 §31)

Every policy is a correlated `EXISTS` against a parent, which is exactly the
shape that turns into a hot-path problem when the child side has no index on its
parent pointer. So this measures the queries the services actually issue, with
the policies on and off, and prints the plan for one of each shape.

METHOD
RLS is toggled on ONE database, so the comparison is between two states of the
same tables with the same data rather than between two environments. Queries run
as `onyx_app_rw` with `app.user_id` set, which is the only configuration where
the policies apply at all.

    PGHOST=... PGPORT=... ONYX_PD1_PROBE_DSN=... python scripts/probe_pd1_rls_cost.py
"""
from __future__ import annotations

import os
import statistics
import sys
import time
import uuid

sys.path.insert(0, os.getcwd())

import psycopg2  # noqa: E402

ROUNDS = 200
CHILDREN_PER_PARENT = 25

#: One query per policy shape, written the way a service reads the child:
#: scoped by the parent it already has.
QUERIES = {
    "depth-1 (analysis_line_item)":
        "SELECT count(*) FROM analysis.analysis_line_item WHERE analysis_id = %s",
    "depth-2 (extraction_field)":
        "SELECT count(*) FROM docs.extraction_field WHERE extraction_id = %s",
    "two-branch (run_rule_snapshot)":
        "SELECT count(*) FROM ioe.run_rule_snapshot WHERE run_id = %s",
    "unscoped child scan (ai_message)":
        "SELECT count(*) FROM ai.ai_message",
}

PD1_TABLES = (
    "ai.ai_message", "ai.ai_message_citation", "ai.ai_prompt_context",
    "analysis.analysis_assumption", "analysis.analysis_input_snapshot",
    "analysis.analysis_line_item", "analysis.reconciliation_check",
    "billing.invoice", "docs.document_extraction", "docs.document_link",
    "docs.extraction_field", "ioe.run_rule_snapshot",
    "reco.recommendation_status_event", "wealth.asset_valuation",
    "wealth.liability_balance", "wealth.registered_account_detail",
)


def _dsn() -> str:
    return os.environ.get(
        "ONYX_PD1_PROBE_DSN",
        "postgresql://onyx_migrator@localhost:5432/onyx_test",
    )


def _seed(cur) -> dict[str, str]:
    """One tenant with enough children that a sequential scan would show."""
    user = str(uuid.uuid4())
    cur.execute("INSERT INTO identity.user_account (id, email, status) "
                "VALUES (%s, %s, 'active')",
                (user, f"pd1probe_{uuid.uuid4().hex[:10]}@example.com"))

    cur.execute("INSERT INTO analysis.analysis_run "
                "(user_id, tax_year, engine_version) "
                "VALUES (%s, 2025, '1.0') RETURNING id", (user,))
    analysis = cur.fetchone()[0]
    for i in range(CHILDREN_PER_PARENT):
        cur.execute("INSERT INTO analysis.analysis_line_item "
                    "(analysis_id, kind, label, amount) "
                    "VALUES (%s, 'income', %s, 1)", (analysis, f"probe {i}"))

    cur.execute("INSERT INTO docs.document (user_id, bucket, object_key) "
                "VALUES (%s, 'probe', %s) RETURNING id",
                (user, uuid.uuid4().hex))
    document = cur.fetchone()[0]
    cur.execute("INSERT INTO docs.document_extraction (document_id, engine) "
                "VALUES (%s, 'probe') RETURNING id", (document,))
    extraction = cur.fetchone()[0]
    for i in range(CHILDREN_PER_PARENT):
        cur.execute("INSERT INTO docs.extraction_field "
                    "(extraction_id, field_name) VALUES (%s, %s)",
                    (extraction, f"probe_{i}"))

    cur.execute("INSERT INTO ioe.optimization_run "
                "(user_id, analysis_id, tax_year) "
                "VALUES (%s, %s, 2025) RETURNING id", (user, analysis))
    run = cur.fetchone()[0]
    for _ in range(CHILDREN_PER_PARENT):
        cur.execute("INSERT INTO ioe.rule_snapshot (snapshot_hash) "
                    "VALUES (%s) RETURNING id", (uuid.uuid4().hex,))
        snapshot = cur.fetchone()[0]
        cur.execute("INSERT INTO ioe.run_rule_snapshot (run_id, snapshot_id) "
                    "VALUES (%s, %s)", (run, snapshot))

    cur.execute("INSERT INTO ai.ai_conversation (user_id) VALUES (%s) "
                "RETURNING id", (user,))
    conversation = cur.fetchone()[0]
    for i in range(CHILDREN_PER_PARENT):
        cur.execute("INSERT INTO ai.ai_message (conversation_id, role, content) "
                    "VALUES (%s, 'user', %s)", (conversation, f"probe {i}"))

    return {"user": user, "analysis": analysis, "extraction": extraction,
            "run": run}


def _set_rls(cur, enabled: bool) -> None:
    for table in PD1_TABLES:
        cur.execute(
            f"ALTER TABLE {table} "
            f"{'ENABLE' if enabled else 'DISABLE'} ROW LEVEL SECURITY")


def _params(key: str, seed: dict[str, str]) -> tuple:
    if "analysis_line_item" in key:
        return (seed["analysis"],)
    if "extraction_field" in key:
        return (seed["extraction"],)
    if "run_rule_snapshot" in key:
        return (seed["run"],)
    return ()


def _measure(cur, sql: str, params: tuple) -> tuple[float, float]:
    for _ in range(10):
        cur.execute(sql, params)
        cur.fetchall()
    samples = []
    for _ in range(ROUNDS):
        started = time.perf_counter()
        cur.execute(sql, params)
        cur.fetchall()
        samples.append(time.perf_counter() - started)
    ordered = sorted(samples)
    return (statistics.median(ordered) * 1000,
            ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] * 1000)


def main() -> int:
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    owner = conn.cursor()
    seed = _seed(owner)

    results: dict[str, dict[str, tuple[float, float]]] = {}
    plans: dict[str, str] = {}

    for label, enabled in (("without RLS", False), ("with RLS", True)):
        _set_rls(owner, enabled)
        work = psycopg2.connect(_dsn())
        work.autocommit = False
        try:
            cur = work.cursor()
            cur.execute("SELECT set_config('app.user_id', %s, true)",
                        (seed["user"],))
            cur.execute("SET ROLE onyx_app_rw")
            for key, sql in QUERIES.items():
                params = _params(key, seed)
                results.setdefault(key, {})[label] = _measure(cur, sql, params)
                if enabled:
                    cur.execute(f"EXPLAIN (COSTS OFF) {sql}", params)
                    plans[key] = "\n      ".join(r[0] for r in cur.fetchall())
        finally:
            work.rollback()
            work.close()

    _set_rls(owner, True)   # leave the database protected
    conn.close()

    print(f"PD-1 policy overhead, n={ROUNDS}, "
          f"{CHILDREN_PER_PARENT} children per parent\n")
    print(f"{'query':34} {'p50 off':>9} {'p50 on':>9} {'p95 off':>9} "
          f"{'p95 on':>9}  {'Δp50':>8}")
    for key, row in results.items():
        (off50, off95) = row["without RLS"]
        (on50, on95) = row["with RLS"]
        print(f"{key:34} {off50:>8.3f}m {on50:>8.3f}m {off95:>8.3f}m "
              f"{on95:>8.3f}m  {on50 - off50:>+7.3f}m")

    print("\nplans with RLS enabled:")
    for key, plan in plans.items():
        print(f"  {key}\n      {plan}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
