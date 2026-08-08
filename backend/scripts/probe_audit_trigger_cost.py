"""What does audit payload minimization cost the write path? (Entry 11B0 §19)

The audit trigger is genuinely hot: it fires on every INSERT, UPDATE and DELETE
of all 33 audited relations, which includes every financial write a user makes.
Migration 0048 replaced a straight `to_jsonb(NEW)` copy with a per-key loop, so
the cost is not obviously nil and should be measured rather than asserted.

METHOD
The two trigger versions are swapped in place on ONE database, so the comparison
is between two functions rather than between two environments. Each round does a
real INSERT into `finance.income_source` through SQL — the shape the trigger
sees — with the session GUCs the application sets.

    PGHOST=... PGPORT=... ONYX_AUDIT_PROBE_DSN=... python scripts/probe_audit_trigger_cost.py
"""
from __future__ import annotations

import os
import pathlib
import statistics
import sys
import time
import uuid

sys.path.insert(0, os.getcwd())

import psycopg2  # noqa: E402

SQL_DIR = pathlib.Path(__file__).resolve().parents[1] / "db" / "sql"
BEFORE = SQL_DIR / "40_audit_secret_redaction.sql"
AFTER = SQL_DIR / "42_audit_payload_minimization.sql"

ROUNDS = 300


def _dsn() -> str:
    return os.environ.get(
        "ONYX_AUDIT_PROBE_DSN",
        "postgresql://onyx_migrator@localhost:5432/onyx_test",
    )


def _apply(cur, path: pathlib.Path) -> None:
    cur.execute(path.read_text())


def _seed(cur) -> tuple[str, str]:
    """One account and one income type to write against."""
    user = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO identity.user_account (id, email, status) "
        "VALUES (%s, %s, 'active')",
        (user, f"auditprobe_{uuid.uuid4().hex[:10]}@example.com"))
    cur.execute("SELECT id FROM ref.income_type LIMIT 1")
    return user, cur.fetchone()[0]


def _time_inserts(cur, user: str, income_type: str) -> list[float]:
    cur.execute("SELECT set_config('app.user_id', %s, false), "
                "       set_config('app.actor_type', 'user', false)", (user,))
    samples = []
    for _ in range(ROUNDS):
        started = time.perf_counter()
        cur.execute(
            "INSERT INTO finance.income_source "
            "  (user_id, tax_year, income_type_id, amount, source_name) "
            "VALUES (%s, 2025, %s, %s, %s)",
            (user, income_type, "1234.56", "probe source"))
        samples.append(time.perf_counter() - started)
    return samples


def _percentiles(samples: list[float]) -> tuple[float, float]:
    ordered = sorted(samples)
    return (statistics.median(ordered) * 1000,
            ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] * 1000)


def _audited_rows(cur, user: str) -> int:
    cur.execute("SELECT count(*) FROM audit.audit_log WHERE actor_id = %s",
                (user,))
    return cur.fetchone()[0]


def main() -> int:
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    try:
        cur = conn.cursor()
        results = {}
        for label, sql in (("before (0046)", BEFORE), ("after  (0048)", AFTER)):
            _apply(cur, sql)
            user, income_type = _seed(cur)
            _time_inserts(cur, user, income_type)          # warm
            user, income_type = _seed(cur)
            samples = _time_inserts(cur, user, income_type)
            results[label] = (_percentiles(samples), _audited_rows(cur, user))

        # Leave the database on the CURRENT function, not the old one.
        _apply(cur, AFTER)

        print(f"finance.income_source INSERT, n={ROUNDS} per row\n")
        print(f"{'trigger':16} {'p50':>9} {'p95':>9} {'audit rows':>11}")
        for label, ((p50, p95), rows) in results.items():
            print(f"{label:16} {p50:>8.3f}m {p95:>8.3f}m {rows:>11}")

        (b50, b95), _ = results["before (0046)"]
        (a50, a95), _ = results["after  (0048)"]
        print(f"\nadded p50: {a50 - b50:+.3f} ms")
        print(f"added p95: {a95 - b95:+.3f} ms")
        print("\nStatement count is unchanged: the trigger fires once per row "
              "either way and issues the same single INSERT.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
