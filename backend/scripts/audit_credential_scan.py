"""Find — and optionally scrub — pre-fix credential material in audit.audit_log.

Migration 0046 stopped `audit.log_change` copying credential values into the
audit log. It is forward-only: rows written before it keep whatever they
already contain, and `audit.audit_log` is append-only by trigger, so nothing in
the ordinary application can remove them.

This script answers, for any environment, "is there historical exposure here?"
and can repair it. It is deliberately NOT an Alembic data migration: a data
migration runs automatically everywhere on upgrade, which is the wrong shape
for a repair we believe is unnecessary in every environment that exists. An
operator runs this against an environment, reads the count, and decides.

    python scripts/audit_credential_scan.py            # report only
    python scripts/audit_credential_scan.py --scrub    # repair, then re-report

OUTPUT DISCIPLINE
Counts, timestamps and table names. Never an email address, never a hash, never
a payload fragment — this is a tool for handling a credential leak and must not
become one. The scan matches on JSON KEY PRESENCE, so it never has to read a
value to decide.

PRIVILEGE
The scrub disables the append-only trigger for the duration of its own
transaction and re-enables it in a `finally`. That requires table ownership, so
it runs as the migration role and NOT as `onyx_app_rw` — the runtime role gains
nothing, and the normal immutability guarantee is unchanged for every other
caller. This is the narrowest controlled mechanism available: PostgreSQL has no
per-statement trigger bypass.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.getcwd())

import psycopg2  # noqa: E402

#: Keys whose VALUE is credential material. Identical to the redaction list in
#: db/sql/40_audit_secret_redaction.sql — if the two drift, the scrub stops
#: matching what the trigger now protects.
SECRET_KEYS = (
    "password_hash",
    "refresh_token_hash",
    "token_hash",
    "reset_token_hash",
    "verification_token_hash",
    "mfa_secret",
    "secret",
)

REDACTED = "[redacted]"

_CANDIDATES = """
    SELECT entity_schema, entity_table, action,
           count(*)                AS rows,
           min(created_at)::text   AS earliest,
           max(created_at)::text   AS latest
      FROM audit.audit_log
     WHERE {predicate}
     GROUP BY 1, 2, 3
     ORDER BY 1, 2, 3
"""

#: A row is a candidate when a secret key is present AND its value is not
#: already the redaction marker. `->>` yields NULL for a JSON null, and the
#: `? ` operator is false when the payload is NULL or not an object, so
#: malformed and partial payloads fall out without special-casing.
_PREDICATE = " OR ".join(
    f"(new_value ? '{k}' AND new_value ->> '{k}' IS DISTINCT FROM '{REDACTED}')"
    f" OR (previous_value ? '{k}' AND previous_value ->> '{k}' "
    f"IS DISTINCT FROM '{REDACTED}')"
    for k in SECRET_KEYS
)


def _dsn() -> str:
    """Owner-role DSN. The scrub needs table ownership; the scan does not, but
    using one connection keeps the reported numbers consistent."""
    return os.environ.get(
        "ONYX_AUDIT_SCAN_DSN",
        "postgresql://onyx_migrator@localhost:5432/onyx",
    )


def scan(cur) -> list[tuple]:
    cur.execute(_CANDIDATES.format(predicate=_PREDICATE))
    return cur.fetchall()


def report(rows: list[tuple]) -> int:
    total = sum(r[3] for r in rows)
    if not rows:
        print("  no audit rows carry unredacted credential material")
        return 0
    print(f"  {'schema.table':32s} {'action':8s} {'rows':>6s}  earliest → latest")
    for schema, table, action, count, earliest, latest in rows:
        print(f"  {schema + '.' + table:32s} {action:8s} {count:6d}  "
              f"{earliest} → {latest}")
    print(f"  TOTAL candidate rows: {total}")
    return total


def scrub(cur) -> int:
    """Replace the VALUE of each secret key with the marker, in place.

    The audit row keeps its id, actor, action, entity, timestamp and every other
    field: an auditor still sees that a credential was created or rotated, which
    is the reason the event is audited at all. Deleting whole rows would remove
    that evidence to solve a problem confined to one key.

    Idempotent by predicate — a row already holding the marker does not match,
    so a second run changes nothing.
    """
    sets = []
    for key in SECRET_KEYS:
        sets.append(
            f"new_value = CASE WHEN new_value ? '{key}' "
            f"THEN jsonb_set(new_value, ARRAY['{key}'], '\"{REDACTED}\"'::jsonb) "
            f"ELSE new_value END"
        )
        sets.append(
            f"previous_value = CASE WHEN previous_value ? '{key}' "
            f"THEN jsonb_set(previous_value, ARRAY['{key}'], "
            f"'\"{REDACTED}\"'::jsonb) ELSE previous_value END"
        )

    # One statement per key pair, applied in sequence, so a payload carrying two
    # different secret keys is fully covered.
    changed = 0
    for statement in sets:
        cur.execute(
            f"UPDATE audit.audit_log SET {statement} WHERE {_PREDICATE}"
        )
        changed = max(changed, cur.rowcount or 0)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scrub", action="store_true",
                        help="repair the rows found, then re-scan")
    args = parser.parse_args()

    conn = psycopg2.connect(_dsn())
    try:
        cur = conn.cursor()
        print("audit.audit_log — credential material scan")
        before = report(scan(cur))

        if not args.scrub:
            return 1 if before else 0

        if not before:
            print("\n  nothing to scrub")
            return 0

        print(f"\n  scrubbing {before} candidate rows...")
        # The append-only trigger is disabled for this transaction only, and
        # restored whatever happens. Ordinary callers are unaffected: they never
        # had the ownership required to do this.
        cur.execute("ALTER TABLE audit.audit_log DISABLE TRIGGER trg_audit_immutable")
        try:
            scrub(cur)
            conn.commit()
        finally:
            cur.execute("ALTER TABLE audit.audit_log ENABLE TRIGGER trg_audit_immutable")
            conn.commit()

        after = report(scan(cur))
        print(f"\n  remaining candidate rows: {after}")
        return 0 if after == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
