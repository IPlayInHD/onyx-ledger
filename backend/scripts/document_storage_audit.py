"""Document/object-storage integrity and legacy-key inventory (Entry 11B4).

Answers three operational questions that only a real environment can answer,
and answers them in COUNTS. Never a filename, never an object key, never a
document id — a tool for handling a privacy defect must not become one.

    python scripts/document_storage_audit.py

WHAT IT REPORTS

  legacy keys (§10)
      How many `docs.document` rows still carry a filename-bearing key from
      before Entry 11B4, and how many carry the opaque `v2` form. The legacy
      format was `{user_id}/{uuid4}/{filename}` — the filename is personal data
      at rest in PostgreSQL, independently of whatever the bucket holds.

  deletion integrity (§39)
      Documents tombstoned without their extraction purged, and documents that
      are live but were never given a key. Both are the shape a crash between
      the object deletion and the database work would leave.

WHAT IT CANNOT REPORT
Objects in the bucket with no database owner. That needs a bucket LISTING, and
the port has no list operation — deliberately, since nothing in the product
needs one. Stated as a limitation rather than silently omitted: if a real
provider is ever wired, an orphan sweep needs either a list capability or a
provider-side inventory report.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.getcwd())

import psycopg2  # noqa: E402

#: The opaque form introduced by Entry 11B4: {user_id}/v2/{document_id}.
OPAQUE_KEY = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"/v2/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _dsn() -> str:
    return os.environ.get(
        "ONYX_DOCUMENT_AUDIT_DSN",
        "postgresql://onyx_migrator@localhost:5432/onyx",
    )


def legacy_key_counts(cur) -> tuple[int, int]:
    """(opaque, non-opaque). Compared in SQL against the same pattern the
    service generates, so the two cannot drift."""
    cur.execute("""
        SELECT
          count(*) FILTER (WHERE object_key ~ %(pattern)s),
          count(*) FILTER (WHERE object_key !~ %(pattern)s)
          FROM docs.document
    """, {"pattern": OPAQUE_KEY.pattern})
    return cur.fetchone()


def integrity_counts(cur) -> dict[str, int]:
    cur.execute("""
        SELECT
          (SELECT count(*) FROM docs.document d
             WHERE d.deleted_at IS NOT NULL
               AND EXISTS (SELECT 1 FROM docs.document_extraction e
                            WHERE e.document_id = d.id)),
          (SELECT count(*) FROM docs.document d
             WHERE d.deleted_at IS NOT NULL
               AND EXISTS (SELECT 1 FROM docs.extraction_field f
                             JOIN docs.document_extraction e
                               ON e.id = f.extraction_id
                            WHERE e.document_id = d.id)),
          (SELECT count(*) FROM docs.document WHERE object_key = ''),
          (SELECT count(*) FROM docs.document WHERE deleted_at IS NOT NULL),
          (SELECT count(*) FROM docs.document WHERE deleted_at IS NULL)
    """)
    tombstoned_with_extraction, tombstoned_with_fields, keyless, dead, live = \
        cur.fetchone()
    return {
        "tombstoned documents still holding an extraction":
            tombstoned_with_extraction,
        "tombstoned documents still holding extracted fields":
            tombstoned_with_fields,
        "documents with no object key": keyless,
        "tombstoned documents": dead,
        "live documents": live,
    }


def main() -> int:
    conn = psycopg2.connect(_dsn())
    try:
        cur = conn.cursor()
        opaque, legacy = legacy_key_counts(cur)

        print("document object keys (PD-2)")
        print(f"  opaque (v2):            {opaque:6d}")
        print(f"  legacy / other format:  {legacy:6d}")
        if legacy:
            print("\n  Legacy keys embed the user-supplied filename, which is "
                  "personal\n  data at rest in PostgreSQL regardless of what "
                  "the bucket holds.\n  Disposition is an operator decision — "
                  "see docs/privacy/pd2-pd8-document-lifecycle.md.")

        print("\ndeletion integrity (§39)")
        failures = 0
        for label, count in integrity_counts(cur).items():
            print(f"  {label:52s} {count:6d}")
            if count and label.startswith("tombstoned documents still"):
                failures += count

        print("\n  NOT CHECKED: bucket objects with no database owner. The "
              "storage port\n  has no list operation, so this cannot be "
              "answered from here.")
        return 1 if failures else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
