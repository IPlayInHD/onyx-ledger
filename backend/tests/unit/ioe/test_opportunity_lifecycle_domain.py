"""Opportunity Lifecycle — the pure join, over synthetic Assurance + Journal.

What is at stake: that seven axes stay independent. Expiry must not rewrite a
decision, a decision must not imply an action, a reported action must not
become verification, staleness must not become expiry, and an absent authority
must not read as readiness.
"""
import ast
import subprocess
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from app.services.ioe.journal.domain import Decision, ExecutionState
from app.services.ioe.lifecycle.domain import (
    Availability,
    DecisionState,
    JournalThreadView,
    LifecycleActionability,
    derive_opportunity_lifecycle,
)
from app.services.state_graph.assurance import (
    ActionStatus,
    AssuranceStatus,
    AssuranceSummary,
    DeadlineAssurance,
    FamilyAssurance,
    OpportunityAssurance,
    SupportAssurance,
    TaxAssuranceMap,
    UrgencyStatus,
)

AS_OF = date(2026, 3, 1)
TAX_YEAR = 2025


# ---------------------------------------------------------------- builders --
def _support(raw="80.00", adjusted="80.00") -> SupportAssurance:
    return SupportAssurance(
        raw_support_score=raw, assumption_adjusted_score=adjusted,
        display_support_score=adjusted, cap_applied=False, cap_reason_code=None)


def _deadline(days: int, code: str = "DL") -> DeadlineAssurance:
    """A deadline `days` from AS_OF, with the urgency Assurance would assign."""
    when = AS_OF + timedelta(days=days)
    if days < 0:
        urgency = UrgencyStatus.EXPIRED
    elif days <= 14:
        urgency = UrgencyStatus.URGENT
    elif days <= 60:
        urgency = UrgencyStatus.APPROACHING
    else:
        urgency = UrgencyStatus.NORMAL
    return DeadlineAssurance(
        deadline_code=code, deadline_date=when.isoformat(), is_hard=True,
        days_remaining=days, urgency=urgency)


def _opportunity(
    code: str = "RRSP_TOPUP",
    *,
    source_id: str | None = None,
    status: AssuranceStatus = AssuranceStatus.READY,
    action: ActionStatus = ActionStatus.ACTION_AVAILABLE,
    eligibility: str = "eligible",
    evidence: str = "READY",
    deadline: DeadlineAssurance | None = None,
    freshness: str = "current",
    stale_reasons: tuple[str, ...] = (),
    integrity: str = "not_checked",
    rank: int | None = 1,
    raw: str = "80.00",
    adjusted: str = "80.00",
) -> OpportunityAssurance:
    return OpportunityAssurance(
        opportunity_code=code,
        source_id=source_id or f"cand-{code}",
        eligibility_status=eligibility,
        status=status,
        action=action,
        blocked_reason_code=(
            "CONFLICT" if status is AssuranceStatus.BLOCKED else None),
        review_reason_codes=(
            ("GOVERNED_RE_EVALUATION_REQUESTED",)
            if status is AssuranceStatus.REVIEW_REQUIRED else ()),
        evidence_readiness=evidence,
        evidence_requirements=(),
        deadline=deadline,
        deadline_count=1 if deadline else 0,
        urgency=deadline.urgency if deadline else UrgencyStatus.NO_DEADLINE,
        support=_support(raw, adjusted),
        assumption_dependent=raw != adjusted,
        standalone_potential="1200.00",
        incremental_portfolio_benefit=None,
        candidate_rank=rank,
        freshness=freshness,
        stale_reason_codes=stale_reasons,
        integrity=integrity,
        integrity_reason_code="NONE",
    )


def _assurance(
    items: list[OpportunityAssurance],
    *,
    family_status: AssuranceStatus = AssuranceStatus.READY,
    family_reason: str = "OPTIMIZATION_RUN_AUTHORITATIVE",
) -> TaxAssuranceMap:
    empty = {"x": 0}
    return TaxAssuranceMap(
        contract_version="1.0.0", view="current", tax_year=TAX_YEAR,
        as_of=AS_OF.isoformat(), graph_hash="deadbeef",
        families=(FamilyAssurance(
            family="OPPORTUNITY", status=family_status,
            reason_code=family_reason, item_count=len(items)),),
        opportunities=tuple(items),
        attention=tuple(i.source_id for i in items),
        assumption_codes=(),
        summary=AssuranceSummary(
            opportunity_count=len(items), opportunities_by_status=empty,
            opportunities_by_action=empty, opportunities_by_urgency=empty,
            assumption_dependent_count=0, upcoming_deadline_count=0,
            families_by_status=empty),
    )


