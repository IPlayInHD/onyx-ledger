"""The Tax Assurance Map derivation — pure, over synthetic graphs.

Every test constructs exactly the graph it needs. What is at stake is the
closed vocabulary: that each status traces to the governed input that caused
it, that the documented precedence holds, and that absence of authority never
reads as readiness.
"""
import subprocess
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

from app.services.state_graph.assurance import (
    APPROACHING_WITHIN_DAYS,
    URGENT_WITHIN_DAYS,
    ActionStatus,
    AssuranceStatus,
    UrgencyStatus,
    canonical_assurance_text,
    derive_assurance_map,
)
from app.services.state_graph.contracts import (
    EdgeType,
    GraphAnchor,
    GraphEdge,
    GraphNode,
    GraphScope,
    GraphView,
    NodeFreshness,
    NodeType,
    Provenance,
    TaxStateGraph,
    summarize,
)
from app.services.state_graph.hashing import compute_graph_hash

AS_OF = date(2026, 3, 1)
TAX_YEAR = 2025


# ---------------------------------------------------------------- builders --
def _opportunity(
    code: str = "RRSP_TOPUP",
    *,
    source_id: str | None = None,
    eligibility: str = "eligible",
    readiness: str | None = "READY",
    exclusion_reason: str | None = None,
    requires_re_evaluation: bool = False,
    freshness: NodeFreshness = NodeFreshness.CURRENT,
    raw: str | None = "80.00",
    adjusted: str | None = "80.00",
    rank: int | None = 1,
) -> GraphNode:
    return GraphNode(
        node_type=NodeType.OPPORTUNITY,
        source_kind="ioe.optimization_candidate",
        source_id=source_id or f"cand-{code}",
        provenance=Provenance.ENGINE_COMPUTED,
        freshness=freshness,
        attributes={
            "opportunity_code": code,
            "eligibility_status": eligibility,
            "readiness": readiness,
            "exclusion_reason_code": exclusion_reason,
            "requires_re_evaluation": requires_re_evaluation,
            "raw_support_score": raw,
            "assumption_adjusted_score": adjusted,
            "display_support_score": adjusted,
            "support_cap_applied": False,
            "support_cap_reason_code": None,
            "standalone_potential": "1200.00",
            "incremental_portfolio_benefit": None,
            "candidate_rank": rank,
        },
    )


def _deadline(code: str, when: date, *, hard: bool = True) -> GraphNode:
    return GraphNode(
        node_type=NodeType.DEADLINE,
        source_kind="rules.rule_deadline",
        source_id=f"dl-{code}",
        provenance=Provenance.RULE_DATA,
        attributes={
            "deadline_code": code, "deadline_date": when.isoformat(),
            "is_hard": hard, "jurisdiction_code": "FED",
        },
    )


def _requirement(type_code: str, readiness: str) -> GraphNode:
    return GraphNode(
        node_type=NodeType.EVIDENCE,
        source_kind="rules.rule_required_document",
        source_id=f"ver:{type_code}",
        provenance=Provenance.DERIVED_DETERMINISTIC,
        attributes={
            "evidence_kind": "requirement", "document_type_code": type_code,
            "necessity": "required", "readiness": readiness,
        },
    )


_RUN_ANCHOR = GraphAnchor(artifact="ioe.optimization_run", artifact_id="run-1")
_ANALYSIS_ANCHOR = GraphAnchor(
    artifact="analysis.analysis_run", artifact_id="analysis-1")


#: Fixed so two builds of the same content are the same graph — the tests
#: compare canonical texts, and a fresh uuid per build would smuggle a
#: difference into every comparison.
_USER_ID = "3d1f8a52-0000-4000-8000-000000000012"


