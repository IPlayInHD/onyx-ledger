"""Emit docs/privacy/data-inventory.csv from the governed registry.

The CSV is a VIEW of `app.privacy.classification.LIFECYCLE`, regenerated rather
than maintained: a spreadsheet edited by hand is a second source of truth, and
the second one is always the stale one.

    python scripts/export_privacy_inventory.py
"""
from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())

from app.privacy import LIFECYCLE  # noqa: E402

COLUMNS = [
    "table", "schema", "privacy_classes", "source_kind", "retention_class",
    "on_account_deletion", "replay_dependency", "immutable", "rls_enforced",
    "exportable", "notes",
]


def main() -> int:
    out = Path(__file__).resolve().parents[2] / "docs" / "privacy" / "data-inventory.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for name in sorted(LIFECYCLE):
            e = LIFECYCLE[name]
            writer.writerow([
                e.table, e.table.split(".", 1)[0],
                " ".join(c.value for c in e.classes),
                e.source.value, e.retention.value, e.on_account_deletion.value,
                str(e.replay_dependency).lower(), str(e.immutable).lower(),
                str(e.rls).lower(), str(e.exportable).lower(), e.notes,
            ])
    print(f"wrote {out} ({len(LIFECYCLE)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
