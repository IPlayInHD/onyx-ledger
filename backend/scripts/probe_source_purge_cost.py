"""What does SOURCE_DATA deletion cost, and what does it scale with? (Entry 11B5I §8-§17)

THE QUESTION IS SHAPE, NOT MILLISECONDS. The architectural claim Entry 11B5
makes is that the account purge is set-based: one statement per governed table,
regardless of how many rows a tenant has. If instead the application issued one
DELETE per source row, a large account would turn into thousands of round trips
inside a single lifecycle phase — and that is a defect visible in the STATEMENT
COUNT long before it is visible in a timing.

So statement counts are the primary evidence here and the latencies are context.

MEASURED ON A LOCAL DEVELOPMENT DATABASE. These numbers say nothing about a
managed PostgreSQL deployment: no network hop, no shared tenancy, no provider
storage layer. They are used to establish COMPLEXITY, which is a property of the
code and transfers; absolute latency is not.

    PGHOST=... PGPORT=... PGSUPER=... python scripts/probe_source_purge_cost.py
"""
from __future__ import annotations

import contextlib
import os
import statistics
import sys
import time
import uuid

sys.path.insert(0, os.getcwd())

import psycopg2  # noqa: E402

ROUNDS = 25
SCALES = (1, 10, 100, 1000)


def _dsn() -> str:
    host = os.environ.get("PGHOST", "/var/run/postgresql")
    port = os.environ.get("PGPORT", "5432")
    user = os.environ.get("PGSUPER", "onyx_migrator")
    database = os.environ.get("ONYX_PROBE_DB", "onyx_test")
    return f"postgresql://{user}@/{database}?host={host}&port={port}"


def _connect():
    conn = psycopg2.connect(_dsn(), cursor_factory=CountingCursor)
    conn.autocommit = True
    return conn


class CountingCursor(psycopg2.extensions.cursor):
    """Counts APPLICATION round trips, which is the property under test.

    §12 and §14 ask whether the application issues one call per source row. A
    psycopg2 cursor will not accept an assigned `execute`, so the count is taken
    by subclassing — the same object the benchmark uses everywhere else, with no
    behavioural difference beyond the increment.

    Statements executed INSIDE `purge_source_data` are fixed by the function
    body — one set-based DELETE per governed table, asserted structurally in
    tests/security/test_partition_purge_coverage.py — so they cannot grow with
    row count and are not what this measures.
    """

    count = 0

    def execute(self, sql, args=None):
        CountingCursor.count += 1
        return super().execute(sql, args)


@contextlib.contextmanager
def counting():
    """Zero the counter, run, and report how many round trips happened."""
    start = CountingCursor.count
    box: list[int] = []
    try:
        yield box
    finally:
        box.append(CountingCursor.count - start)


def _percentiles(samples: list[float]) -> tuple[float, float]:
    ordered = sorted(samples)
    return (statistics.median(ordered) * 1000,
            ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] * 1000)


# ------------------------------------------------------------------ fixture --
def _account(cur) -> uuid.UUID:
    user = uuid.uuid4()
    cur.execute("INSERT INTO identity.user_account (id, email, status) "
                "VALUES (%s, %s, 'active')",
                (str(user), f"perf_{uuid.uuid4().hex[:12]}@example.com"))
    return user


