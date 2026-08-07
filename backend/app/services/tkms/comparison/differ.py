"""Pure field-level differ for rule-version snapshots.

A snapshot is a flat {field: stringified-value} dict built from a
tax_rule_version (+ its formula/conditions). Comparing a draft against its
currently-published baseline yields ChangeItems the reviewer sees before
approving. Deterministic and DB-free so it is golden-file testable.
"""
from __future__ import annotations

from app.services.tkms.domain.models import ChangeItem

# fields compared, in a stable display order
SNAPSHOT_FIELDS = (
    "description",
    "effective_date",
    "expiry_date",
    "max_amount",
    "min_amount",
    "income_threshold_low",
    "income_threshold_high",
    "reduction_rate",
    "formula_expression",
    "condition_count",
)


def diff_snapshots(
    baseline: dict | None, draft: dict
) -> tuple[list[ChangeItem], str]:
    """Return (change items, human summary). No baseline ⇒ everything 'added'."""
    items: list[ChangeItem] = []
    for field in SNAPSHOT_FIELDS:
        new = _norm(draft.get(field))
        old = _norm(baseline.get(field)) if baseline is not None else None
        if baseline is None:
            if new is not None:
                items.append(ChangeItem(field=field, change_type="added", new_value=new))
        elif old != new:
            if old is None:
                items.append(ChangeItem(field=field, change_type="added", new_value=new))
            elif new is None:
                items.append(ChangeItem(field=field, change_type="removed", old_value=old))
            else:
                items.append(ChangeItem(field=field, change_type="changed",
                                        old_value=old, new_value=new))

    if baseline is None:
        summary = f"New rule — {len(items)} field(s) set (no published baseline)."
    elif not items:
        summary = "No field-level changes vs the currently-published version."
    else:
        added = sum(1 for i in items if i.change_type == "added")
        removed = sum(1 for i in items if i.change_type == "removed")
        changed = sum(1 for i in items if i.change_type == "changed")
        summary = f"{changed} changed, {added} added, {removed} removed vs published."
    return items, summary


def _norm(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)