def _graph(
    nodes: list[GraphNode],
    edges: list[GraphEdge] = [],  # noqa: B006 - never mutated
    *,
    anchors: tuple[GraphAnchor, ...] = (_ANALYSIS_ANCHOR, _RUN_ANCHOR),
) -> TaxStateGraph:
    scope = GraphScope(
        user_id=_USER_ID, tax_year=TAX_YEAR, view=GraphView.CURRENT)
    ordered_nodes = tuple(sorted(nodes, key=lambda n: n.key))
    ordered_edges = tuple(sorted(edges, key=lambda e: e.sort_key))
    summary = summarize(ordered_nodes, ordered_edges)
    return TaxStateGraph(
        scope=scope, anchors=anchors,
        nodes=ordered_nodes,
        edges=ordered_edges,
        summary=summary,
        # Hashed over the SORTED content, as the real assembler hashes it —
        # otherwise input order would move the hash and every order-invariance
        # assertion downstream would be comparing different graphs.
        graph_hash=compute_graph_hash(
            scope=scope, anchors=anchors, nodes=ordered_nodes,
            edges=ordered_edges, summary=summary),
    )


def _expires(opportunity: GraphNode, deadline: GraphNode) -> GraphEdge:
    return GraphEdge(EdgeType.EXPIRES_AT, opportunity.key, deadline.key)


def _requires(opportunity: GraphNode, requirement: GraphNode) -> GraphEdge:
    return GraphEdge(EdgeType.REQUIRES, opportunity.key, requirement.key)


def _only(assurance):
    (item,) = assurance.opportunities
    return item


# ===========================================================================
# Status precedence — the documented order, condition by condition
# ===========================================================================
def test_a_governed_exclusion_outranks_every_other_condition():
    node = _opportunity(
        eligibility="indeterminate", readiness="MISSING",
        exclusion_reason="CONFLICTS_WITH_SELECTED", requires_re_evaluation=True,
        freshness=NodeFreshness.STALE,
    )
    item = _only(derive_assurance_map(_graph([node]), as_of=AS_OF))
    assert item.status is AssuranceStatus.BLOCKED
    assert item.action is ActionStatus.BLOCKED
    assert item.blocked_reason_code == "CONFLICTS_WITH_SELECTED"


def test_review_outranks_evidence():
    node = _opportunity(readiness="MISSING", requires_re_evaluation=True)
    item = _only(derive_assurance_map(_graph([node]), as_of=AS_OF))
    assert item.status is AssuranceStatus.REVIEW_REQUIRED
    assert item.action is ActionStatus.DECISION_REQUIRED
    assert "GOVERNED_RE_EVALUATION_REQUESTED" in item.review_reason_codes


def test_every_review_trigger_is_named():
    node = _opportunity(
        eligibility="indeterminate", requires_re_evaluation=True,
        freshness=NodeFreshness.STALE,
    )
    item = _only(derive_assurance_map(_graph([node]), as_of=AS_OF))
    assert set(item.review_reason_codes) == {
        "GOVERNED_RE_EVALUATION_REQUESTED",
        "INPUTS_CHANGED_SINCE_EVALUATION",
        "ELIGIBILITY_INDETERMINATE",
    }


def test_missing_evidence_makes_evidence_required():
    item = _only(derive_assurance_map(
        _graph([_opportunity(readiness="MISSING")]), as_of=AS_OF))
    assert item.status is AssuranceStatus.EVIDENCE_REQUIRED
    assert item.action is ActionStatus.EVIDENCE_REQUIRED


def test_unknown_readiness_is_a_gap_not_a_pass():
    """UNKNOWN is a conditional requirement nobody resolved. Treating it as
    ready would tell a user they hold what they may not hold."""
    item = _only(derive_assurance_map(
        _graph([_opportunity(readiness="UNKNOWN")]), as_of=AS_OF))
    assert item.status is AssuranceStatus.EVIDENCE_REQUIRED


def test_an_eligible_ready_item_is_action_available():
    item = _only(derive_assurance_map(
        _graph([_opportunity()]), as_of=AS_OF))
    assert item.status is AssuranceStatus.READY
    assert item.action is ActionStatus.ACTION_AVAILABLE


def test_conditional_eligibility_owes_a_decision_even_when_ready():
    item = _only(derive_assurance_map(
        _graph([_opportunity(eligibility="conditionally_eligible")]),
        as_of=AS_OF))
    assert item.status is AssuranceStatus.READY
    assert item.action is ActionStatus.DECISION_REQUIRED