def _compose(cur, user: uuid.UUID, rows: int) -> dict[str, int]:
    """A realistic purge workload, not `rows` copies of one table.

    The purge touches eight governed tables; loading all of a scale into
    `income_source` would measure one DELETE and call it a thousand rows. The
    split below spreads across the tables and the tax-year partitions that a
    real account actually populates.
    """
    profile_rows = 1 if rows >= 1 else 0
    dependents = min(max(rows // 20, 0), 10)
    remainder = max(rows - profile_rows - dependents, 0)
    income_rows = (remainder + 1) // 2
    expense_rows = remainder // 2

    if profile_rows:
        cur.execute("INSERT INTO profile.tax_profile (user_id, province_code, "
                    "marital_status) VALUES (%s, 'ON', 'single')", (str(user),))
    if dependents:
        cur.execute("INSERT INTO profile.dependent (user_id, relationship) "
                    "SELECT %s, 'child' FROM generate_series(1, %s)",
                    (str(user), dependents))
    if income_rows:
        cur.execute("""
            INSERT INTO finance.income_source
                (user_id, tax_year, income_type_id, amount, province_code)
            SELECT %s, 2024 + (g %% 2),
                   (SELECT id FROM ref.income_type WHERE code = 'employment'),
                   1000 + g, 'ON'
              FROM generate_series(1, %s) g
        """, (str(user), income_rows))
    if expense_rows:
        cur.execute("""
            INSERT INTO finance.expense_record
                (user_id, tax_year, expense_category_id, amount)
            SELECT %s, 2024 + (g %% 2),
                   (SELECT id FROM ref.expense_category LIMIT 1), 10 + g
              FROM generate_series(1, %s) g
        """, (str(user), expense_rows))

    return {"tax_profile": profile_rows, "dependent": dependents,
            "income_source": income_rows, "expense_record": expense_rows}


def _to_purging(cur, user: uuid.UUID) -> uuid.UUID:
    cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                "VALUES (%s, 'DELETION_REQUESTED')", (str(user),))
    for state in ("ACCESS_DISABLED", "PURGE_PENDING", "PURGING"):
        cur.execute("UPDATE identity.account_lifecycle SET state = %s "
                    " WHERE user_id = %s", (state, str(user)))
    token = uuid.uuid4()
    cur.execute("UPDATE identity.account_lifecycle SET claimed_by = 'perf', "
                "claim_token = %s, claimed_at = now() WHERE user_id = %s",
                (str(token), str(user)))
    cur.execute("SELECT identity.start_lifecycle_phase(%s, %s, %s, 'perf')",
                (str(user), "SOURCE_DATA", str(token)))
    return token


def _remaining(cur, user: uuid.UUID) -> int:
    cur.execute("SELECT identity.count_remaining_source_data(%s)", (str(user),))
    return cur.fetchone()[0]


def _outbox(cur, user: uuid.UUID) -> int:
    cur.execute("SELECT count(*) FROM ioe.freshness_outbox WHERE user_id = %s",
                (str(user),))
    return cur.fetchone()[0]


# ---------------------------------------------------------------- benchmarks -
def bench_account_purge(conn) -> None:
    print("account SOURCE_DATA purge — scaling with qualifying rows")
    print(f"{'rows':>6} {'composition':38} {'stmts':>6} {'p50':>9} {'p95':>9} "
          f"{'after':>6} {'events':>7}")

    for scale in SCALES:
        samples: list[float] = []
        statements = 0
        composition: dict[str, int] = {}
        before_rows = after_rows = 0
        events = 0

        rounds = ROUNDS if scale <= 100 else max(ROUNDS // 5, 5)
        for _ in range(rounds):
            cur = conn.cursor()
            user = _account(cur)
            composition = _compose(cur, user, scale)
            token = _to_purging(cur, user)
            before_rows = _remaining(cur, user)

            with counting() as calls:
                started = time.perf_counter()
                cur.execute("SELECT identity.purge_source_data(%s, %s, 'perf')",
                            (str(user), str(token)))
                samples.append(time.perf_counter() - started)
            statements = calls[0]

            after_rows = _remaining(cur, user)
            events = _outbox(cur, user)
            # §16 — correctness is not suspended for a benchmark.
            assert after_rows == 0, f"purge left {after_rows} rows at scale {scale}"
            cur.execute(
                "SELECT identity.complete_lifecycle_phase(%s, %s, %s, 'perf')",
                (str(user), "SOURCE_DATA", str(token)))
            assert cur.fetchone()[0], f"completion refused at scale {scale}"

        p50, p95 = _percentiles(samples)
        shape = ", ".join(f"{k.split('_')[0]}={v}" for k, v in composition.items() if v)
        print(f"{before_rows:>6} {shape:38} {statements:>6} {p50:>8.2f}m "
              f"{p95:>8.2f}m {after_rows:>6} {events:>7}")


def bench_remaining_count(conn) -> None:
    print("\ncount_remaining_source_data — the completion authority")
    print(f"{'rows':>6} {'stmts':>6} {'p50':>9} {'p95':>9}")
    for scale in SCALES:
        cur = conn.cursor()
        user = _account(cur)
        _compose(cur, user, scale)
        samples = []
        with counting() as calls:
            for _ in range(ROUNDS):
                started = time.perf_counter()
                cur.execute("SELECT identity.count_remaining_source_data(%s)",
                            (str(user),))
                cur.fetchone()
                samples.append(time.perf_counter() - started)
        p50, p95 = _percentiles(samples)
        print(f"{scale:>6} {calls[0] // ROUNDS:>6} {p50:>8.2f}m {p95:>8.2f}m")


def bench_retry(conn) -> None:
    print("\npurge retry — first run vs immediate repeat (rows already absent)")
    print(f"{'rows':>6} {'first p50':>11} {'repeat p50':>12} {'repeat p95':>12}")
    for scale in (10, 1000):
        cur = conn.cursor()
        user = _account(cur)
        _compose(cur, user, scale)
        token = _to_purging(cur, user)

        started = time.perf_counter()
        cur.execute("SELECT identity.purge_source_data(%s, %s, 'perf')",
                    (str(user), str(token)))
        first = (time.perf_counter() - started) * 1000
        assert _remaining(cur, user) == 0

        repeats = []
        for _ in range(ROUNDS):
            started = time.perf_counter()
            cur.execute("SELECT identity.purge_source_data(%s, %s, 'perf')",
                        (str(user), str(token)))
            repeats.append(time.perf_counter() - started)
        p50, p95 = _percentiles(repeats)
        print(f"{scale:>6} {first:>10.2f}m {p50:>11.2f}m {p95:>11.2f}m")


def bench_individual_delete(conn) -> None:
    print("\nindividual deletion — one row out of a populated account")
    print(f"{'operation':22} {'account rows':>13} {'stmts':>6} {'p50':>9} {'p95':>9}")
    for label, table, extra in (
        ("delete_income_source", "finance.income_source", ""),
        ("delete_expense", "finance.expense_record", ""),
    ):
        cur = conn.cursor()
        user = _account(cur)
        composition = _compose(cur, user, 500)
        total = sum(composition.values())
        cur.execute(f"SELECT id, tax_year FROM {table} WHERE user_id = %s "
                    f"LIMIT {ROUNDS}", (str(user),))
        targets = cur.fetchall()

        samples = []
        cur.execute("SET app.user_id = %s", (str(user),))
        with counting() as calls:
            for row_id, year in targets:
                started = time.perf_counter()
                cur.execute(f"DELETE FROM {table} WHERE id = %s AND tax_year = %s{extra}",
                            (str(row_id), year))
                samples.append(time.perf_counter() - started)
        cur.execute("RESET app.user_id")
        p50, p95 = _percentiles(samples)
        print(f"{label:22} {total:>13} {calls[0] // len(targets):>6} "
              f"{p50:>8.2f}m {p95:>8.2f}m")


def bench_worker_claim(conn) -> None:
    print("\nprivacy worker claim — scaling with PENDING SUBJECTS")
    print(f"{'pending':>8} {'claimed':>8} {'stmts':>6} {'p50':>9} {'p95':>9}")
    for pending in (1, 10, 100):
        cur = conn.cursor()
        for _ in range(pending):
            user = _account(cur)
            cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                        "VALUES (%s, 'DELETION_REQUESTED')", (str(user),))
        samples = []
        claimed = 0
        total_calls = 0
        for _ in range(5):
            with counting() as calls:
                started = time.perf_counter()
                cur.execute("SELECT count(*) FROM "
                            "identity.claim_account_lifecycle(50, 'perf-worker')")
                claimed = cur.fetchone()[0]
                samples.append(time.perf_counter() - started)
            total_calls += calls[0]
            cur.execute("UPDATE identity.account_lifecycle SET claimed_by = NULL, "
                        "claim_token = NULL, claimed_at = NULL "
                        " WHERE claimed_by = 'perf-worker'")
        p50, p95 = _percentiles(samples)
        print(f"{pending:>8} {claimed:>8} {total_calls // 5:>6} "
              f"{p50:>8.2f}m {p95:>8.2f}m")


def main() -> None:
    conn = _connect()
    try:
        print("Entry 11B5I — SOURCE_DATA deletion cost")
        print("LOCAL DEVELOPMENT DATABASE. Complexity transfers; latency does not.\n")
        bench_account_purge(conn)
        bench_remaining_count(conn)
        bench_retry(conn)
        bench_individual_delete(conn)
        bench_worker_claim(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
