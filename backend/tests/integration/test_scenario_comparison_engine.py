"""Entry 12B — the comparison engine over genuinely sealed state.

The unit suite proves every change kind in isolation. This proves the COMPLETE
customer path: seal through the ordinary production v3 entry point, load the
sealed sides, assert both are authoritative, assemble, project, compare — with
every recomputation route wrapped and counted at zero.

WHAT THE COUNTERS ARE FOR. A comparison that ran the tax engine would answer a
question about March with June's rules; one that ran the rules evaluator would
decide eligibility twice. Both are absent, and both are counted rather than
asserted, because an absent SQL string is weaker evidence than a counter that
stayed at zero while the whole path ran.

EVERY FIXTURE ESTABLISHES ITS OWN RULE STATE. Candidates, deadlines and
evidence requirements all come from the pinned rules evaluation; a test that
asserts on them without publishing its own rule is asserting on residue.
"""
import time
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisLineItem,
    AnalysisRun,
    Document,
    DocumentType,
    IncomeSource,
    IncomeType,
    Jurisdiction,
    RuleDeadline,
    RuleOutcome,
    RuleRequiredDocument,
    TaxProfile,
    TaxRule,
    TaxRuleVersion,
    UserAccount,
)
from app.database.session import unit_of_work
from app.services.ioe.domain.integrity import DependencyUnavailable, IntegrityReason
from app.services.ioe.domain.scenario import (
    SCENARIO_RESULT_SCHEMA_V2,
    ScenarioSpec,
)
from app.services.ioe.frozen.models import reconstruct_tax_input
from app.services.ioe.scenario import historical_source as hs
from app.services.ioe.scenario.comparison import (
    ChangeKind,
    ComparisonSide,
    canonical_comparison_text,
    compare_scenario_graphs,
    invert,
)
from app.services.ioe.scenario.historical_graph import assemble_historical_graph
from app.services.ioe.scenario.service import ScenarioService
from app.services.state_graph.contracts import NodeType
from app.services.state_graph.scenario_projection import (
    project_scenario_comparable_graph,
)
from tests.conftest import frozen_snapshot

TAX_YEAR = 2025
RRSP = "INCREASE_RRSP_DEDUCTION"


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


# ---------------------------------------------------------------------- fixtures
async def _user_with_baseline_analysis() -> tuple[uuid.UUID, uuid.UUID]:
    from app.services.tax_engine.core.engine import compute

    async with unit_of_work(actor_type="system") as s:
        account = UserAccount(
            email=f"cmp_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(account)
        await s.flush()
        uid = account.id
        income_type_id = (await s.scalar(
            select(IncomeType).where(IncomeType.code == "employment"))).id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(TaxProfile(user_id=uid, province_code="ON", marital_status="single"))
        s.add(IncomeSource(
            user_id=uid, tax_year=TAX_YEAR, income_type_id=income_type_id,
            amount=Decimal("95000"), province_code="ON"))
        run = AnalysisRun(
            user_id=uid, tax_year=TAX_YEAR, province_code="ON",
            engine_version="py-1.0.0", status="completed",
            started_at=datetime.now(tz=UTC), completed_at=datetime.now(tz=UTC),
            data_verified=True)
        s.add(run)
        await s.flush()
        payload, digest = frozen_snapshot(employment_income=Decimal("95000"))
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot=payload, snapshot_hash=digest))
        result = compute(reconstruct_tax_input(payload))
        for i, item in enumerate(result.line_items):
            s.add(AnalysisLineItem(
                analysis_id=run.id, kind=item["kind"], label=item["label"],
                amount=item["amount"], sort_order=i))
        await s.flush()
        return uid, run.id


async def _publish_rule(description: str = "12B comparison fixture") -> uuid.UUID:
    """This test's OWN governed rule. Never inherited from a neighbour."""
    async with unit_of_work(actor_type="admin") as s:
        jurisdiction = await s.scalar(
            select(Jurisdiction).where(Jurisdiction.code == "FED"))
        code = f"CMP_{uuid.uuid4().hex[:8].upper()}"
        rule = TaxRule(code=code, name=code, category="deduction",
                       jurisdiction_id=jurisdiction.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=TAX_YEAR,
            effective_date=date(TAX_YEAR, 1, 1), status="published",
            description=description, eligibility_basis_codes=["BASIS_CMP"])
        s.add(version)
        await s.flush()
        s.add(RuleOutcome(rule_version_id=version.id, outcome_type="recommend",
                          priority=1, title_template=f"{description} opportunity"))
        s.add(RuleRequiredDocument(rule_version_id=version.id,
                                   document_type_code="T4", necessity="required"))
        s.add(RuleDeadline(rule_version_id=version.id, deadline_code=f"DL_{code}",
                           deadline_date=date(TAX_YEAR + 1, 4, 30), is_hard=True,
                           jurisdiction_code="FED"))
        await s.flush()
        return version.id


