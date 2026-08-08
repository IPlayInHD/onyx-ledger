"""Find — and optionally minimize — pre-fix personal data in audit.audit_log.

Migration 0048 stopped `audit.log_change` copying whole user rows into the
audit log (PD-4). It is forward-only: rows written before it keep whatever they
already contain, and `audit.audit_log` is append-only by trigger, so nothing in
the ordinary application can touch them.

That leaves a real question no migration should answer on its own — does THIS
environment hold historical rows carrying somebody's salary, employer or email
address? An operator runs this, reads the counts, and decides.

    python scripts/audit_payload_scan.py             # report only
    python scripts/audit_payload_scan.py --minimize  # repair, then re-report

WHY NOT AN ALEMBIC DATA MIGRATION
A data migration runs automatically in every environment on upgrade, including
ones with no exposure, and would have to disable the append-only trigger inside
the same transaction that is upgrading the schema. The volume is unknown and
the operation is irreversible. Entry 11A made the same call for the credential
scrub (`audit_credential_scan.py`); this follows it deliberately rather than by
coincidence.

OUTPUT DISCIPLINE
Counts, timestamps, table names and COLUMN NAMES. Never a value, never an email
address, never a figure — a tool for handling a personal-data leak must not
become one. The scan decides by comparing against the marker, so it never has
to print what it found.

WHAT "MINIMIZE" DOES
Exactly what the fixed trigger does to a new row: for a relation that is not on
the operator-plane allowlist, every non-structural VALUE is replaced by
`[redacted]`. Keys are kept, so the record still shows which columns the row
had. It cannot reconstruct `[changed]` — that needed both sides at write time,
and inventing it now would be inventing evidence.

PRIVILEGE
Minimizing disables the append-only trigger for the duration of its own
transaction and re-enables it in a `finally`. That requires table ownership, so
it runs as the migration role and NOT as `onyx_app_rw` — the runtime role gains
nothing and immutability is unchanged for every other caller. PostgreSQL has no
per-statement trigger bypass; this is the narrowest controlled mechanism there
is.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.getcwd())

import psycopg2  # noqa: E402

REDACTED = "[redacted]"
CHANGED = "[changed]"

#: Relations whose payload is operator/reference data. MUST match
#: `audit.audit_retains_values` in db/sql/42_audit_payload_minimization.sql —
#: `tests/security/test_audit_payload_privacy.py` asserts they agree, because a
#: drift here would either scrub governance evidence or miss personal data.
RETAINED_RELATIONS = (
    "tax_kb.tax_rule",
    "tax_kb.tax_rule_version",
    "rules.rule_condition",
    "rules.rule_outcome",
    "rules.calc_formula",
    "admin.rule_change_request",
    "admin.rule_publication",
    "tkms.import_job",
    "tkms.rollback_record",
    "ioe.weight_config",
)

#: Columns whose values survive. MUST match `audit.audit_structural_columns()`.
STRUCTURAL_COLUMNS = (
    "id", "user_id", "analysis_id", "subscription_id", "optimization_run_id",
    "scenario_id", "admin_id", "tax_rule_version_id",
    "created_at", "updated_at", "deleted_at",
    "row_version", "status", "workflow_status", "visibility_status",
    "evidence_status", "verification_status",
    "manifest_hash", "version_manifest",
    "optimization_result_hash", "optimization_spec_hash",
    "scenario_result_hash", "scenario_spec_hash",
    "baseline_input_snapshot_hash", "baseline_result_hash",
    "engine_version", "lever_registry_version", "objective_version",
    "result_schema_version", "input_execution_policy_version",
    "schema_version", "version",
)


def _dsn() -> str:
    return os.environ.get(
        "ONYX_AUDIT_SCAN_DSN",
        "postgresql://onyx_migrator@localhost:5432/onyx",
    )


#: A payload key is unminimized when it is neither structural nor already a
#: marker. Expressed in SQL so the scan never materializes a value in Python.
_UNMINIMIZED = """
    EXISTS (
        SELECT 1 FROM jsonb_each_text({column}) AS kv(k, v)
         WHERE NOT (kv.k = ANY (%(structural)s))
           AND kv.v IS DISTINCT FROM %(redacted)s
           AND kv.v IS DISTINCT FROM %(changed)s
    )
"""

_PREDICATE = f"""
    (entity_schema || '.' || entity_table) <> ALL (%(retained)s)
    AND (
        (new_value IS NOT NULL AND jsonb_typeof(new_value) = 'object'
         AND {_UNMINIMIZED.format(column='new_value')})
        OR
        (previous_value IS NOT NULL AND jsonb_typeof(previous_value) = 'object'
         AND {_UNMINIMIZED.format(column='previous_value')})
    )