def test_no_governed_requirement_reads_not_required_not_unknown():
    """`None` readiness = no requirement row exists. Nothing was demanded, so
    nothing is missing — distinct from UNKNOWN, which is a real requirement."""
    item = _only(derive_assurance_map(
        _graph([_opportunity(readiness=None)]), as_of=AS_OF))
    assert item.evidence_readiness == "NOT_REQUIRED"
    assert item.status is AssuranceStatus.READY


def test_a_portfolio_edge_blocks_without_an_exclusion_attribute():
    blocker = _opportunity("BLOCKER", source_id="cand-blocker")
    blocked = _opportunity("BLOCKED_ONE", source_id="cand-blocked")
    edge = GraphEdge(EdgeType.INELIGIBLE_BECAUSE, blocked.key, blocker.key)
    assurance = derive_assurance_map(
        _graph([blocker, blocked], [edge]), as_of=AS_OF)
    by_code = {i.opportunity_code: i for i in assurance.opportunities}
    assert by_code["BLOCKED_ONE"].status is AssuranceStatus.BLOCKED
    assert by_code["BLOCKED_ONE"].blocked_reason_code == "PORTFOLIO_EXCLUSION"
    assert by_code["BLOCKER"].status is AssuranceStatus.READY


# ===========================================================================
# Urgency — exact boundaries, evaluated against the injected as_of
# ===========================================================================
def test_urgency_boundaries_are_exact():
    cases = [
        (AS_OF - timedelta(days=1), UrgencyStatus.EXPIRED),
        (AS_OF, UrgencyStatus.URGENT),
        (date(2026, 3, 1 + URGENT_WITHIN_DAYS), UrgencyStatus.URGENT),
        (date(2026, 3, 1 + URGENT_WITHIN_DAYS + 1), UrgencyStatus.APPROACHING),
        (date(2026, 4, 30), UrgencyStatus.APPROACHING),  # day 60
        (date(2026, 5, 1), UrgencyStatus.NORMAL),        # day 61
    ]
    assert (date(2026, 4, 30) - AS_OF).days == APPROACHING_WITHIN_DAYS
    for deadline_date, expected in cases:
        node = _opportunity()
        deadline = _deadline("DL", deadline_date)
        item = _only(derive_assurance_map(
            _graph([node, deadline], [_expires(node, deadline)]), as_of=AS_OF))
        assert item.urgency is expected, (deadline_date, expected)
        assert item.deadline.days_remaining == (deadline_date - AS_OF).days


def test_no_deadline_is_its_own_state_never_fabricated():
    item = _only(derive_assurance_map(_graph([_opportunity()]), as_of=AS_OF))
    assert item.urgency is UrgencyStatus.NO_DEADLINE
    assert item.deadline is None
    assert item.deadline_count == 0


def test_the_earliest_deadline_binds_and_all_are_counted():
    node = _opportunity()
    late = _deadline("LATE", date(2026, 6, 30))
    early = _deadline("EARLY", date(2026, 3, 10))
    item = _only(derive_assurance_map(
        _graph([node, late, early],
               [_expires(node, late), _expires(node, early)]),
        as_of=AS_OF))
    assert item.deadline.deadline_code == "EARLY"
    assert item.deadline_count == 2
    assert item.urgency is UrgencyStatus.URGENT


def test_as_of_moves_urgency_and_nothing_else():
    node = _opportunity()
    deadline = _deadline("DL", date(2026, 3, 10))
    graph = _graph([node, deadline], [_expires(node, deadline)])

    near = derive_assurance_map(graph, as_of=date(2026, 3, 1))
    far = derive_assurance_map(graph, as_of=date(2025, 11, 1))

    assert _only(near).urgency is UrgencyStatus.URGENT
    assert _only(far).urgency is UrgencyStatus.NORMAL
    assert _only(near).status == _only(far).status
    assert _only(near).support == _only(far).support
    assert near.graph_hash == far.graph_hash


