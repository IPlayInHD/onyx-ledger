"""Every producer and every live edge type, exercised deterministically.

WHY THIS IS A UNIT TEST. The integration suite proves the wiring against a real
database, but it can only assert on whatever the optimizer happened to produce
— and a measured run on a fresh database yielded a DEGENERATE portfolio (every
candidate excluded, zero ledger entries, zero members), which leaves RESOURCE,
`CONSUMES_RESOURCE`, `CONSTRAINED_BY`, `INELIGIBLE_BECAUSE` and
`CONFLICTS_WITH` untouched. Waiting for the rule landscape to cooperate is not
coverage.

The assembler is pure and takes a `GraphSources` value, so the producers can be
driven directly with the exact shapes the loader returns. That is the whole
reason loading and assembly were separated.
"""
import uuid
from datetime import date
from decimal import Decimal

from app.database.models.analysis import AnalysisInputSnapshot, AnalysisLineItem, AnalysisRun
from app.database.models.finance import IncomeSource
from app.database.models.ioe import (
    OptimizationCandidate,
    OptimizationRun,
    PortfolioExclusion,
    PortfolioMember,
    RecommendationRelationship,
    ResourceLedgerEntry,
    Scenario,
    ScenarioAssumption,
    StrategyPortfolio,
)
from app.database.models.tax_kb import RuleDeadline, RuleRequiredDocument
from app.services.state_graph.assembler import assemble_graph
from app.services.state_graph.contracts import (
    LIVE_NODE_TYPES,
    EdgeType,
    EvidenceReadiness,
    NodeFreshness,
    NodeType,
    Provenance,
)
from app.services.state_graph.loader import GraphSources

USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
ANALYSIS = uuid.UUID("22222222-2222-2222-2222-222222222222")
RUN = uuid.UUID("33333333-3333-3333-3333-333333333333")
PORTFOLIO = uuid.UUID("44444444-4444-4444-4444-444444444444")
VERSION = uuid.UUID("55555555-5555-5555-5555-555555555555")
CAND_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
CAND_B = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000002")
DOC = uuid.UUID("dddddddd-0000-0000-0000-000000000001")
SCENARIO = uuid.UUID("55555555-0000-0000-0000-000000000009")


def _candidate(candidate_id: uuid.UUID, code: str) -> OptimizationCandidate:
    return OptimizationCandidate(
        id=candidate_id, run_id=RUN, opportunity_code=code,
        tax_rule_version_id=VERSION,
        eligibility_status="eligible", calculation_basis="engine_determined",
        evidence_status="documented_verified",
        standalone_potential=Decimal("1000.00"),
        incremental_portfolio_benefit=Decimal("900.00"),
        portfolio_membership="included", exclusion_reason_code=None,
        candidate_rank=1, recommendation_score=Decimal("80.00"),
        raw_support_score=Decimal("70.00"),
        assumption_adjusted_score=Decimal("65.00"),
        display_support_score=Decimal("60.00"),
        support_cap_applied=False, support_cap_reason_code=None,
        requires_re_evaluation=False, re_evaluation_reason_code=None,
    )