def _thread(
    subject: str | None,
    *,
    decision: Decision = Decision.CONSIDERING,
    execution: ExecutionState = ExecutionState.NOT_REPORTED,
    minutes: int = 0,
    action_date: date | None = None,
    journal_id: uuid.UUID | None = None,
) -> JournalThreadView:
    return JournalThreadView(
        journal_id=journal_id or uuid.uuid4(),
        subject=subject,
        created_at=datetime(2026, 2, 1, 12, tzinfo=UTC) + timedelta(minutes=minutes),
        decision=decision, execution=execution,
        last_reported_action_date=action_date)


def _only(lifecycle):
    (entry,) = lifecycle.opportunities
    return entry


# ===========================================================================
# Timing — reused verbatim, never recomputed
# ===========================================================================
@pytest.mark.parametrize("days,expected", [
    (-1, UrgencyStatus.EXPIRED),
    (0, UrgencyStatus.URGENT),      # same-day: URGENT, per the certified rule
    (14, UrgencyStatus.URGENT),
    (15, UrgencyStatus.APPROACHING),
    (60, UrgencyStatus.APPROACHING),
    (61, UrgencyStatus.NORMAL),
])
def test_timing_is_the_assurance_verdict_verbatim(days, expected):
    entry = _only(derive_opportunity_lifecycle(
        _assurance([_opportunity(deadline=_deadline(days))]), [], as_of=AS_OF))
    assert entry.timing is expected
    assert entry.days_remaining == days


def test_no_governed_deadline_neither_expires_nor_invents_days():
    entry = _only(derive_opportunity_lifecycle(
        _assurance([_opportunity(deadline=None)]), [], as_of=AS_OF))
    assert entry.timing is UrgencyStatus.NO_DEADLINE
    assert entry.deadline is None
    assert entry.days_remaining is None
    assert entry.attention.expired is False
    assert entry.attention.deadline_urgent is False


def test_the_lifecycle_defines_no_threshold_of_its_own():
    """The whole point of reusing Assurance's authority: a day count evaluated
    here would be a second answer to "is this urgent".

    Checked over the parsed module rather than its text, for two reasons. Prose
    MUST stay free to name the thresholds — the docstring explaining that they
    are governed by Assurance is the documentation this delegation depends on,
    and a substring rule would forbid exactly the sentence worth writing. And
    the AST is the stricter test: `14` reached as `1 + 13` defeats a grep and
    not this.
    """
    tree = ast.parse((Path(__file__).parents[3]
                      / "app/services/ioe/lifecycle/domain.py").read_text())
    ints = {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, int)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}

    # Non-vacuous: the walk sees real code, and that code reads the verdict.
    assert "UrgencyStatus" in names, "the AST walk found no Assurance reference"

    assert not ints & {14, 60}, (
        f"lifecycle/domain.py evaluates day thresholds {sorted(ints & {14, 60})}; "
        "timing must come from the Assurance verdict")
    for constant in ("URGENT_WITHIN_DAYS", "APPROACHING_WITHIN_DAYS"):
        assert constant not in names, (
            f"lifecycle/domain.py reads {constant}; it must consume the urgency "
            "Assurance already derived, not re-apply the threshold itself")


# ===========================================================================
# Journal join
# ===========================================================================
def test_without_a_thread_there_is_no_decision():
    entry = _only(derive_opportunity_lifecycle(
        _assurance([_opportunity()]), [], as_of=AS_OF))
    assert entry.decision is DecisionState.NO_DECISION
    assert entry.execution is ExecutionState.NOT_REPORTED
    assert entry.journal is None