# ===========================================================================
# Families — absence of authority never reads as readiness
# ===========================================================================
def test_no_optimization_run_is_unavailable_not_an_empty_ready():
    assurance = derive_assurance_map(
        _graph([], anchors=(_ANALYSIS_ANCHOR,)), as_of=AS_OF)
    by_family = {f.family: f for f in assurance.families}

    for family in ("OPPORTUNITY", "DEADLINE", "EVIDENCE", "RESOURCE",
                   "ASSUMPTION"):
        assert by_family[family].status is AssuranceStatus.UNAVAILABLE, family
        assert by_family[family].reason_code == (
            "NO_OPTIMIZATION_RUN_FOR_TAX_YEAR")
    assert by_family["TAX_STATE"].status is AssuranceStatus.READY
    assert by_family["FACT"].status is AssuranceStatus.READY


def test_a_run_with_zero_candidates_is_authoritatively_empty_and_ready():
    """THE distinction this model exists to keep: zero items under a present
    authority is READY-empty; zero items under no authority is UNAVAILABLE."""
    assurance = derive_assurance_map(_graph([]), as_of=AS_OF)
    opportunity = next(
        f for f in assurance.families if f.family == "OPPORTUNITY")
    assert opportunity.status is AssuranceStatus.READY
    assert opportunity.item_count == 0
    assert opportunity.reason_code == "OPTIMIZATION_RUN_AUTHORITATIVE"


def test_no_analysis_run_marks_tax_state_unavailable():
    assurance = derive_assurance_map(
        _graph([], anchors=(_RUN_ANCHOR,)), as_of=AS_OF)
    tax_state = next(
        f for f in assurance.families if f.family == "TAX_STATE")
    assert tax_state.status is AssuranceStatus.UNAVAILABLE
    assert tax_state.reason_code == "NO_ANALYSIS_RUN_FOR_TAX_YEAR"


def test_assumptions_fail_closed_when_nothing_was_declared():
    """A run with zero assumption nodes is either a pre-authority seal or a
    genuinely empty declaration, and the graph does not record which. Claiming
    READY-empty would assert an authoritative emptiness nobody recorded."""
    assurance = derive_assurance_map(_graph([]), as_of=AS_OF)
    assumption = next(
        f for f in assurance.families if f.family == "ASSUMPTION")
    assert assumption.status is AssuranceStatus.UNAVAILABLE
    assert assumption.reason_code == "ASSUMPTION_DECLARATION_ABSENT"


def test_declared_assumptions_make_the_family_ready_and_are_listed():
    node = GraphNode(
        node_type=NodeType.ASSUMPTION,
        source_kind="ioe.optimization_run.assumption_set",
        source_id="run-1:INFLATION",
        provenance=Provenance.ASSUMPTION_DECLARED,
        attributes={"assumption_code": "INFLATION"},
    )
    assurance = derive_assurance_map(_graph([node]), as_of=AS_OF)
    assumption = next(
        f for f in assurance.families if f.family == "ASSUMPTION")
    assert assumption.status is AssuranceStatus.READY
    assert assurance.assumption_codes == ("INFLATION",)


def test_reserved_families_are_not_applicable_never_unavailable():
    assurance = derive_assurance_map(_graph([]), as_of=AS_OF)
    by_family = {f.family: f for f in assurance.families}
    for reserved in ("OBLIGATION", "DECISION"):
        assert by_family[reserved].status is AssuranceStatus.NOT_APPLICABLE
        assert by_family[reserved].reason_code == "RESERVED_NO_PRODUCER"
        assert by_family[reserved].item_count == 0


# ===========================================================================
# Assumption dependence — read from the support model, never re-derived
# ===========================================================================
def test_a_governed_support_adjustment_marks_assumption_dependence():
    adjusted = _only(derive_assurance_map(
        _graph([_opportunity(raw="80.00", adjusted="65.00")]), as_of=AS_OF))
    grounded = _only(derive_assurance_map(
        _graph([_opportunity(raw="80.00", adjusted="80.00")]), as_of=AS_OF))
    absent = _only(derive_assurance_map(
        _graph([_opportunity(raw=None, adjusted=None)]), as_of=AS_OF))

    assert adjusted.assumption_dependent is True
    assert grounded.assumption_dependent is False
    assert absent.assumption_dependent is False