def _sources() -> GraphSources:
    """One of everything, so all eight producers and all live edges fire."""
    return GraphSources(
        analysis=AnalysisRun(
            id=ANALYSIS, user_id=USER, tax_year=2025, province_code="ON",
            engine_version="py-1.0.0", status="completed",
            total_income=Decimal("95000.00"), taxable_income=Decimal("90000.00"),
            estimated_tax=Decimal("20000.00"), estimated_savings=Decimal("0.00"),
            marginal_rate=Decimal("0.295000"), average_rate=Decimal("0.222000"),
            confidence_score=80, data_verified=True,
        ),
        line_items=(
            AnalysisLineItem(
                id=uuid.UUID("66666666-0000-0000-0000-000000000001"),
                analysis_id=ANALYSIS, kind="deduction", label="RRSP",
                amount=Decimal("5000.00"), fact_key="rrsp_contribution",
                tax_rule_version_id=VERSION, sort_order=1,
            ),
        ),
        snapshot=AnalysisInputSnapshot(
            analysis_id=ANALYSIS, snapshot={"a": 1}, snapshot_hash="snap-hash",
        ),
        income=(
            IncomeSource(
                id=uuid.UUID("77777777-0000-0000-0000-000000000001"),
                user_id=USER, tax_year=2025,
                income_type_id=uuid.UUID("0e000000-0000-0000-0000-000000000001"), amount=Decimal("95000.00"),
                currency_code="CAD", frequency="annual", province_code="ON",
                verification_status="verified", document_id=DOC,
            ),
        ),
        run=OptimizationRun(
            id=RUN, user_id=USER, analysis_id=ANALYSIS, tax_year=2025,
            workflow_status="completed", freshness_status="stale",
            stale_reason_codes=["RULE_SNAPSHOT_SUPERSEDED", "ENGINE_VERSION_CHANGED"],
            optimization_result_hash="result-hash",
            integrity_status="verified", integrity_reason_code="NONE",
            assumption_set=[{
                "code": "CONTRIBUTION_ROOM_AVAILABLE", "source": "user",
                "certainty": "user_asserted", "materiality": "high",
                "affects_eligibility": True,
            }],
        ),
        candidates=(_candidate(CAND_A, "OPP_A"), _candidate(CAND_B, "OPP_B")),
        portfolio=StrategyPortfolio(
            id=PORTFOLIO, run_id=RUN, assembly_policy_version="1",
            assembly_method="greedy", objective_metric="tax",
            objective_version="1", baseline_tax=Decimal("20000.00"),
            portfolio_tax=Decimal("19000.00"),
            portfolio_total_benefit=Decimal("1000.00"),
        ),
        members=(
            PortfolioMember(
                id=uuid.UUID("0e000000-0000-0000-0000-000000000002"), portfolio_id=PORTFOLIO, candidate_id=CAND_A,
                apply_order=1, incremental_benefit=Decimal("900.00"),
                resource_allocations={"RRSP_ROOM": "5000.00"},
            ),
        ),
        exclusions=(
            PortfolioExclusion(
                id=uuid.UUID("0e000000-0000-0000-0000-000000000003"), portfolio_id=PORTFOLIO, candidate_id=CAND_B,
                membership="excluded_resource_exhausted",
                reason_code="SHARED_RESOURCE_EXHAUSTED",
                blocking_candidate_id=CAND_A,
                shared_resource_code="RRSP_ROOM",
            ),
        ),
        ledger=(
            ResourceLedgerEntry(
                id=uuid.UUID("0e000000-0000-0000-0000-000000000004"), portfolio_id=PORTFOLIO, resource_code="RRSP_ROOM",
                pool_scope="individual", capacity=Decimal("5000.00"),
                allocated=Decimal("5000.00"), remaining=Decimal("0.00"),
            ),
        ),
        relationships=(
            RecommendationRelationship(
                id=uuid.UUID("0e000000-0000-0000-0000-000000000005"), run_id=RUN,
                source_candidate_id=CAND_A, target_candidate_id=CAND_B,
                relationship_type="mutually_exclusive",
                explanation_code="SHARED_POOL",
                derivation_source="rules_contract",
                shared_resource_code="RRSP_ROOM",
                measured_delta=Decimal("100.00"),
            ),
        ),
        scenarios=(
            Scenario(
                id=SCENARIO, user_id=USER, base_analysis_id=ANALYSIS,
                label="a name that must not enter the hash",
                note="nor must this",
                workflow_status="completed", visibility_status="active",
                freshness_status="current", tax_year=2025, jurisdiction="ON",
                objective_code="minimise_tax", objective_version="1",
                baseline_tax=Decimal("20000.00"),
                scenario_result_hash="scenario-hash",
                integrity_status="not_checked", integrity_reason_code="NONE",
            ),
        ),
        scenario_assumptions=(
            ScenarioAssumption(
                id=uuid.UUID("88888888-0000-0000-0000-000000000001"),
                scenario_id=SCENARIO, assumption_code="CONTRIBUTION_ROOM_AVAILABLE",
                value_number=Decimal("5000.000000"), materiality="high",
                source="user", certainty="user_asserted", affects_eligibility=True,
            ),
        ),
        required_documents=(
            RuleRequiredDocument(
                id=uuid.UUID("0e000000-0000-0000-0000-000000000006"), rule_version_id=VERSION,
                document_type_code="T4", necessity="required",
            ),
        ),
        deadlines=(
            RuleDeadline(
                id=uuid.UUID("99999999-0000-0000-0000-000000000001"),
                rule_version_id=VERSION, deadline_code="RRSP_CONTRIBUTION",
                deadline_date=date(2026, 3, 2), is_hard=True,
                jurisdiction_code="FED",
            ),
        ),
        held_documents=((DOC, "T4"),),
    )