@pytest.mark.parametrize("decision,expected", [
    (Decision.CONSIDERING, DecisionState.CONSIDERING),
    (Decision.PROCEED, DecisionState.PROCEED),
    (Decision.DEFER, DecisionState.DEFER),
    (Decision.DECLINE, DecisionState.DECLINE),
])
def test_the_journal_decision_is_carried_verbatim(decision, expected):
    entry = _only(derive_opportunity_lifecycle(
        _assurance([_opportunity("A_CODE")]),
        [_thread("A_CODE", decision=decision)], as_of=AS_OF))
    assert entry.decision is expected


def test_a_thread_naming_no_opportunity_is_counted_not_dropped():
    lifecycle = derive_opportunity_lifecycle(
        _assurance([_opportunity("A_CODE")]),
        [_thread(None), _thread(None), _thread("A_CODE")], as_of=AS_OF)
    assert lifecycle.summary.unlinked_thread_count == 2
    assert _only(lifecycle).journal is not None


def test_a_thread_for_another_opportunity_does_not_attach():
    entry = _only(derive_opportunity_lifecycle(
        _assurance([_opportunity("A_CODE")]),
        [_thread("B_CODE", decision=Decision.PROCEED)], as_of=AS_OF))
    assert entry.decision is DecisionState.NO_DECISION
    assert entry.journal is None


def test_the_newest_thread_decides_and_the_others_are_counted():
    """Multiple threads per opportunity are possible — the schema has no
    uniqueness constraint — so the choice must be deterministic and the rest
    must not vanish."""
    old = _thread("A_CODE", decision=Decision.DEFER, minutes=0)
    new = _thread("A_CODE", decision=Decision.PROCEED, minutes=60)
    forward = _only(derive_opportunity_lifecycle(
        _assurance([_opportunity("A_CODE")]), [old, new], as_of=AS_OF))
    reversed_ = _only(derive_opportunity_lifecycle(
        _assurance([_opportunity("A_CODE")]), [new, old], as_of=AS_OF))

    assert forward.decision is DecisionState.PROCEED
    assert forward.journal.journal_id == new.journal_id
    assert forward.journal.thread_count == 2
    assert reversed_.journal.journal_id == new.journal_id, (
        "input order changed which thread won")


def test_threads_created_in_the_same_instant_break_the_tie_by_id():
    a = _thread("A_CODE", decision=Decision.DEFER, minutes=0,
                journal_id=uuid.UUID(int=1))
    b = _thread("A_CODE", decision=Decision.PROCEED, minutes=0,
                journal_id=uuid.UUID(int=2))
    for order in ([a, b], [b, a]):
        entry = _only(derive_opportunity_lifecycle(
            _assurance([_opportunity("A_CODE")]), order, as_of=AS_OF))
        assert entry.journal.journal_id == uuid.UUID(int=2)


# ===========================================================================
# The axes do not collapse
# ===========================================================================
def test_proceed_is_not_an_action():
    entry = _only(derive_opportunity_lifecycle(
        _assurance([_opportunity("A_CODE")]),
        [_thread("A_CODE", decision=Decision.PROCEED)], as_of=AS_OF))
    assert entry.decision is DecisionState.PROCEED
    assert entry.execution is ExecutionState.NOT_REPORTED


def test_a_reported_action_with_missing_evidence_stays_visible_as_both():
    """The state the product must never round off to COMPLETE."""
    entry = _only(derive_opportunity_lifecycle(
        _assurance([_opportunity("A_CODE", evidence="MISSING",
                                 action=ActionStatus.EVIDENCE_REQUIRED)]),
        [_thread("A_CODE", decision=Decision.PROCEED,
                 execution=ExecutionState.USER_REPORTED,
                 action_date=date(2026, 2, 20))],
        as_of=AS_OF))
    assert entry.execution is ExecutionState.USER_REPORTED
    assert entry.evidence == "MISSING"
    assert entry.attention.needs_evidence is True
    assert entry.actionability is LifecycleActionability.ACTION_REPORTED
    assert entry.last_reported_action_date == date(2026, 2, 20)