"""

_PARAMS = {
    "structural": list(STRUCTURAL_COLUMNS),
    "retained": list(RETAINED_RELATIONS),
    "redacted": REDACTED,
    "changed": CHANGED,
}


def scan(cur) -> list[tuple]:
    cur.execute(f"""
        SELECT entity_schema, entity_table, action,
               count(*)              AS rows,
               min(created_at)::text AS earliest,
               max(created_at)::text AS latest
          FROM audit.audit_log
         WHERE {_PREDICATE}
         GROUP BY 1, 2, 3
         ORDER BY 1, 2, 3
    """, _PARAMS)
    return cur.fetchall()


def exposed_columns(cur) -> list[tuple]:
    """Which COLUMN NAMES are carrying values. Names only, never values."""
    cur.execute(f"""
        SELECT entity_schema || '.' || entity_table AS relation,
               kv.k                                 AS column_name,
               count(*)                             AS rows
          FROM audit.audit_log,
               LATERAL jsonb_each_text(
                   coalesce(new_value, previous_value, '{{}}'::jsonb)) AS kv(k, v)
         WHERE {_PREDICATE}
           AND NOT (kv.k = ANY (%(structural)s))
           AND kv.v IS DISTINCT FROM %(redacted)s
           AND kv.v IS DISTINCT FROM %(changed)s
         GROUP BY 1, 2
         ORDER BY 3 DESC, 1, 2
    """, _PARAMS)
    return cur.fetchall()


def report(rows: list[tuple]) -> int:
    total = sum(r[3] for r in rows)
    if not rows:
        print("  no audit rows carry unminimized personal data")
        return 0
    print(f"  {'schema.table':34s} {'action':8s} {'rows':>6s}  earliest → latest")
    for schema, table, action, count, earliest, latest in rows:
        print(f"  {schema + '.' + table:34s} {action:8s} {count:6d}  "
              f"{earliest} → {latest}")
    print(f"  TOTAL candidate rows: {total}")
    return total


def minimize(cur) -> None:
    """Replace every non-structural value with the marker, in place.

    The audit row keeps its id, actor, action, entity, timestamp and its column
    NAMES — deleting whole rows would destroy the who/what/when the log exists
    for in order to fix what the values were doing.

    Idempotent by predicate: a row whose values are already markers does not
    match, so a second run changes nothing and reports zero.
    """
    cur.execute(f"""
        UPDATE audit.audit_log SET
            new_value = CASE
                WHEN new_value IS NULL OR jsonb_typeof(new_value) <> 'object'
                THEN new_value
                ELSE (
                    SELECT jsonb_object_agg(
                        kv.k,
                        CASE WHEN kv.k = ANY (%(structural)s)
                                  OR kv.v = %(changed)s
                             THEN to_jsonb(kv.v)
                             ELSE to_jsonb(%(redacted)s::text) END)
                      FROM jsonb_each_text(new_value) AS kv(k, v)
                )
            END,
            previous_value = CASE
                WHEN previous_value IS NULL
                     OR jsonb_typeof(previous_value) <> 'object'
                THEN previous_value
                ELSE (
                    SELECT jsonb_object_agg(
                        kv.k,
                        CASE WHEN kv.k = ANY (%(structural)s)
                                  OR kv.v = %(changed)s
                             THEN to_jsonb(kv.v)
                             ELSE to_jsonb(%(redacted)s::text) END)
                      FROM jsonb_each_text(previous_value) AS kv(k, v)
                )
            END
        WHERE {_PREDICATE}
    """, _PARAMS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minimize", action="store_true",
                        help="repair the rows found, then re-scan")
    args = parser.parse_args()

    conn = psycopg2.connect(_dsn())
    try:
        cur = conn.cursor()
        print("audit.audit_log — personal-data payload scan (PD-4)")
        before = report(scan(cur))

        if before:
            print("\n  columns carrying values (names only):")
            for relation, column, count in exposed_columns(cur)[:40]:
                print(f"    {relation:34s} {column:28s} {count:6d}")

        if not args.minimize:
            return 1 if before else 0
        if not before:
            print("\n  nothing to minimize")
            return 0

        print(f"\n  minimizing {before} candidate rows...")
        cur.execute(
            "ALTER TABLE audit.audit_log DISABLE TRIGGER trg_audit_immutable")
        try:
            minimize(cur)
            conn.commit()
        finally:
            cur.execute(
                "ALTER TABLE audit.audit_log ENABLE TRIGGER trg_audit_immutable")
            conn.commit()

        after = report(scan(cur))
        print(f"\n  remaining candidate rows: {after}")
        return 0 if after == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