def _graph():
    return assemble_graph(_sources(), user_id=USER, tax_year=2025)


def test_all_eight_live_producers_emit():
    """The producer matrix promises eight. This is the test that would have
    caught a producer silently emitting nothing."""
    graph = _graph()
    emitted = {n.node_type for n in graph.nodes}
    missing = sorted(t.value for t in LIVE_NODE_TYPES - emitted)
    assert missing == [], f"live producers that emitted nothing: {missing}"


def test_no_reserved_node_is_emitted_from_a_fully_populated_source():
    """The 12A invariant at its strongest: every producer had data to work
    with, and OBLIGATION and DECISION are still zero."""
    graph = _graph()
    assert graph.nodes_of(NodeType.OBLIGATION) == ()
    assert graph.nodes_of(NodeType.DECISION) == ()


def test_every_live_edge_type_is_emitted():
    graph = _graph()
    emitted = {e.edge_type for e in graph.edges}
    expected = {
        EdgeType.DERIVED_FROM, EdgeType.REQUIRES, EdgeType.SUPPORTED_BY,
        EdgeType.INELIGIBLE_BECAUSE, EdgeType.CONSTRAINED_BY,
        EdgeType.CONSUMES_RESOURCE, EdgeType.CONFLICTS_WITH,
        EdgeType.EXPIRES_AT, EdgeType.ASSUMES, EdgeType.REFERENCES_SCENARIO,
    }
    assert expected - emitted == set()
    assert EdgeType.REFERENCES_RULE not in emitted


def test_ineligible_because_and_constrained_by_answer_different_questions():
    """One exclusion produces both: the candidate that won, and the resource
    that ran out. Collapsing them would lose which answer applies."""
    graph = _graph()
    blocked = [e for e in graph.edges if e.edge_type is EdgeType.INELIGIBLE_BECAUSE]
    constrained = [e for e in graph.edges if e.edge_type is EdgeType.CONSTRAINED_BY]
    assert len(blocked) == 1 and len(constrained) == 1
    assert blocked[0].target_key.startswith(NodeType.OPPORTUNITY.value)
    assert constrained[0].target_key.startswith(NodeType.RESOURCE.value)


def test_conflicts_with_carries_its_derivation_source():
    """`rules_contract` edges are authoritative; the rest are derived and may
    already have been trimmed by the sparse-derivation budget upstream. A reader
    that cannot tell them apart cannot tell an absent edge from a budgeted one.
    """
    graph = _graph()
    conflicts = [e for e in graph.edges if e.edge_type is EdgeType.CONFLICTS_WITH]
    assert conflicts and all(
        e.attributes["derivation_source"] == "rules_contract" for e in conflicts
    )


def test_conflicts_with_is_never_expanded_to_all_pairs():
    """Only rows that exist are emitted. Two candidates admit two ordered pairs;
    one relationship row must produce exactly one edge."""
    graph = _graph()
    assert sum(1 for e in graph.edges if e.edge_type is EdgeType.CONFLICTS_WITH) == 1


def test_the_run_freshness_and_its_reasons_reach_the_candidates():
    graph = _graph()
    for node in graph.nodes_of(NodeType.OPPORTUNITY):
        assert node.freshness is NodeFreshness.STALE
        assert node.stale_reason_codes == (
            "ENGINE_VERSION_CHANGED", "RULE_SNAPSHOT_SUPERSEDED",
        )


def test_a_stale_run_can_still_be_verified():
    """Freshness and integrity are different axes and are never collapsed: the
    inputs moved, and the answer we gave is still reproducible."""
    graph = _graph()
    node = graph.nodes_of(NodeType.OPPORTUNITY)[0]
    assert node.freshness is NodeFreshness.STALE
    assert node.integrity.value == "verified"


def test_a_scenario_that_was_never_checked_says_not_checked():
    graph = _graph()
    assert graph.nodes_of(NodeType.SCENARIO)[0].integrity.value == "not_checked"


def test_tables_with_no_freshness_column_report_not_tracked():
    """Not `current`. Claiming currency for something nothing measures would be
    the graph inventing a fact about itself."""
    graph = _graph()
    for node_type in (NodeType.FACT, NodeType.TAX_STATE, NodeType.DEADLINE):
        for node in graph.nodes_of(node_type):
            assert node.freshness is NodeFreshness.NOT_TRACKED