def test_ready_evidence_after_a_report_is_not_verification():
    entry = _only(derive_opportunity_lifecycle(
        _assurance([_opportunity("A_CODE", evidence="READY")]),
        [_thread("A_CODE", decision=Decision.PROCEED,
                 execution=ExecutionState.USER_REPORTED)], as_of=AS_OF))
    assert entry.execution is ExecutionState.USER_REPORTED
    assert "SYSTEM_VERIFIED" not in {e.value for e in ExecutionState}
    assert "COMPLETE" not in {a.value for a in LifecycleActionability}


def test_declining_does_not_make_an_opportunity_ineligible():
    entry = _only(derive_opportunity_lifecycle(
        _assurance([_opportunity("A_CODE")]),
        [_thread("A_CODE", decision=Decision.DECLINE)], as_of=AS_OF))
    assert entry.decision is DecisionState.DECLINE
    assert entry.availability is Availability.AVAILABLE
    assert entry.actionability is LifecycleActionability.DECLINED


def test_deferring_does_not_expire_anything():
    entry = _only(derive_opportunity_lifecycle(
        _assurance([_opportunity("A_CODE", deadline=_deadline(90))]),
        [_thread("A_CODE", decision=Decision.DEFER)], as_of=AS_OF))
    assert entry.decision is DecisionState.DEFER
    assert entry.timing is UrgencyStatus.NORMAL
    assert entry.attention.expired is False


def test_staleness_is_not_expiry_and_expiry_is_not_staleness():
    stale = _only(derive_opportunity_lifecycle(
        _assurance([_opportunity("A_CODE", freshness="stale",
                                 stale_reasons=("INPUTS_CHANGED",),
                                 deadline=_deadline(90))]), [], as_of=AS_OF))
    expired = _only(derive_opportunity_lifecycle(
        _assurance([_opportunity("B_CODE", deadline=_deadline(-5))]),
        [], as_of=AS_OF))

    assert stale.attention.stale is True and stale.attention.expired is False
    assert expired.attention.expired is True and expired.attention.stale is False


def test_integrity_travels_separately_from_every_other_axis():
    entry = _only(derive_opportunity_lifecycle(
        _assurance([_opportunity("A_CODE", integrity="mismatch")]),
        [_thread("A_CODE", decision=Decision.PROCEED)], as_of=AS_OF))
    assert entry.integrity == "mismatch"
    assert entry.decision is DecisionState.PROCEED
    assert entry.availability is Availability.AVAILABLE


# ===========================================================================
# Availability
# ===========================================================================
@pytest.mark.parametrize("eligibility,expected", [
    ("eligible", Availability.AVAILABLE),
    ("conditionally_eligible", Availability.CONDITIONALLY_AVAILABLE),
    ("indeterminate", Availability.UNDETERMINED),
])
def test_availability_renames_governed_eligibility_and_nothing_more(
        eligibility, expected):
    entry = _only(derive_opportunity_lifecycle(
        _assurance([_opportunity(eligibility=eligibility)]), [], as_of=AS_OF))
    assert entry.availability is expected


def test_a_governed_exclusion_blocks_regardless_of_eligibility():
    entry = _only(derive_opportunity_lifecycle(
        _assurance([_opportunity(status=AssuranceStatus.BLOCKED,
                                 action=ActionStatus.BLOCKED)]), [], as_of=AS_OF))
    assert entry.availability is Availability.BLOCKED
    assert entry.actionability is LifecycleActionability.BLOCKED
    assert "GOVERNED_EXCLUSION_APPLIES" in entry.reason_codes


def test_no_run_reports_unavailable_authority_not_an_empty_ready():
    lifecycle = derive_opportunity_lifecycle(
        _assurance([], family_status=AssuranceStatus.UNAVAILABLE,
                   family_reason="NO_OPTIMIZATION_RUN_FOR_TAX_YEAR"),
        [], as_of=AS_OF)
    assert lifecycle.opportunities == ()
    assert lifecycle.opportunity_authority == "UNAVAILABLE"
    assert lifecycle.opportunity_authority_reason == (
        "NO_OPTIMIZATION_RUN_FOR_TAX_YEAR")