# ===========================================================================
# The attention queue — the documented key, nothing else
# ===========================================================================
def test_attention_orders_by_urgency_then_gap_then_the_optimizers_own_rank():
    urgent_evidence = _opportunity(
        "URGENT_EVIDENCE", source_id="a", readiness="MISSING", rank=9)
    urgent_ready = _opportunity("URGENT_READY", source_id="b", rank=9)
    normal_first = _opportunity("NORMAL_FIRST", source_id="c", rank=1)
    normal_second = _opportunity("NORMAL_SECOND", source_id="d", rank=2)
    expired = _opportunity("EXPIRED_ONE", source_id="e", rank=1)
    blocked = _opportunity(
        "BLOCKED_ONE", source_id="f", exclusion_reason="CONFLICT", rank=1)

    near = _deadline("NEAR", date(2026, 3, 5))
    far = _deadline("FAR", date(2026, 9, 30))
    past = _deadline("PAST", date(2026, 1, 31))

    assurance = derive_assurance_map(_graph(
        [urgent_evidence, urgent_ready, normal_first, normal_second,
         expired, blocked, near, far, past],
        [_expires(urgent_evidence, near), _expires(urgent_ready, near),
         _expires(normal_first, far), _expires(normal_second, far),
         _expires(expired, past)],
    ), as_of=AS_OF)

    by_id = {i.source_id: i.opportunity_code for i in assurance.opportunities}
    ordered = [by_id[source_id] for source_id in assurance.attention]
    assert ordered == [
        "URGENT_EVIDENCE",   # urgent band, closable evidence gap first
        "URGENT_READY",      # urgent band, ready
        "NORMAL_FIRST",      # normal band, optimizer rank 1
        "NORMAL_SECOND",     # normal band, optimizer rank 2
        "BLOCKED_ONE",       # no-deadline band, blocked action ranks last
        "EXPIRED_ONE",       # expired: the window closed, effort goes elsewhere
    ]


# ===========================================================================
# Evidence — product-semantic identity only
# ===========================================================================
def test_requirements_are_exposed_by_document_type_never_by_document():
    node = _opportunity(readiness="PARTIAL")
    t4 = _requirement("T4", "READY")
    receipt = _requirement("RRSP_RECEIPT", "MISSING")
    held = GraphNode(
        node_type=NodeType.EVIDENCE, source_kind="docs.document",
        source_id=str(uuid.uuid4()), provenance=Provenance.DOCUMENT_EXTRACTED,
        attributes={"evidence_kind": "held_document",
                    "document_type_code": "T4"},
    )
    assurance = derive_assurance_map(_graph(
        [node, t4, receipt, held],
        [_requires(node, t4), _requires(node, receipt)],
    ), as_of=AS_OF)

    item = _only(assurance)
    assert [r.document_type_code for r in item.evidence_requirements] == [
        "RRSP_RECEIPT", "T4"]
    text = canonical_assurance_text(assurance)
    assert held.source_id not in text, "a document id reached the product payload"
    assert "docs.document" not in text


# ===========================================================================
# Determinism
# ===========================================================================
def test_node_order_does_not_move_the_map():
    nodes = [
        _opportunity("B_CODE", source_id="b", rank=2),
        _opportunity("A_CODE", source_id="a", rank=1),
    ]
    forward = derive_assurance_map(_graph(list(nodes)), as_of=AS_OF)
    reversed_ = derive_assurance_map(_graph(list(reversed(nodes))), as_of=AS_OF)
    assert canonical_assurance_text(forward) == canonical_assurance_text(reversed_)


def test_repeated_derivation_is_identical():
    node = _opportunity()
    deadline = _deadline("DL", date(2026, 4, 1))
    graph = _graph([node, deadline], [_expires(node, deadline)])
    first = derive_assurance_map(graph, as_of=AS_OF)
    second = derive_assurance_map(graph, as_of=AS_OF)
    assert first == second
    assert canonical_assurance_text(first) == canonical_assurance_text(second)