async def _hold(uid: uuid.UUID, code: str = "T4") -> uuid.UUID:
    async with unit_of_work(actor_type="system") as s:
        type_id = (await s.scalar(
            select(DocumentType).where(DocumentType.code == code))).id
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        document = Document(
            user_id=uid, document_type_id=type_id, tax_year=TAX_YEAR,
            bucket=f"onyx-bucket-{uuid.uuid4().hex}",
            object_key=f"{uid}/v3/{uuid.uuid4()}",
            content_hash=uuid.uuid4().hex, status="processed")
        s.add(document)
        await s.flush()
        return document.id


async def _seal_public(uid: uuid.UUID, analysis_id: uuid.UUID, amount="5000"):
    """THE ORDINARY PRODUCTION PATH — `simulate`, not the internal seam."""
    return await ScenarioService(uid).simulate(
        analysis_id,
        ScenarioSpec.parse(
            [{"lever_code": RRSP, "parameters": {"amount": Decimal(amount)}}]))


async def _seal_legacy_v2(uid: uuid.UUID, analysis_id: uuid.UUID):
    return await ScenarioService(uid)._simulate(
        analysis_id,
        ScenarioSpec.parse(
            [{"lever_code": RRSP, "parameters": {"amount": Decimal("5000")}}]),
        result_schema_version=SCENARIO_RESULT_SCHEMA_V2)


async def _sides(uid: uuid.UUID, scenario_id: uuid.UUID):
    """load → assemble → project, for both sides."""
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        baseline, counterfactual = await hs.load_sealed_sides(s, uid, scenario_id)
    return tuple(
        ComparisonSide(
            graph=project_scenario_comparable_graph(
                assemble_historical_graph(bundle, user_id=uid)),
            authority=bundle.authority,
        )
        for bundle in (baseline, counterfactual)
    )


async def _compare(uid: uuid.UUID, scenario_id: uuid.UUID):
    baseline, counterfactual = await _sides(uid, scenario_id)
    return compare_scenario_graphs(baseline, counterfactual)


def _lines(comparison):
    return {
        n.key.rsplit(":", 2)[-2:][0] + ":" + n.key.rsplit(":", 1)[-1]: n
        for n in comparison.node_changes
        if n.family == NodeType.TAX_STATE.value
        and "line_items" in n.key
    }