# ===========================================================================
# Expiry preserves history
# ===========================================================================
@pytest.mark.parametrize("decision,execution,expected_decision", [
    (Decision.CONSIDERING, ExecutionState.NOT_REPORTED, DecisionState.CONSIDERING),
    (Decision.PROCEED, ExecutionState.NOT_REPORTED, DecisionState.PROCEED),
    (Decision.DEFER, ExecutionState.NOT_REPORTED, DecisionState.DEFER),
    (Decision.PROCEED, ExecutionState.USER_REPORTED, DecisionState.PROCEED),
])
def test_expiry_never_rewrites_what_the_user_decided_or_reported(
        decision, execution, expected_decision):
    entry = _only(derive_opportunity_lifecycle(
        _assurance([_opportunity("A_CODE", deadline=_deadline(-3))]),
        [_thread("A_CODE", decision=decision, execution=execution)],
        as_of=AS_OF))
    assert entry.timing is UrgencyStatus.EXPIRED
    assert entry.decision is expected_decision
    assert entry.execution is execution


def test_an_expired_window_the_user_already_used_does_not_read_as_missed():
    """ACTION_REPORTED outranks EXPIRED deliberately: labelling a window the
    user acted within as 'expired' would imply they missed it."""
    entry = _only(derive_opportunity_lifecycle(
        _assurance([_opportunity("A_CODE", deadline=_deadline(-3))]),
        [_thread("A_CODE", decision=Decision.PROCEED,
                 execution=ExecutionState.USER_REPORTED)], as_of=AS_OF))
    assert entry.actionability is LifecycleActionability.ACTION_REPORTED
    assert entry.timing is UrgencyStatus.EXPIRED


def test_expired_opportunities_are_never_dropped_from_the_map():
    lifecycle = derive_opportunity_lifecycle(
        _assurance([
            _opportunity("LIVE_ONE", source_id="a", deadline=_deadline(90)),
            _opportunity("GONE_ONE", source_id="b", deadline=_deadline(-9)),
        ]), [], as_of=AS_OF)
    codes = {e.opportunity_code for e in lifecycle.opportunities}
    assert codes == {"LIVE_ONE", "GONE_ONE"}
    assert set(lifecycle.attention_order) == {"a", "b"}


# ===========================================================================
# Attention ordering
# ===========================================================================
def test_attention_orders_by_flag_then_urgency_then_gap_then_optimizer_rank():
    urgent_evidence = _opportunity(
        "URGENT_EV", source_id="a", evidence="MISSING",
        action=ActionStatus.EVIDENCE_REQUIRED, deadline=_deadline(5), rank=9)
    urgent_decision = _opportunity(
        "URGENT_DEC", source_id="b", status=AssuranceStatus.REVIEW_REQUIRED,
        action=ActionStatus.DECISION_REQUIRED, deadline=_deadline(5), rank=9)
    approaching = _opportunity(
        "APPROACH", source_id="c", status=AssuranceStatus.REVIEW_REQUIRED,
        action=ActionStatus.DECISION_REQUIRED, deadline=_deadline(30), rank=1)
    no_deadline_ev = _opportunity(
        "NO_DL_EV", source_id="d", evidence="MISSING",
        action=ActionStatus.EVIDENCE_REQUIRED, rank=1)
    expired = _opportunity("EXPIRED", source_id="e", deadline=_deadline(-2), rank=1)
    blocked = _opportunity(
        "BLOCKED", source_id="f", status=AssuranceStatus.BLOCKED,
        action=ActionStatus.BLOCKED, rank=1)
    declined = _opportunity("DECLINED", source_id="g", rank=1)

    lifecycle = derive_opportunity_lifecycle(
        _assurance([urgent_evidence, urgent_decision, approaching,
                    no_deadline_ev, expired, blocked, declined]),
        [_thread("DECLINED", decision=Decision.DECLINE)], as_of=AS_OF)

    by_id = {e.source_id: e.opportunity_code for e in lifecycle.opportunities}
    assert [by_id[s] for s in lifecycle.attention_order] == [
        "URGENT_EV",    # flagged, urgent, closable evidence gap
        "URGENT_DEC",   # flagged, urgent, decision owed
        "APPROACH",     # flagged, approaching
        "NO_DL_EV",     # flagged, no deadline, evidence gap
        "BLOCKED",      # flagged (blocked), no deadline band
        "EXPIRED",      # flagged, expired band last among flagged
        "DECLINED",     # unflagged: the user answered — settled work last
    ]