def test_a_scenario_label_and_note_do_not_enter_the_hash():
    """The precedent is on `ioe.scenario` itself, where both carry column
    comments saying renaming must not change identity. A graph hash that moved
    when a scenario was renamed would report a change that did not happen."""
    baseline = _graph().graph_hash
    renamed = _sources()
    renamed.scenarios[0].label = "a completely different name"
    renamed.scenarios[0].note = "and a different note"
    assert assemble_graph(renamed, user_id=USER, tax_year=2025).graph_hash == baseline


def test_the_portfolio_total_is_carried_not_computed():
    """The candidates sum to 2000.00 standalone; the portfolio's own total is
    1000.00. The graph must report the portfolio's number."""
    graph = _graph()
    assert graph.summary.portfolio_total_benefit == "1000.00"


def test_both_evidence_axes_are_present_and_independent():
    graph = _graph()
    opportunity = graph.nodes_of(NodeType.OPPORTUNITY)[0]
    assert opportunity.attributes["evidence_status"] == "documented_verified"
    assert opportunity.attributes["readiness"] == EvidenceReadiness.READY.value


def test_a_held_document_contributes_no_storage_detail():
    graph = _graph()
    documents = [
        n for n in graph.nodes_of(NodeType.EVIDENCE)
        if n.attributes.get("evidence_kind") == "held_document"
    ]
    assert documents
    assert set(documents[0].attributes) == {"evidence_kind", "document_type_code"}


def test_rule_identity_travels_as_an_attribute_since_there_is_no_rule_node():
    graph = _graph()
    opportunity = graph.nodes_of(NodeType.OPPORTUNITY)[0]
    assert opportunity.attributes["tax_rule_version_id"] == VERSION


def test_anchors_name_the_frozen_and_sealed_artifacts():
    """These are why no snapshot table is needed: a historical graph is a
    deterministic function of artifacts that cannot change."""
    graph = _graph()
    anchors = {a.artifact: a.content_hash for a in graph.anchors}
    assert anchors["analysis.analysis_run"] == "snap-hash"
    assert anchors["ioe.optimization_run"] == "result-hash"
    assert anchors["ioe.scenario"] == "scenario-hash"


def test_assembly_is_deterministic_over_the_same_sources():
    assert _graph().graph_hash == _graph().graph_hash


def test_a_run_sealed_before_assumption_sets_were_stored_emits_no_assumptions():
    """NULL means unavailable, not empty. Inventing an empty set would report
    that the run made no assumptions, which is a different claim."""
    sources = _sources()
    sources.run.assumption_set = None
    graph = assemble_graph(sources, user_id=USER, tax_year=2025)
    run_assumptions = [
        n for n in graph.nodes_of(NodeType.ASSUMPTION)
        if n.source_kind == "ioe.optimization_run.assumption_set"
    ]
    assert run_assumptions == []


def test_an_assumption_entry_with_no_code_is_skipped_not_positionally_keyed():
    """Keying by list position would change a node's identity whenever the
    sealed order changed."""
    sources = _sources()
    sources.run.assumption_set = [{"value": 1}, {"code": "REAL_CODE"}]
    graph = assemble_graph(sources, user_id=USER, tax_year=2025)
    codes = {
        n.attributes["assumption_code"] for n in graph.nodes_of(NodeType.ASSUMPTION)
        if n.source_kind == "ioe.optimization_run.assumption_set"
    }
    assert codes == {"REAL_CODE"}


def test_an_empty_source_set_assembles_an_empty_graph_rather_than_failing():
    graph = assemble_graph(GraphSources(), user_id=USER, tax_year=2025)
    assert graph.nodes == () and graph.edges == ()
    assert graph.graph_hash
    assert graph.summary.portfolio_total_benefit is None


def test_provenance_follows_verification_rather_than_the_presence_of_a_document():
    """A row can name a document and still be unverified; claiming
    DOCUMENT_EXTRACTED for it would overstate where the number came from."""
    graph = _graph()
    income = [n for n in graph.nodes_of(NodeType.FACT)
              if n.source_kind == "finance.income_source"][0]
    assert income.provenance is Provenance.DOCUMENT_EXTRACTED

    sources = _sources()
    sources.income[0].verification_status = "unverified"
    graph = assemble_graph(sources, user_id=USER, tax_year=2025)
    income = [n for n in graph.nodes_of(NodeType.FACT)
              if n.source_kind == "finance.income_source"][0]
    assert income.provenance is Provenance.USER_DECLARED
    assert income.attributes["has_supporting_document"] is True
