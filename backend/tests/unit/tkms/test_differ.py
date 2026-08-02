"""Pure snapshot differ — golden change reports."""
from datetime import date
from decimal import Decimal

from app.services.tkms.comparison.differ import diff_snapshots


def _snap(**kw) -> dict:
    base = {
        "description": "Medical credit",
        "effective_date": date(2025, 1, 1),
        "expiry_date": None,
        "max_amount": Decimal("2759"),
        "min_amount": None,
        "income_threshold_low": None,
        "income_threshold_high": None,
        "reduction_rate": Decimal("0.03"),
        "formula_expression": "net 0.03 *",
        "condition_count": 0,
    }
    base.update(kw)
    return base


def test_no_baseline_marks_everything_added():
    items, summary = diff_snapshots(None, _snap())
    assert all(i.change_type == "added" for i in items)
    assert "New rule" in summary
    # nullable-empty fields are not emitted
    fields = {i.field for i in items}
    assert "expiry_date" not in fields
    assert "description" in fields


def test_identical_snapshots_have_no_changes():
    items, summary = diff_snapshots(_snap(), _snap())
    assert items == []
    assert "No field-level changes" in summary


def test_changed_added_removed():
    baseline = _snap(max_amount=Decimal("2759"), reduction_rate=Decimal("0.03"), expiry_date=None)
    draft = _snap(max_amount=Decimal("3000"), reduction_rate=None, expiry_date=date(2025, 12, 31))
    items, summary = diff_snapshots(baseline, draft)
    by_field = {i.field: i for i in items}
    assert by_field["max_amount"].change_type == "changed"
    assert by_field["max_amount"].old_value == "2759"
    assert by_field["max_amount"].new_value == "3000"
    assert by_field["reduction_rate"].change_type == "removed"
    assert by_field["expiry_date"].change_type == "added"
    assert "changed" in summary