def test_a_declined_opportunity_is_ranked_last_but_never_hidden():
    lifecycle = derive_opportunity_lifecycle(
        _assurance([
            _opportunity("DECLINED", source_id="a", rank=1),
            _opportunity("OPEN_ONE", source_id="b", evidence="MISSING",
                         action=ActionStatus.EVIDENCE_REQUIRED, rank=9),
        ]),
        [_thread("DECLINED", decision=Decision.DECLINE)], as_of=AS_OF)
    assert lifecycle.attention_order == ("b", "a")
    assert len(lifecycle.opportunities) == 2


def test_the_lifecycle_never_reranks_by_amount():
    """Material-impact ordering stays delegated to the optimizer's sealed
    rank. If lifecycle sorted by money it would be a second optimizer."""
    small_rank_first = _opportunity(
        "SMALL", source_id="a", evidence="MISSING",
        action=ActionStatus.EVIDENCE_REQUIRED, rank=1)
    large_rank_last = _opportunity(
        "LARGE", source_id="b", evidence="MISSING",
        action=ActionStatus.EVIDENCE_REQUIRED, rank=2)
    lifecycle = derive_opportunity_lifecycle(
        _assurance([small_rank_first, large_rank_last]), [], as_of=AS_OF)
    assert lifecycle.attention_order == ("a", "b")


# ===========================================================================
# Determinism
# ===========================================================================
def test_input_order_cannot_change_the_map():
    items = [_opportunity("B_CODE", source_id="b", rank=2),
             _opportunity("A_CODE", source_id="a", rank=1)]
    threads = [_thread("A_CODE", decision=Decision.PROCEED, minutes=5),
               _thread("B_CODE", decision=Decision.DEFER, minutes=1)]
    forward = derive_opportunity_lifecycle(
        _assurance(list(items)), list(threads), as_of=AS_OF)
    backward = derive_opportunity_lifecycle(
        _assurance(list(reversed(items))), list(reversed(threads)), as_of=AS_OF)
    assert forward == backward


def test_repeated_derivation_is_identical():
    assurance = _assurance([_opportunity("A_CODE", deadline=_deadline(10))])
    threads = [_thread("A_CODE", decision=Decision.PROCEED)]
    assert (derive_opportunity_lifecycle(assurance, threads, as_of=AS_OF)
            == derive_opportunity_lifecycle(assurance, threads, as_of=AS_OF))


def test_the_map_is_invariant_under_pythonhashseed(tmp_path: Path):
    script = tmp_path / "derive.py"
    script.write_text(
        "import sys\n"
        "from datetime import date\n"
        "sys.path.insert(0, '.')\n"
        f"sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
        "from test_opportunity_lifecycle_domain import (\n"
        "    _assurance, _deadline, _opportunity, _thread)\n"
        "from app.services.ioe.journal.domain import Decision\n"
        "from app.services.ioe.lifecycle.domain import (\n"
        "    derive_opportunity_lifecycle)\n"
        "m = derive_opportunity_lifecycle(\n"
        "    _assurance([_opportunity('A_CODE', source_id='a',\n"
        "                             deadline=_deadline(5), evidence='MISSING'),\n"
        "                _opportunity('B_CODE', source_id='b')]),\n"
        "    [_thread('B_CODE', decision=Decision.DEFER)],\n"
        "    as_of=date(2026, 3, 1))\n"
        "print(m.attention_order, sorted(m.summary.by_timing.items()),\n"
        "      [(e.opportunity_code, e.actionability.value, e.decision.value)\n"
        "       for e in m.opportunities])\n"
    )
    rendered = set()
    for seed in ("0", "1", "42"):
        out = subprocess.run(                                       # noqa: S603
            [sys.executable, str(script)],
            capture_output=True, text=True, check=True,
            env={"PYTHONHASHSEED": seed, "PYTHONPATH": ".",
                 "PATH": "/usr/bin:/bin", "ONYX_JWT_SECRET": "x" * 40},
        )
        rendered.add(out.stdout.strip())
    assert len(rendered) == 1, f"the map moved with PYTHONHASHSEED: {rendered}"


