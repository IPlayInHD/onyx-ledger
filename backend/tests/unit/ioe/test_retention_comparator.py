"""Retention comparator — the pure diff, over synthetic snapshots.

What is at stake: that this engine reports state TRANSITIONS and never clock
drift, presentation order, or a first visit. The mandatory acceptance is §47 —
a countdown moving inside its band must produce nothing at all, while the band
transitions must each produce exactly one change.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.ioe.retention.domain import (
    BaselineStatus,
    ChangeCategory,
    ChangeKind,
    ChangeSeverity,
    compare_retention_snapshots,
)
from app.services.ioe.retention.snapshot import (
    FamilySnapshot,
    OpportunitySnapshot,
    RetentionSnapshot,
    snapshot_hash,
    snapshot_payload,
)

AS_OF = "2026-03-01"
TAX_YEAR = 2025


# ---------------------------------------------------------------- builders --
def _opportunity(
    code: str = "rrsp_topup",
    *,
    availability: str = "AVAILABLE",
    decision: str = "NO_DECISION",
    execution: str = "NOT_REPORTED",
    evidence: str = "READY",
    timing: str = "NORMAL",
    freshness: str = "current",
    integrity: str = "not_checked",
    integrity_reason_code: str = "NONE",
    deadline_code: str | None = "DL_RRSP",
    deadline_date: str | None = "2026-04-30",
) -> OpportunitySnapshot:
    return OpportunitySnapshot(
        opportunity_code=code, availability=availability, decision=decision,
        execution=execution, evidence=evidence, timing=timing,
        freshness=freshness, integrity=integrity,
        integrity_reason_code=integrity_reason_code,
        deadline_code=deadline_code, deadline_date=deadline_date)


def _snapshot(
    items: list[OpportunitySnapshot],
    *,
    families: list[FamilySnapshot] | None = None,
    as_of: str = AS_OF,
    authority: str = "READY",
) -> RetentionSnapshot:
    return RetentionSnapshot(
        schema_version="1.0.0", tax_year=TAX_YEAR, evaluated_as_of=as_of,
        opportunity_authority=authority,
        opportunity_authority_reason="OPTIMIZATION_RUN_AUTHORITATIVE",
        opportunities=tuple(sorted(items, key=lambda o: o.opportunity_code)),
        families=tuple(families or [
            FamilySnapshot("OPPORTUNITY", "READY", "OPTIMIZATION_RUN_AUTHORITATIVE")
        ]))


def _compare(before: RetentionSnapshot | None, after: RetentionSnapshot):
    return compare_retention_snapshots(
        before, after,
        current_hash=snapshot_hash(after),
        acknowledged_hash=snapshot_hash(before) if before is not None else None)


def _only(changeset):
    (change,) = changeset.changes
    return change


# ===========================================================================
# §47 — THE MANDATORY NOISE ACCEPTANCE
# ===========================================================================
def test_a_countdown_inside_one_band_is_not_a_change():
    """38 -> 37 days. The single most important assertion in this suite: it is
    the difference between a product that tells you something and one that
    interrupts you daily to say time passed.

    It holds structurally, not by filtering — `days_remaining` is not a field
    the snapshot records, so there is nothing here to suppress.
    """
    before = _snapshot([_opportunity(timing="NORMAL")])
    after = _snapshot([_opportunity(timing="NORMAL")], as_of="2026-03-02")

    result = _compare(before, after)

    assert result.changes == ()
    assert result.summary.total == 0
    assert "days_remaining" not in str(snapshot_payload(after))


@pytest.mark.parametrize("before_band,after_band", [
    ("NORMAL", "APPROACHING"),
    ("APPROACHING", "URGENT"),
    ("URGENT", "EXPIRED"),
])
def test_each_band_transition_is_exactly_one_material_change(
        before_band, after_band):
    result = _compare(
        _snapshot([_opportunity(timing=before_band)]),
        _snapshot([_opportunity(timing=after_band)]))

    change = _only(result)
    assert change.category is ChangeCategory.TIMING
    assert change.kind is ChangeKind.CHANGED
    assert change.transitions == (
        change.transitions[0],)  # one field moved, not several
    assert change.transitions[0].field == "timing"
    assert change.transitions[0].before == before_band
    assert change.transitions[0].after == after_band


def test_an_as_of_advance_alone_changes_nothing():
    """The request date is recorded but is not itself state. Two reads a week
    apart over identical product state must agree there is no news."""
    before = _snapshot([_opportunity()], as_of="2026-03-01")
    after = _snapshot([_opportunity()], as_of="2026-03-08")
    assert _compare(before, after).changes == ()


def test_input_order_is_not_a_change():
    """Snapshots sort by semantic identity, so the order a source yielded rows
    in cannot reach the comparison."""
    a = _opportunity("aaa")
    b = _opportunity("bbb")
    forward = _snapshot([a, b])
    backward = _snapshot([b, a])
    assert _compare(forward, backward).changes == ()
    assert snapshot_hash(forward) == snapshot_hash(backward)


def test_excluded_noise_never_reaches_the_snapshot():
    """The exclusion list is the noise-suppression contract; assert it on the
    stored bytes so a future field cannot quietly rejoin them."""
    rendered = str(snapshot_payload(_snapshot([_opportunity()])))
    for noisy in ("days_remaining", "candidate_rank", "source_id",
                  "actionability", "attention", "support", "reason_codes",
                  "journal", "standalone_potential"):
        assert noisy not in rendered, f"{noisy} reached the retention snapshot"


# ===========================================================================
# §50 / §4 — first use
# ===========================================================================
def test_no_baseline_reports_no_baseline_and_invents_no_arrivals():
    current = _snapshot([_opportunity(f"opp_{i}") for i in range(10)])

    result = compare_retention_snapshots(
        None, current, current_hash=snapshot_hash(current))

    assert result.baseline_status is BaselineStatus.NO_BASELINE
    assert result.changes == ()
    assert result.summary.total == 0
    assert result.summary.opportunities_added == 0, (
        "a first visit was described as ten things happening")
    assert result.baseline_snapshot_hash is None
    assert result.current_snapshot_hash == snapshot_hash(current)


def test_an_established_baseline_over_identical_state_is_silent():
    snapshot = _snapshot([_opportunity(f"opp_{i}") for i in range(10)])
    result = _compare(snapshot, snapshot)
    assert result.baseline_status is BaselineStatus.ESTABLISHED
    assert result.changes == ()


def test_a_missing_baseline_is_not_an_empty_baseline():
    """The distinction §4 turns on. Against NO baseline: silence. Against an
    EMPTY one: ten arrivals, because something really did appear."""
    current = _snapshot([_opportunity(f"opp_{i}") for i in range(10)])

    no_baseline = compare_retention_snapshots(
        None, current, current_hash=snapshot_hash(current))
    empty_baseline = _compare(_snapshot([]), current)

    assert no_baseline.changes == ()
    assert len(empty_baseline.changes) == 10
    assert all(c.kind is ChangeKind.ADDED for c in empty_baseline.changes)


# ===========================================================================
# §51 — opportunity presence and availability
# ===========================================================================
def test_an_opportunity_that_appears_is_added():
    result = _compare(_snapshot([]), _snapshot([_opportunity("new_one")]))
    change = _only(result)
    assert change.kind is ChangeKind.ADDED
    assert change.category is ChangeCategory.OPPORTUNITY
    assert change.subject == "new_one"


def test_an_opportunity_that_disappears_is_removed_and_never_called_expired():
    """§13: disappearance from the authoritative view and deadline expiry are
    different facts. Only the lifecycle authority may say EXPIRED."""
    result = _compare(_snapshot([_opportunity("gone")]), _snapshot([]))

    change = _only(result)
    assert change.kind is ChangeKind.REMOVED
    assert change.category is ChangeCategory.OPPORTUNITY
    assert "EXPIRED" not in str(change), (
        "a disappearance was reported as an expiry")


@pytest.mark.parametrize("before,after,expected_severity", [
    ("AVAILABLE", "BLOCKED", ChangeSeverity.HIGH),
    ("BLOCKED", "AVAILABLE", ChangeSeverity.MEDIUM),
])
def test_availability_transitions_are_material(before, after, expected_severity):
    result = _compare(
        _snapshot([_opportunity(availability=before)]),
        _snapshot([_opportunity(availability=after)]))
    change = _only(result)
    assert change.category is ChangeCategory.OPPORTUNITY
    assert change.kind is ChangeKind.CHANGED
    assert change.severity is expected_severity


# ===========================================================================
# §52 — Journal-driven transitions, reached through the lifecycle
# ===========================================================================
@pytest.mark.parametrize("before,after", [
    ("NO_DECISION", "PROCEED"),
    ("CONSIDERING", "PROCEED"),
    ("DEFER", "PROCEED"),
    ("PROCEED", "DECLINE"),
])
def test_a_decision_transition_is_one_decision_change(before, after):
    result = _compare(
        _snapshot([_opportunity(decision=before)]),
        _snapshot([_opportunity(decision=after)]))
    change = _only(result)
    assert change.category is ChangeCategory.DECISION
    assert change.transitions[0].before == before
    assert change.transitions[0].after == after


def test_a_reported_action_is_an_execution_change_and_not_verification():
    result = _compare(
        _snapshot([_opportunity(execution="NOT_REPORTED")]),
        _snapshot([_opportunity(execution="USER_REPORTED")]))
    change = _only(result)
    assert change.category is ChangeCategory.EXECUTION
    assert change.transitions[0].after == "USER_REPORTED"
    assert "VERIFIED" not in str(change)
    assert result.summary.execution_reports == 1


def test_one_visit_that_moves_two_axes_is_two_categorised_changes_not_five():
    """A user who proceeds AND reports acting moved two governed axes. That is
    two changes, one per axis — not one blurred event, and not a record per
    differing field."""
    result = _compare(
        _snapshot([_opportunity(decision="DEFER", execution="NOT_REPORTED")]),
        _snapshot([_opportunity(decision="PROCEED", execution="USER_REPORTED")]))

    assert len(result.changes) == 2
    assert {c.category for c in result.changes} == {
        ChangeCategory.DECISION, ChangeCategory.EXECUTION}
    assert all(c.subject == "rrsp_topup" for c in result.changes)


# ===========================================================================
# §53 — evidence
# ===========================================================================
@pytest.mark.parametrize("before,after,improved", [
    ("MISSING", "READY", True),
    ("PARTIAL", "READY", True),
    ("READY", "MISSING", False),
    ("UNKNOWN", "READY", True),
])
def test_evidence_transitions_are_material_and_directional(
        before, after, improved):
    result = _compare(
        _snapshot([_opportunity(evidence=before)]),
        _snapshot([_opportunity(evidence=after)]))
    change = _only(result)
    assert change.category is ChangeCategory.EVIDENCE
    assert result.summary.evidence_improvements == (1 if improved else 0)
    assert result.summary.evidence_regressions == (0 if improved else 1)


def test_a_not_required_transition_is_reported_but_not_graded():
    """NOT_REQUIRED is not a rung on the readiness ladder — nothing was
    demanded. The change is real; calling it an improvement or a regression
    would be inventing an ordering the authority does not define."""
    result = _compare(
        _snapshot([_opportunity(evidence="NOT_REQUIRED")]),
        _snapshot([_opportunity(evidence="MISSING")]))
    change = _only(result)
    assert change.category is ChangeCategory.EVIDENCE
    assert result.summary.evidence_improvements == 0
    assert result.summary.evidence_regressions == 0


def test_no_document_identity_can_appear_in_a_change():
    """Evidence retention is about readiness. The snapshot has no field that
    could carry storage identity, so this asserts the shape, not a filter."""
    result = _compare(
        _snapshot([_opportunity(evidence="MISSING")]),
        _snapshot([_opportunity(evidence="READY")]))
    rendered = str(result)
    for leak in ("bucket", "object_key", "content_hash", "document_id",
                 "filename"):
        assert leak not in rendered


# ===========================================================================
# §54 — freshness and integrity never collapse
# ===========================================================================
def test_freshness_alone_is_a_freshness_change():
    result = _compare(
        _snapshot([_opportunity(freshness="current")]),
        _snapshot([_opportunity(freshness="stale")]))
    change = _only(result)
    assert change.category is ChangeCategory.FRESHNESS
    assert result.summary.freshness_changes == 1
    assert result.summary.integrity_changes == 0


def test_integrity_alone_is_an_integrity_change_and_keeps_its_reason():
    result = _compare(
        _snapshot([_opportunity(integrity="not_checked",
                                integrity_reason_code="NONE")]),
        _snapshot([_opportunity(integrity="mismatch",
                                integrity_reason_code="HASH_MISMATCH")]))
    change = _only(result)
    assert change.category is ChangeCategory.INTEGRITY
    assert change.severity is ChangeSeverity.HIGH
    reasons = [t for t in change.transitions if t.field == "integrity_reason_code"]
    assert reasons and reasons[0].after == "HASH_MISMATCH", (
        "the governed integrity reason was dropped")
    assert result.summary.freshness_changes == 0


def test_freshness_and_integrity_together_stay_two_changes():
    """§18: they must never be interchangeable, and never collapse into one
    generic 'outdated'."""
    result = _compare(
        _snapshot([_opportunity(freshness="current", integrity="not_checked")]),
        _snapshot([_opportunity(freshness="stale", integrity="mismatch")]))

    assert {c.category for c in result.changes} == {
        ChangeCategory.FRESHNESS, ChangeCategory.INTEGRITY}
    assert result.summary.freshness_changes == 1
    assert result.summary.integrity_changes == 1
    assert "outdated" not in str(result).lower()


# ===========================================================================
# Deadline value vs timing band
# ===========================================================================
def test_a_deadline_that_moves_is_a_deadline_change_even_inside_one_band():
    """A deadline can shift months and stay NORMAL. The band cannot express
    that, so the governed date is carried and compared in its own right."""
    result = _compare(
        _snapshot([_opportunity(deadline_date="2026-04-30", timing="NORMAL")]),
        _snapshot([_opportunity(deadline_date="2026-06-30", timing="NORMAL")]))
    change = _only(result)
    assert change.category is ChangeCategory.DEADLINE
    assert change.transitions[0].after == "2026-06-30"


def test_a_deadline_code_and_date_moving_together_is_one_change():
    result = _compare(
        _snapshot([_opportunity(deadline_code="DL_A", deadline_date="2026-04-30")]),
        _snapshot([_opportunity(deadline_code="DL_B", deadline_date="2026-06-30")]))
    change = _only(result)
    assert change.category is ChangeCategory.DEADLINE
    assert len(change.transitions) == 2


# ===========================================================================
# Assurance families
# ===========================================================================
def test_a_family_status_change_is_an_assurance_change():
    result = _compare(
        _snapshot([], families=[FamilySnapshot("OPPORTUNITY", "READY", "OK")]),
        _snapshot([], families=[
            FamilySnapshot("OPPORTUNITY", "UNAVAILABLE", "NO_RUN")]))
    change = _only(result)
    assert change.category is ChangeCategory.ASSURANCE
    assert change.subject == "OPPORTUNITY"
    assert change.transitions[0].after == "UNAVAILABLE"
    assert change.transitions[1].after == "NO_RUN", (
        "the governed family reason was dropped")


def test_an_unchanged_family_reports_nothing():
    families = [FamilySnapshot("OPPORTUNITY", "READY", "OK")]
    assert _compare(
        _snapshot([], families=families),
        _snapshot([], families=families)).changes == ()


# ===========================================================================
# Ordering, severity and identity
# ===========================================================================
def test_changes_are_ordered_by_attention_then_category_then_subject():
    before = _snapshot([
        _opportunity("a_expiring", timing="URGENT"),
        _opportunity("b_evidence", evidence="MISSING"),
        _opportunity("c_decision", decision="NO_DECISION"),
    ])
    after = _snapshot([
        _opportunity("a_expiring", timing="EXPIRED"),
        _opportunity("b_evidence", evidence="READY"),
        _opportunity("c_decision", decision="PROCEED"),
    ])

    result = _compare(before, after)

    assert [(c.severity.value, c.subject) for c in result.changes] == [
        ("CRITICAL", "a_expiring"),   # became EXPIRED
        ("MEDIUM", "b_evidence"),     # evidence moved
        ("LOW", "c_decision"),        # the user answered
    ]


def test_nothing_is_ordered_by_money():
    """The comparator never sees an amount, so it cannot rank by one."""
    source = Path(__file__).parents[3] / "app/services/ioe/retention"
    for module in ("domain.py", "snapshot.py"):
        text = (source / module).read_text()
        for banned in ("standalone_potential", "incremental_portfolio_benefit",
                       "candidate_rank", "support_score"):
            assert f'"{banned}"' not in text, (
                f"{module} names {banned}; retention must not rank by amount")


def test_no_numeric_retention_score_exists():
    from app.services.ioe.retention import domain

    assert not any(
        name.endswith("_score") for name in dir(domain)), (
        "a retention score appeared; §19 forbids one")


def test_change_identity_is_stable_and_distinguishes_states():
    before = _snapshot([_opportunity(timing="NORMAL")])
    after = _snapshot([_opportunity(timing="URGENT")])
    other = _snapshot([_opportunity(timing="EXPIRED")])

    first = _only(_compare(before, after)).change_id
    again = _only(_compare(before, after)).change_id
    different = _only(_compare(before, other)).change_id

    assert first == again, "the same comparison produced two identities"
    assert first != different
    assert len(first) == 64, "not a sha-256 hex digest"


def test_the_change_id_is_not_a_row_id():
    change = _only(_compare(
        _snapshot([_opportunity(timing="NORMAL")]),
        _snapshot([_opportunity(timing="URGENT")])))
    assert not change.change_id.isdigit()
    int(change.change_id, 16)  # hex, therefore a digest rather than a counter


# ===========================================================================
# Determinism
# ===========================================================================
def test_repeated_comparison_is_identical():
    before = _snapshot([_opportunity(f"opp_{i}", timing="NORMAL")
                        for i in range(5)])
    after = _snapshot([_opportunity(f"opp_{i}", timing="URGENT")
                       for i in range(5)])
    assert _compare(before, after) == _compare(before, after)


def test_the_comparison_is_invariant_under_pythonhashseed(tmp_path: Path):
    """Set and dict iteration order move with the seed; the change set and
    every change identity must not."""
    script = tmp_path / "run.py"
    script.write_text(
        "import json\n"
        "from app.services.ioe.retention.domain import "
        "compare_retention_snapshots\n"
        "from app.services.ioe.retention.snapshot import (\n"
        "    FamilySnapshot, OpportunitySnapshot, RetentionSnapshot, "
        "snapshot_hash)\n"
        "def opp(code, **kw):\n"
        "    base = dict(availability='AVAILABLE', decision='NO_DECISION',\n"
        "        execution='NOT_REPORTED', evidence='READY', timing='NORMAL',\n"
        "        freshness='current', integrity='not_checked',\n"
        "        integrity_reason_code='NONE', deadline_code='DL',\n"
        "        deadline_date='2026-04-30')\n"
        "    base.update(kw)\n"
        "    return OpportunitySnapshot(opportunity_code=code, **base)\n"
        "def snap(items):\n"
        "    return RetentionSnapshot('1.0.0', 2025, '2026-03-01', 'READY',\n"
        "        'OK', tuple(items), (FamilySnapshot('OPPORTUNITY','READY','OK'),))\n"
        "b = snap([opp(f'o{i}') for i in range(12)])\n"
        "c = snap([opp(f'o{i}', timing='URGENT', decision='PROCEED')\n"
        "          for i in range(12)])\n"
        "r = compare_retention_snapshots(b, c, current_hash=snapshot_hash(c),\n"
        "    acknowledged_hash=snapshot_hash(b))\n"
        "print(json.dumps({'hash': r.current_snapshot_hash,\n"
        "  'ids': [x.change_id for x in r.changes],\n"
        "  'order': [(x.category.value, x.subject) for x in r.changes]}))\n"
    )
    outputs = set()
    for seed in ("0", "1", "42"):
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, check=True,
            cwd=str(Path(__file__).parents[3]),
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin",
                 "PYTHONPATH": str(Path(__file__).parents[3])})
        outputs.add(result.stdout.strip())
    assert len(outputs) == 1, f"seed variation changed the result: {outputs}"


def test_the_snapshot_hash_binds_schema_version_and_as_of():
    base = _snapshot([_opportunity()])
    moved = _snapshot([_opportunity()], as_of="2026-03-02")
    assert snapshot_hash(base) != snapshot_hash(moved), (
        "the hash ignores the date that produced the timing band")


def test_the_snapshot_hash_ignores_nothing_material():
    base = _snapshot([_opportunity()])
    for field, value in (("availability", "BLOCKED"), ("decision", "PROCEED"),
                         ("execution", "USER_REPORTED"), ("evidence", "MISSING"),
                         ("timing", "URGENT"), ("freshness", "stale"),
                         ("integrity", "mismatch"),
                         ("deadline_date", "2026-06-30")):
        assert snapshot_hash(_snapshot([_opportunity(**{field: value})])) != \
            snapshot_hash(base), f"{field} does not reach the snapshot hash"


# ===========================================================================
# §20 — no financial claims
# ===========================================================================
def test_the_change_set_makes_no_money_claim():
    result = _compare(
        _snapshot([_opportunity(timing="URGENT")]),
        _snapshot([]))
    rendered = str(result).lower()
    for claim in ("missed", "lost", "saved", "savings", "$"):
        assert claim not in rendered, f"the change set claims {claim!r}"


def test_comparator_cost_and_snapshot_size_are_measured():
    """§58/§59: the comparator must scale with the SUM of the two sides, and a
    stored snapshot must be small enough that keeping history is cheap.

    Both are reported rather than merely bounded — a storage figure nobody
    looked at is how a table quietly becomes the largest one in the database.
    """
    import json
    import time

    per_item = {}
    report = []
    for label, size in (("small", 10), ("moderate", 200), ("stress", 2000)):
        # Every item differs on one axis, so the comparison does maximal work
        # rather than short-circuiting on equality.
        before = _snapshot([_opportunity(f"opp_{i:05d}", timing="NORMAL")
                            for i in range(size)])
        after = _snapshot([_opportunity(f"opp_{i:05d}", timing="URGENT")
                           for i in range(size)])
        current_hash = snapshot_hash(after)
        baseline_hash = snapshot_hash(before)

        timings = []
        for _ in range(5):
            start = time.perf_counter()
            result = compare_retention_snapshots(
                before, after, current_hash=current_hash,
                acknowledged_hash=baseline_hash)
            timings.append((time.perf_counter() - start) * 1000)
        timings.sort()
        elapsed = timings[len(timings) // 2]
        per_item[label] = elapsed / size

        stored = json.dumps(snapshot_payload(after), separators=(",", ":"))
        report.append({
            "label": label, "opportunities": size,
            "compare_ms": round(elapsed, 3),
            "us_per_item": round(per_item[label] * 1000, 3),
            "snapshot_bytes": len(stored.encode()),
            "bytes_per_opportunity": round(len(stored.encode()) / size),
            "x100_checkpoints_kb": round(len(stored.encode()) * 100 / 1024),
        })
        # Non-vacuous: the comparison really did produce one change per item.
        assert len(result.changes) == size

    print("\nretention comparator cost and storage:")             # noqa: T201
    for row in report:
        print(f"  {row}")                                         # noqa: T201
    assert per_item["stress"] < per_item["small"] * 8, per_item