# ===========================================================================
# §26 — the ordinary v3 production path, end to end
# ===========================================================================
@pytest.mark.asyncio
async def test_an_ordinary_v3_scenario_compares_end_to_end():
    """THE CENTRAL ACCEPTANCE PROOF. Sealed through the public entry point,
    compared through the whole read path, with every recomputation counted."""
    import app.services.ioe.portfolio.eligibility as eligibility
    import app.services.ioe.portfolio.service as portfolio
    import app.services.ioe.scenario.service as scenario_module
    import app.services.tax_engine.rules_service as rules_module
    import app.services.tax_engine.service as engine_service

    uid, analysis_id = await _user_with_baseline_analysis()
    await _hold(uid)
    await _publish_rule()
    outcome = await _seal_public(uid, analysis_id)

    engine_runs: list[int] = []
    evaluations: list[int] = []
    statements: list[str] = []
    real_scenario_compute = scenario_module.compute
    real_engine_compute = engine_service.compute
    real_evaluate = rules_module.RulesEvaluatorService.evaluate

    def counting_scenario(inp):
        engine_runs.append(1)
        return real_scenario_compute(inp)

    def counting_engine(inp):
        engine_runs.append(1)
        return real_engine_compute(inp)

    async def counting_evaluate(self, *a, **kw):
        evaluations.append(1)
        return await real_evaluate(self, *a, **kw)

    def record(conn, cur, statement, parameters, context, executemany):
        statements.append(statement.lower())

    from app.database.session import engine

    scenario_module.compute = counting_scenario      # type: ignore[assignment]
    engine_service.compute = counting_engine         # type: ignore[assignment]
    for module in (portfolio, eligibility):
        if getattr(module, "compute", None) is not None:
            module.compute = counting_engine         # type: ignore[assignment]
    rules_module.RulesEvaluatorService.evaluate = counting_evaluate  # type: ignore[method-assign]
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        comparison = await _compare(uid, outcome.scenario_id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
        scenario_module.compute = real_scenario_compute   # type: ignore[assignment]
        engine_service.compute = real_engine_compute      # type: ignore[assignment]
        for module in (portfolio, eligibility):
            if getattr(module, "compute", None) is not None:
                module.compute = real_engine_compute      # type: ignore[assignment]
        rules_module.RulesEvaluatorService.evaluate = real_evaluate  # type: ignore[method-assign]

    assert engine_runs == [], f"TaxEngineService ran {len(engine_runs)} times"
    assert evaluations == [], f"RulesEvaluatorService ran {len(evaluations)} times"
    assert [x for x in statements if "docs.document" in x] == []
    assert [x for x in statements
            if "optimization_run" in x or "optimization_candidate" in x] == []
    assert [x for x in statements if "reco.recommendation" in x] == []

    # ...and it produced a real comparison, or the counters prove nothing.
    assert comparison.node_changes
    assert comparison.comparison_hash
    assert comparison.direction == "BASELINE_TO_COUNTERFACTUAL"


@pytest.mark.asyncio
async def test_the_comparison_issues_no_statement_after_the_sources_are_loaded():
    """§26's boundary: loading is I/O, comparing is not."""
    uid, analysis_id = await _user_with_baseline_analysis()
    await _publish_rule()
    outcome = await _seal_public(uid, analysis_id)

    statements: list[str] = []

    def record(conn, cur, statement, parameters, context, executemany):
        statements.append(statement.lower())

    from app.database.session import engine

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        baseline, counterfactual = await _sides(uid, outcome.scenario_id)
        after_load = len(statements)
        compare_scenario_graphs(baseline, counterfactual)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert len(statements) == after_load, "the comparison issued a query"


# ===========================================================================
# §8 — the tax-state acceptance family, over sealed values
# ===========================================================================
@pytest.mark.asyncio
async def test_an_untouched_line_is_unchanged_and_a_moved_one_carries_a_delta():
    """The two halves of §8 that a real RRSP lever actually produces."""
    uid, analysis_id = await _user_with_baseline_analysis()
    await _publish_rule()
    outcome = await _seal_public(uid, analysis_id, amount="15000")

    comparison = await _compare(uid, outcome.scenario_id)
    by_key = {n.key: n for n in comparison.node_changes}

    income = ("TAX_STATE:ioe.scenario_result.line_items:income:Total income")
    deductions = (
        "TAX_STATE:ioe.scenario_result.line_items:deduction:Total deductions")

    assert by_key[income].change is ChangeKind.UNCHANGED, (
        "an RRSP deduction must not move total income")
    assert by_key[deductions].change is ChangeKind.CHANGED

    (amount,) = [f for f in by_key[deductions].fields
                 if f.field == "attributes.amount"]
    assert amount.delta is not None
    assert Decimal(amount.delta) == Decimal(amount.after) - Decimal(amount.before)
    assert Decimal(amount.delta) > 0, "the lever increased the deduction"


@pytest.mark.asyncio
async def test_the_sealed_opportunity_appears_on_both_sides_as_unchanged():
    """The phantom-addition defect, seen from the comparator. Before Entry 12B's
    baseline work this opportunity existed on one side only and would have been
    reported as something the scenario created."""
    uid, analysis_id = await _user_with_baseline_analysis()
    await _hold(uid)
    await _publish_rule()
    outcome = await _seal_public(uid, analysis_id)

    comparison = await _compare(uid, outcome.scenario_id)
    opportunities = [n for n in comparison.node_changes
                     if n.family == NodeType.OPPORTUNITY.value]

    assert opportunities, "no opportunity reached the comparison"
    assert any(n.change is ChangeKind.UNCHANGED for n in opportunities), (
        "every opportunity was reported as a difference; the baseline side is "
        "not being compared against")
    assert not [n for n in opportunities if n.change is ChangeKind.ADDED], (
        "an opportunity was manufactured as an addition")


# ===========================================================================
# §25 — legacy v2 fails closed
# ===========================================================================
@pytest.mark.asyncio
async def test_a_legacy_v2_scenario_is_refused_rather_than_compared():
    """§25. A v2 seal carries no baseline opportunity authority, so it is
    non-comparable — never a misleading diff, and never a reconstruction."""
    import app.services.tax_engine.rules_service as rules_module

    uid, analysis_id = await _user_with_baseline_analysis()
    await _publish_rule()
    outcome = await _seal_legacy_v2(uid, analysis_id)

    evaluations: list[int] = []
    real_evaluate = rules_module.RulesEvaluatorService.evaluate

    async def counting(self, *a, **kw):
        evaluations.append(1)
        return await real_evaluate(self, *a, **kw)

    rules_module.RulesEvaluatorService.evaluate = counting  # type: ignore[method-assign]
    try:
        baseline, counterfactual = await _sides(uid, outcome.scenario_id)
        with pytest.raises(DependencyUnavailable) as caught:
            compare_scenario_graphs(baseline, counterfactual)
    finally:
        rules_module.RulesEvaluatorService.evaluate = real_evaluate  # type: ignore[method-assign]

    assert caught.value.reason is IntegrityReason.SEALED_EVIDENCE_INCOMPLETE
    assert evaluations == [], "the refusal path evaluated rules"
    assert "OPPORTUNITY" in baseline.authority and (
        baseline.authority["OPPORTUNITY"].value == "MISSING_AUTHORITY")


# ===========================================================================
# §21 / §22 — direction and self-comparison over sealed state
# ===========================================================================
@pytest.mark.asyncio
async def test_direction_inverts_over_real_sealed_state():
    uid, analysis_id = await _user_with_baseline_analysis()
    await _publish_rule()
    outcome = await _seal_public(uid, analysis_id, amount="15000")

    baseline, counterfactual = await _sides(uid, outcome.scenario_id)
    forward = compare_scenario_graphs(baseline, counterfactual)
    backward = compare_scenario_graphs(counterfactual, baseline)

    assert invert(forward) == backward
    assert invert(forward).comparison_hash == backward.comparison_hash


@pytest.mark.asyncio
async def test_a_sealed_side_compared_with_itself_reports_no_change():
    uid, analysis_id = await _user_with_baseline_analysis()
    await _publish_rule()
    outcome = await _seal_public(uid, analysis_id)

    _, counterfactual = await _sides(uid, outcome.scenario_id)
    comparison = compare_scenario_graphs(counterfactual, counterfactual)

    assert all(n.change is ChangeKind.UNCHANGED for n in comparison.node_changes)
    assert all(e.change is ChangeKind.UNCHANGED for e in comparison.edge_changes)
    assert comparison.summary.changed_families == ()


# ===========================================================================
# §27 — no live fallback
# ===========================================================================
@pytest.mark.asyncio
async def test_the_comparison_does_not_move_when_live_state_moves():
    """§27. Seal, compare, then churn current financials, current rules and
    current documents. The comparison must be byte-identical."""
    import app.services.tax_engine.rules_service as rules_module
    import app.services.tax_engine.service as engine_service

    uid, analysis_id = await _user_with_baseline_analysis()
    await _hold(uid)
    await _publish_rule()
    outcome = await _seal_public(uid, analysis_id)

    before = await _compare(uid, outcome.scenario_id)
    before_text = canonical_comparison_text(before)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        row = await s.scalar(
            select(IncomeSource).where(IncomeSource.user_id == uid))
        row.amount = Decimal("250000")
    await _publish_rule("post-seal rule")
    await _hold(uid, "RRSP")

    engine_runs: list[int] = []
    evaluations: list[int] = []
    statements: list[str] = []
    real_engine_compute = engine_service.compute
    real_evaluate = rules_module.RulesEvaluatorService.evaluate

    def counting_engine(inp):
        engine_runs.append(1)
        return real_engine_compute(inp)

    async def counting_evaluate(self, *a, **kw):
        evaluations.append(1)
        return await real_evaluate(self, *a, **kw)

    def record(conn, cur, statement, parameters, context, executemany):
        statements.append(statement.lower())

    from app.database.session import engine

    engine_service.compute = counting_engine        # type: ignore[assignment]
    rules_module.RulesEvaluatorService.evaluate = counting_evaluate  # type: ignore[method-assign]
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        after = await _compare(uid, outcome.scenario_id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
        engine_service.compute = real_engine_compute     # type: ignore[assignment]
        rules_module.RulesEvaluatorService.evaluate = real_evaluate  # type: ignore[method-assign]

    assert canonical_comparison_text(after) == before_text
    assert after.comparison_hash == before.comparison_hash
    assert after.node_changes == before.node_changes
    assert after.edge_changes == before.edge_changes
    assert engine_runs == [] and evaluations == []
    assert [x for x in statements if "docs.document" in x] == []


# ===========================================================================
# §31 — measured cost
# ===========================================================================
@pytest.mark.asyncio
async def test_comparison_cost_is_measured_and_linear():
    """§31. Keyed maps by semantic identity, never pairwise matching: cost per
    element must not grow with the graph."""
    from app.services.ioe.scenario.historical_source import (
        COMPARISON_REQUIRED_FAMILIES,
        SourceAuthority,
    )
    from app.services.state_graph.contracts import (
        EdgeType,
        GraphAnchor,
        GraphEdge,
        GraphNode,
        GraphScope,
        GraphView,
        NodeFreshness,
        Provenance,
        TaxStateGraph,
        summarize,
    )
    from app.services.state_graph.contracts import (
        NodeType as NT,
    )
    from app.services.state_graph.hashing import compute_graph_hash

    complete = {f: SourceAuthority.AUTHORITATIVE
                for f in COMPARISON_REQUIRED_FAMILIES}

    def synthetic(size: int, *, shift: int) -> ComparisonSide:
        scope = GraphScope(user_id=str(uuid.uuid4()), tax_year=TAX_YEAR,
                           view=GraphView.HISTORICAL)
        header = GraphNode(
            node_type=NT.TAX_STATE, source_kind="ioe.scenario_result",
            source_id="h", provenance=Provenance.ENGINE_COMPUTED,
            freshness=NodeFreshness.NOT_TRACKED)
        nodes = [header]
        edges = []
        for i in range(size):
            item = GraphNode(
                node_type=NT.TAX_STATE,
                source_kind="ioe.scenario_result.line_items",
                source_id=f"k:{i:06d}", provenance=Provenance.ENGINE_COMPUTED,
                freshness=NodeFreshness.NOT_TRACKED,
                attributes={"amount": f"{i + shift}.00"})
            nodes.append(item)
            edges.append(GraphEdge(EdgeType.DERIVED_FROM, item.key, header.key))
        summary = summarize(nodes, edges)
        graph = TaxStateGraph(
            scope=scope,
            anchors=(GraphAnchor(artifact="ioe.scenario", artifact_id="a"),),
            nodes=tuple(sorted(nodes, key=lambda n: n.key)),
            edges=tuple(sorted(edges, key=lambda e: e.sort_key)),
            summary=summary,
            graph_hash=compute_graph_hash(
                scope=scope,
                anchors=(GraphAnchor(artifact="ioe.scenario", artifact_id="a"),),
                nodes=nodes, edges=edges, summary=summary))
        return ComparisonSide(
            graph=project_scenario_comparable_graph(graph),
            authority=dict(complete))

    report = []
    per_element = {}
    for label, size in (("small", 10), ("moderate", 200), ("stress", 2000)):
        left, right = synthetic(size, shift=0), synthetic(size, shift=1)
        timings = []
        for _ in range(5):
            start = time.perf_counter()
            comparison = compare_scenario_graphs(left, right)
            timings.append((time.perf_counter() - start) * 1000)
        timings.sort()
        elapsed = timings[len(timings) // 2]
        elements = (len(left.graph.graph.nodes) + len(left.graph.graph.edges)
                    + len(right.graph.graph.nodes) + len(right.graph.graph.edges))
        per_element[label] = elapsed / elements
        report.append({
            "label": label,
            "baseline_nodes": len(left.graph.graph.nodes),
            "counterfactual_nodes": len(right.graph.graph.nodes),
            "baseline_edges": len(left.graph.graph.edges),
            "counterfactual_edges": len(right.graph.graph.edges),
            "comparison_ms": round(elapsed, 3),
            "us_per_element": round(per_element[label] * 1000, 3),
            "output_records": (len(comparison.node_changes)
                               + len(comparison.edge_changes)),
            "output_bytes": len(canonical_comparison_text(comparison).encode()),
        })

    print("\n12B comparison cost:")                                 # noqa: T201
    for row in report:
        print(f"  {row}")                                           # noqa: T201

    # O(n + m): a pairwise matcher would show `stress` costing many times more
    # per element than `small`. The ceiling is loose — a shape check, not a
    # budget.
    assert per_element["stress"] < per_element["small"] * 8, per_element


# ===========================================================================
# §32 — isolation
# ===========================================================================
@pytest.mark.asyncio
async def test_two_independent_scenarios_compare_independently():
    """No shared-state leakage between comparisons: each scenario's comparison
    names its own artifacts."""
    uid_a, analysis_a = await _user_with_baseline_analysis()
    uid_b, analysis_b = await _user_with_baseline_analysis()
    await _publish_rule()
    a = await _seal_public(uid_a, analysis_a, amount="5000")
    b = await _seal_public(uid_b, analysis_b, amount="15000")

    left = await _compare(uid_a, a.scenario_id)
    right = await _compare(uid_b, b.scenario_id)

    assert left.comparison_hash != right.comparison_hash
    assert left.baseline_graph_hash != right.baseline_graph_hash