def test_as_of_is_echoed_and_the_domain_reads_no_clock():
    source = (Path(__file__).parents[3]
              / "app/services/ioe/lifecycle/domain.py").read_text()
    assert "now()" not in source and "utcnow" not in source, (
        "the pure domain reads a clock; as_of must be the only time input")
    lifecycle = derive_opportunity_lifecycle(
        _assurance([_opportunity()]), [], as_of=AS_OF)
    assert lifecycle.as_of == AS_OF.isoformat()


# ===========================================================================
# Summary
# ===========================================================================
def test_the_summary_counts_the_entries_it_travels_with():
    lifecycle = derive_opportunity_lifecycle(
        _assurance([
            _opportunity("A_CODE", source_id="a", evidence="MISSING",
                         action=ActionStatus.EVIDENCE_REQUIRED),
            _opportunity("B_CODE", source_id="b", deadline=_deadline(-1)),
            _opportunity("C_CODE", source_id="c", status=AssuranceStatus.BLOCKED,
                         action=ActionStatus.BLOCKED),
        ]),
        [_thread("A_CODE", decision=Decision.PROCEED), _thread(None)],
        as_of=AS_OF)
    summary = lifecycle.summary

    assert summary.opportunity_count == 3
    assert sum(summary.by_availability.values()) == 3
    assert sum(summary.by_actionability.values()) == 3
    assert summary.by_decision["PROCEED"] == 1
    assert summary.by_decision["NO_DECISION"] == 2
    assert summary.by_timing["EXPIRED"] == 1
    assert summary.by_availability["BLOCKED"] == 1
    assert summary.unlinked_thread_count == 1
    assert summary.needing_attention == 3


def test_the_map_makes_no_money_or_strategy_claim():
    lifecycle = derive_opportunity_lifecycle(
        _assurance([_opportunity(deadline=_deadline(-5))]), [], as_of=AS_OF)
    rendered = repr(lifecycle).lower()
    for claim in ("missed", "lost", "savings", "decay_score", "priority_score",
                  "urgency_score", "recommended", "optimal", "probability"):
        assert claim not in rendered, f"the lifecycle asserts {claim!r}"


def test_join_cost_is_measured_and_scales_with_the_sum_not_the_product():
    """The docstring claims O(opportunities + threads): threads are indexed
    once into a keyed map, so no opportunity ever scans the thread list.

    Both dimensions grow together here, because that is exactly what a pairwise
    join would punish — the naive implementation is O(opportunities x threads),
    and at these sizes it would show as per-item cost climbing with scale. The
    ceiling is a shape check, not a budget.
    """
    import time

    per_item = {}
    report = []
    for label, size in (("small", 10), ("moderate", 200), ("stress", 2000)):
        items = [
            _opportunity(
                f"OPP_{i:05d}", source_id=f"cand-{i:05d}",
                evidence="MISSING" if i % 3 == 0 else "READY",
                deadline=_deadline(-1 + i % 120), rank=i)
            for i in range(size)
        ]
        assurance = _assurance(items)
        # One matching thread per opportunity, plus a tail that matches none —
        # unlinked threads must not cost a scan either.
        threads = [
            _thread(f"OPP_{i:05d}", decision=Decision.PROCEED, minutes=i)
            for i in range(size)
        ] + [_thread(None, minutes=i) for i in range(size // 10)]

        timings = []
        for _ in range(5):
            start = time.perf_counter()
            lifecycle = derive_opportunity_lifecycle(
                assurance, threads, as_of=AS_OF)
            timings.append((time.perf_counter() - start) * 1000)
        timings.sort()
        elapsed = timings[len(timings) // 2]
        per_item[label] = elapsed / size
        report.append({
            "label": label, "opportunities": size, "threads": len(threads),
            "derive_ms": round(elapsed, 3),
            "us_per_item": round(per_item[label] * 1000, 3),
        })
        # Non-vacuous: the join really did the work at this size.
        assert len(lifecycle.opportunities) == size
        assert lifecycle.summary.unlinked_thread_count == size // 10

    print("\nlifecycle join cost:")                                  # noqa: T201
    for row in report:
        print(f"  {row}")                                            # noqa: T201
    assert per_item["stress"] < per_item["small"] * 8, per_item