def test_the_map_is_invariant_under_pythonhashseed(tmp_path: Path):
    script = tmp_path / "derive.py"
    script.write_text(
        "import sys\n"
        "from datetime import date\n"
        "sys.path.insert(0, '.')\n"
        f"sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
        "from test_assurance import _deadline, _expires, _graph, _opportunity\n"
        "from app.services.state_graph.assurance import (\n"
        "    canonical_assurance_text, derive_assurance_map)\n"
        "a = _opportunity('A_CODE', source_id='a', readiness='MISSING')\n"
        "b = _opportunity('B_CODE', source_id='b')\n"
        "dl = _deadline('DL', date(2026, 3, 10))\n"
        "graph = _graph([a, b, dl], [_expires(a, dl)])\n"
        "print(canonical_assurance_text(\n"
        "    derive_assurance_map(graph, as_of=date(2026, 3, 1))))\n"
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


# ===========================================================================
# Summary consistency and product safety
# ===========================================================================
def test_the_summary_counts_the_items_it_travels_with():
    nodes = [
        _opportunity("A_CODE", source_id="a", readiness="MISSING"),
        _opportunity("B_CODE", source_id="b"),
        _opportunity("C_CODE", source_id="c",
                     exclusion_reason="CONFLICT", raw="70.00", adjusted="55.00"),
    ]
    assurance = derive_assurance_map(_graph(nodes), as_of=AS_OF)
    summary = assurance.summary

    assert summary.opportunity_count == 3
    assert summary.opportunities_by_status["EVIDENCE_REQUIRED"] == 1
    assert summary.opportunities_by_status["READY"] == 1
    assert summary.opportunities_by_status["BLOCKED"] == 1
    assert summary.opportunities_by_action["ACTION_AVAILABLE"] == 1
    assert summary.assumption_dependent_count == 1
    assert summary.upcoming_deadline_count == 0
    assert sum(summary.opportunities_by_status.values()) == 3


def test_derivation_cost_is_measured_and_linear():
    """Keyed maps by identity, never pairwise: cost per item must not grow
    with the graph. The ceiling is a shape check, not a budget."""
    import time

    per_item = {}
    report = []
    for label, size in (("small", 10), ("moderate", 200), ("stress", 2000)):
        nodes = []
        edges = []
        for i in range(size):
            node = _opportunity(
                f"OPP_{i:05d}", source_id=f"cand-{i:05d}",
                readiness="MISSING" if i % 3 == 0 else "READY",
                rank=i)
            deadline = _deadline(
                f"DL_{i:05d}", AS_OF + timedelta(days=1 + i % 100))
            nodes.extend((node, deadline))
            edges.append(_expires(node, deadline))
        graph = _graph(nodes, edges)

        timings = []
        for _ in range(5):
            start = time.perf_counter()
            assurance = derive_assurance_map(graph, as_of=AS_OF)
            text = canonical_assurance_text(assurance)
            timings.append((time.perf_counter() - start) * 1000)
        timings.sort()
        elapsed = timings[len(timings) // 2]
        per_item[label] = elapsed / size
        report.append({
            "label": label, "items": size,
            "derive_and_serialize_ms": round(elapsed, 3),
            "us_per_item": round(per_item[label] * 1000, 3),
            "payload_bytes": len(text.encode()),
        })

    print("\nassurance derivation cost:")                           # noqa: T201
    for row in report:
        print(f"  {row}")                                           # noqa: T201
    assert per_item["stress"] < per_item["small"] * 8, per_item


def test_the_map_makes_no_strategy_or_probability_claim():
    node = _opportunity(raw="80.00", adjusted="65.00")
    deadline = _deadline("DL", date(2026, 3, 10))
    text = canonical_assurance_text(derive_assurance_map(
        _graph([node, deadline], [_expires(node, deadline)]), as_of=AS_OF,
    )).lower()
    for claim in ('"savings"', '"best', '"recommended', '"recommendation"',
                  '"optimal', '"probability', '"guaranteed', '"audit_risk'):
        assert claim not in text, f"the map asserts {claim}"
