"""Entry 12B1 §17 — the projection over genuinely sealed state.

The unit suite proves the policy in isolation. These prove the COMPLETE customer
historical-read path: sealed bundle in, two symmetric projected graphs out, with
nothing recomputed on the way.

WHAT "NOTHING RECOMPUTED" MEANS HERE, AND WHY IT IS COUNTED RATHER THAN ASSERTED
--------------------------------------------------------------------------------
A customer reading a sealed scenario must get what was sealed. Every way of
getting something else is a call: the tax engine, the rules evaluator, the
optimizer, a latest-rule resolver, or `docs.document`. So each is wrapped and
counted, and each must be zero — an absent SQL string is weaker evidence than a
counter that stayed at zero while the whole path ran.

EVERY FIXTURE ESTABLISHES ITS OWN RULE STATE
--------------------------------------------
Candidates, opportunities, deadlines and evidence requirements all come from the
pinned rules evaluation. A test that asserts on any of them without publishing a
rule of its own is really asserting on whatever other tests left in the shared
database — which is exactly how the §16 bundle test passed for the wrong reason
before it was fixed. `_publish_rule_requiring` is called by every test here that
depends on one.
"""
import time
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from app.database.models import (
    AnalysisInputSnapshot,
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
from app.services.ioe.domain.scenario import (
    SCENARIO_RESULT_SCHEMA_V2,
    ScenarioSpec,
)
from app.services.ioe.scenario import historical_source as hs
from app.services.ioe.scenario.historical_graph import assemble_historical_graph
from app.services.ioe.scenario.service import ScenarioService
from app.services.state_graph.contracts import EdgeType, NodeType
from app.services.state_graph.scenario_projection import (
    Comparability,
    canonical_projection_text,
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
async def _user_with_analysis() -> tuple[uuid.UUID, uuid.UUID]:
    async with unit_of_work(actor_type="system") as s:
        account = UserAccount(
            email=f"proj_{uuid.uuid4().hex[:8]}@test.ca", status="active")
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
        await s.flush()
        return uid, run.id


async def _publish_rule_requiring(
    document_type_code: str = "T4", *, description: str = "12B1 §17 fixture"
) -> uuid.UUID:
    """A published rule for the tax year with an outcome, a required document
    and a deadline.

    THE PRECONDITION EVERY ASSERTION HERE DEPENDS ON. Candidates come from the
    pinned rules evaluation; requirements and deadlines are reached through the
    rule version a candidate pins. Without publishing one, a test asserting that
    any of those families is non-empty is asserting on database residue.
    """
    async with unit_of_work(actor_type="admin") as s:
        jurisdiction = await s.scalar(
            select(Jurisdiction).where(Jurisdiction.code == "FED"))
        code = f"PRJ_{uuid.uuid4().hex[:8].upper()}"
        rule = TaxRule(code=code, name=code, category="deduction",
                       jurisdiction_id=jurisdiction.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=TAX_YEAR,
            effective_date=date(TAX_YEAR, 1, 1), status="published",
            description=description, eligibility_basis_codes=["BASIS_PRJ"])
        s.add(version)
        await s.flush()
        s.add(RuleOutcome(
            rule_version_id=version.id, outcome_type="recommend", priority=1,
            title_template=f"{description} opportunity"))
        s.add(RuleRequiredDocument(
            rule_version_id=version.id, document_type_code=document_type_code,
            necessity="required"))
        s.add(RuleDeadline(
            rule_version_id=version.id, deadline_code=f"DL_{code}",
            deadline_date=date(TAX_YEAR + 1, 4, 30), is_hard=True,
            jurisdiction_code="FED"))
        await s.flush()
        return version.id


async def _hold(uid: uuid.UUID, code: str) -> uuid.UUID:
    async with unit_of_work(actor_type="system") as s:
        type_id = (await s.scalar(
            select(DocumentType).where(DocumentType.code == code))).id
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        # Every locator is long and unique. A one-character bucket would be a
        # substring of any document at all, and the identity test that searches
        # for it would pass or fail for reasons having nothing to do with the
        # projection.
        document = Document(
            user_id=uid, document_type_id=type_id, tax_year=TAX_YEAR,
            bucket=f"onyx-bucket-{uuid.uuid4().hex}",
            object_key=f"{uid}/v2/{uuid.uuid4()}",
            content_hash=uuid.uuid4().hex, status="processed")
        s.add(document)
        await s.flush()
        return document.id


async def _seal_v2(uid: uuid.UUID, analysis_id: uuid.UUID):
    return await ScenarioService(uid)._simulate(
        analysis_id,
        ScenarioSpec.parse(
            [{"lever_code": RRSP, "parameters": {"amount": Decimal("5000")}}]),
        result_schema_version=SCENARIO_RESULT_SCHEMA_V2)


async def _projected_sides(uid: uuid.UUID, scenario_id: uuid.UUID):
    """THE COMPLETE HISTORICAL READ PATH, in the order §14 specifies."""
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        baseline_bundle, counterfactual_bundle = await hs.load_sealed_sides(
            s, uid, scenario_id)
    return (
        project_scenario_comparable_graph(
            assemble_historical_graph(baseline_bundle, user_id=uid)),
        project_scenario_comparable_graph(
            assemble_historical_graph(counterfactual_bundle, user_id=uid)),
    )


def _by_family(entries):
    return {e.family: e for e in entries}


# ===========================================================================
# §14 — the complete historical read path, with every recomputation counted
# ===========================================================================
@pytest.mark.asyncio
async def test_the_whole_projected_path_runs_no_engine_evaluator_or_optimizer():
    """THE CENTRAL §14 ACCEPTANCE PROOF.

    Load, assemble and project both sides while every recomputation route is
    wrapped and counted. All five counters must be zero, and the path must still
    have produced real content — a path that produced nothing would also run
    nothing.
    """
    import app.services.ioe.portfolio.eligibility as eligibility
    import app.services.ioe.portfolio.service as portfolio
    import app.services.tax_engine.core.engine as engine_module
    import app.services.tax_engine.rules_service as rules_module

    uid, analysis_id = await _user_with_analysis()
    await _hold(uid, "T4")
    await _publish_rule_requiring("T4")
    outcome = await _seal_v2(uid, analysis_id)

    engine_runs: list[int] = []
    evaluations: list[int] = []
    resolutions: list[int] = []
    statements: list[str] = []

    real_compute = engine_module.compute
    real_evaluate = rules_module.RulesEvaluatorService.evaluate
    real_resolve = getattr(
        rules_module.RulesEvaluatorService, "_resolve_rule_versions", None)

    def counting_compute(inp):
        engine_runs.append(1)
        return real_compute(inp)

    async def counting_evaluate(self, *a, **kw):
        evaluations.append(1)
        return await real_evaluate(self, *a, **kw)

    async def counting_resolve(self, *a, **kw):
        resolutions.append(1)
        return await real_resolve(self, *a, **kw)

    def record(conn, cur, statement, parameters, context, executemany):
        statements.append(statement.lower())

    from app.database.session import engine

    for module in (engine_module, portfolio, eligibility):
        if getattr(module, "compute", None) is not None:
            module.compute = counting_compute          # type: ignore[assignment]
    rules_module.RulesEvaluatorService.evaluate = counting_evaluate  # type: ignore[method-assign]
    if real_resolve is not None:
        rules_module.RulesEvaluatorService._resolve_rule_versions = (  # type: ignore[method-assign]
            counting_resolve)
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        baseline, counterfactual = await _projected_sides(
            uid, outcome.scenario_id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
        for module in (engine_module, portfolio, eligibility):
            if getattr(module, "compute", None) is not None:
                module.compute = real_compute          # type: ignore[assignment]
        rules_module.RulesEvaluatorService.evaluate = real_evaluate  # type: ignore[method-assign]
        if real_resolve is not None:
            rules_module.RulesEvaluatorService._resolve_rule_versions = (  # type: ignore[method-assign]
                real_resolve)

    assert engine_runs == [], f"TaxEngineService ran {len(engine_runs)} times"
    assert evaluations == [], f"RulesEvaluatorService ran {len(evaluations)} times"
    assert resolutions == [], f"a rule resolver ran {len(resolutions)} times"
    assert [x for x in statements if "docs.document" in x] == [], (
        "the projected historical read touched the live document library")
    assert [x for x in statements
            if "optimization_run" in x or "optimization_candidate" in x] == [], (
        "the projected historical read touched optimizer output")

    # ...and it actually produced something, or the counters prove nothing.
    assert counterfactual.graph.nodes_of(NodeType.OPPORTUNITY), (
        "no sealed opportunity reached the projection")
    assert counterfactual.graph.nodes_of(NodeType.EVIDENCE)
    assert baseline.graph.nodes_of(NodeType.FACT)


@pytest.mark.asyncio
async def test_both_projected_sides_agree_on_every_applicability_verdict():
    """§8 over real sealed state. One projection policy, two sides."""
    uid, analysis_id = await _user_with_analysis()
    await _hold(uid, "T4")
    await _publish_rule_requiring("T4")
    outcome = await _seal_v2(uid, analysis_id)

    baseline, counterfactual = await _projected_sides(uid, outcome.scenario_id)

    def verdicts(projected):
        return {
            e.family: (e.status, e.reason_code)
            for e in (*projected.node_applicability,
                      *projected.edge_applicability,
                      *projected.identity_exclusions)
        }

    assert verdicts(baseline) == verdicts(counterfactual)
    assert baseline.contract_version == counterfactual.contract_version
    # The two sides differ in CONTENT — the lever moved a fact and the sealed
    # counterfactual carries opportunities the baseline bundle does not.
    assert baseline.graph.graph_hash != counterfactual.graph.graph_hash


@pytest.mark.asyncio
async def test_the_sealed_sides_carry_the_expected_families():
    """The eight families, over sealed state, with the two policy outcomes
    stated explicitly rather than inferred from counts."""
    uid, analysis_id = await _user_with_analysis()
    await _hold(uid, "T4")
    await _publish_rule_requiring("T4")
    outcome = await _seal_v2(uid, analysis_id)

    baseline, counterfactual = await _projected_sides(uid, outcome.scenario_id)

    families = _by_family(counterfactual.node_applicability)
    for present in (NodeType.FACT, NodeType.TAX_STATE, NodeType.OPPORTUNITY,
                    NodeType.DEADLINE, NodeType.EVIDENCE, NodeType.SCENARIO):
        assert families[present.value].retained, present

    resource = families[NodeType.RESOURCE.value]
    assert resource.status is (
        Comparability.NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON)
    assert resource.retained is None

    # The baseline bundle seals no counterfactual line items or candidates, so
    # those families are COMPARABLE and hold zero on this side. That is the
    # distinction §4 exists to keep: zero is not inapplicable.
    baseline_families = _by_family(baseline.node_applicability)
    assert baseline_families[NodeType.OPPORTUNITY.value].status is (
        Comparability.COMPARABLE)
    assert baseline_families[NodeType.OPPORTUNITY.value].retained == 0
    assert baseline_families[NodeType.RESOURCE.value].retained is None

    # No portfolio edge and no identity edge survived on either side.
    for side in (baseline, counterfactual):
        kinds = {e.edge_type for e in side.graph.edges}
        assert not (kinds & {
            EdgeType.SUPPORTED_BY, EdgeType.INELIGIBLE_BECAUSE,
            EdgeType.CONSTRAINED_BY, EdgeType.CONSUMES_RESOURCE,
            EdgeType.CONFLICTS_WITH})


@pytest.mark.asyncio
async def test_no_document_identity_survives_into_the_projection():
    """§7 over real state: the user genuinely holds a document, and its id
    appears nowhere in either projected side while its readiness does."""
    uid, analysis_id = await _user_with_analysis()
    document_id = await _hold(uid, "T4")
    await _publish_rule_requiring("T4")
    outcome = await _seal_v2(uid, analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        document = await s.get(Document, document_id)
        stored = (str(document.id), document.object_key, document.content_hash,
                  document.bucket)

    baseline, counterfactual = await _projected_sides(uid, outcome.scenario_id)

    for side in (baseline, counterfactual):
        text = canonical_projection_text(side)
        # The VALUES that identify or locate the document, none of which the
        # projection may carry. Asserted on the values rather than on field
        # names, because `content_hash` is also a `GraphAnchor` field naming the
        # sealed scenario result — a name scan would fail on a true statement.
        for secret in stored:
            assert secret not in text, f"{secret!r} reached the comparable view"
        # `docs.document` appears exactly once and only as the NAME of the
        # excluded family — the metadata that declares the exclusion. What must
        # not exist is a node carrying that source kind.
        assert text.count("docs.document") == 1
        assert _by_family(side.identity_exclusions)["EVIDENCE:docs.document"]
        assert not [n for n in side.graph.nodes
                    if n.source_kind == "docs.document"]
        assert not [n for n in side.graph.nodes
                    if {"object_key", "bucket", "document_id"} & set(n.attributes)]

    held = [n for n in counterfactual.graph.nodes_of(NodeType.EVIDENCE)
            if n.attributes.get("evidence_kind") == "held_evidence_type"]
    assert [n.attributes["document_type_code"] for n in held] == ["T4"], (
        "the sealed held TYPE semantics were lost")
    requirements = [n for n in counterfactual.graph.nodes_of(NodeType.EVIDENCE)
                    if n.attributes.get("evidence_kind") == "requirement"]
    assert requirements, "requirements were lost"
    assert all(n.attributes["readiness"] for n in requirements)


@pytest.mark.asyncio
async def test_a_required_document_the_user_lacks_projects_as_missing():
    """§10. Readiness derives from sealed held evidence and sealed required
    evidence, and a legitimate `READY -> MISSING` transition stays observable
    with no document identity anywhere."""
    uid, analysis_id = await _user_with_analysis()
    await _hold(uid, "T4")
    # The rule requires a type the user does NOT hold.
    await _publish_rule_requiring("RRSP")
    outcome = await _seal_v2(uid, analysis_id)

    _, counterfactual = await _projected_sides(uid, outcome.scenario_id)

    readiness = {
        n.attributes["document_type_code"]: n.attributes["readiness"]
        for n in counterfactual.graph.nodes_of(NodeType.EVIDENCE)
        if n.attributes.get("evidence_kind") == "requirement"
    }
    assert readiness.get("RRSP") == "MISSING", readiness


# ===========================================================================
# §15 — no live fallback
# ===========================================================================
@pytest.mark.asyncio
async def test_the_projection_does_not_move_when_live_state_moves():
    """§15. Seal, capture both projections, then independently mutate current
    financial data, current published rules and current documents. Reload and
    project again: the historical reconstruction must be byte-identical.

    This is the property the whole entry exists for. Freshness of the LIVE graph
    may change all it likes; what was sealed in March may not be rewritten in
    June by an upload the user made yesterday.
    """
    uid, analysis_id = await _user_with_analysis()
    await _hold(uid, "T4")
    await _publish_rule_requiring("T4")
    outcome = await _seal_v2(uid, analysis_id)

    before_baseline, before_counterfactual = await _projected_sides(
        uid, outcome.scenario_id)
    before = (
        canonical_projection_text(before_baseline),
        canonical_projection_text(before_counterfactual),
    )

    # A. current financial data moves
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        row = await s.scalar(
            select(IncomeSource).where(IncomeSource.user_id == uid))
        row.amount = Decimal("250000")
    # B. current published rules move — a brand new rule for the same year
    await _publish_rule_requiring("T4A", description="post-seal rule")
    # C. current documents move
    await _hold(uid, "RRSP")

    after_baseline, after_counterfactual = await _projected_sides(
        uid, outcome.scenario_id)

    assert canonical_projection_text(after_baseline) == before[0]
    assert canonical_projection_text(after_counterfactual) == before[1]
    assert after_baseline.graph.graph_hash == before_baseline.graph.graph_hash
    assert after_counterfactual.graph.graph_hash == (
        before_counterfactual.graph.graph_hash)

    # Applicability metadata is part of the frozen answer, not decoration.
    assert after_baseline.node_applicability == (
        before_baseline.node_applicability)
    assert after_counterfactual.edge_applicability == (
        before_counterfactual.edge_applicability)
    assert after_counterfactual.identity_exclusions == (
        before_counterfactual.identity_exclusions)


@pytest.mark.asyncio
async def test_the_projection_still_runs_nothing_after_the_live_state_moved():
    """The zero-invocation proof, re-run on the mutated world. A fallback that
    only engages when live state has diverged would pass the first proof and
    fail the user."""
    import app.services.tax_engine.core.engine as engine_module
    import app.services.tax_engine.rules_service as rules_module

    uid, analysis_id = await _user_with_analysis()
    await _hold(uid, "T4")
    await _publish_rule_requiring("T4")
    outcome = await _seal_v2(uid, analysis_id)

    await _publish_rule_requiring("T4A", description="post-seal rule")
    await _hold(uid, "RRSP")

    engine_runs: list[int] = []
    evaluations: list[int] = []
    statements: list[str] = []
    real_compute = engine_module.compute
    real_evaluate = rules_module.RulesEvaluatorService.evaluate

    def counting_compute(inp):
        engine_runs.append(1)
        return real_compute(inp)

    async def counting_evaluate(self, *a, **kw):
        evaluations.append(1)
        return await real_evaluate(self, *a, **kw)

    def record(conn, cur, statement, parameters, context, executemany):
        statements.append(statement.lower())

    from app.database.session import engine

    engine_module.compute = counting_compute            # type: ignore[assignment]
    rules_module.RulesEvaluatorService.evaluate = counting_evaluate  # type: ignore[method-assign]
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        await _projected_sides(uid, outcome.scenario_id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
        engine_module.compute = real_compute            # type: ignore[assignment]
        rules_module.RulesEvaluatorService.evaluate = real_evaluate  # type: ignore[method-assign]

    assert engine_runs == []
    assert evaluations == []
    assert [x for x in statements if "docs.document" in x] == []


# ===========================================================================
# §16 — the current graph is untouched
# ===========================================================================
@pytest.mark.asyncio
async def test_the_current_graph_still_carries_resource_and_its_own_hash():
    """§16. The projection is an ADDITIONAL read view. The canonical current
    graph keeps every live family, including the one comparison excludes."""
    from app.services.state_graph import GraphLoader, assemble_graph
    from app.services.state_graph.contracts import (
        LIVE_NODE_TYPES,
        RESERVED_NODE_TYPES,
    )

    uid, _ = await _user_with_analysis()
    await _hold(uid, "T4")
    await _publish_rule_requiring("T4")

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        sources = await GraphLoader(s, uid).load(tax_year=TAX_YEAR)
    graph = assemble_graph(sources, user_id=uid, tax_year=TAX_YEAR)

    assert len(LIVE_NODE_TYPES) == 8
    assert NodeType.RESOURCE in LIVE_NODE_TYPES, (
        "RESOURCE must remain a valid family of the full current graph")
    assert graph.scope.view.value == "current"
    for reserved in RESERVED_NODE_TYPES:
        assert graph.summary.nodes_by_type[reserved.value] == 0

    # Assembling twice over unchanged state yields the same hash: the projection
    # did not reach into the canonical assembler.
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        again = await GraphLoader(s, uid).load(tax_year=TAX_YEAR)
    assert assemble_graph(
        again, user_id=uid, tax_year=TAX_YEAR).graph_hash == graph.graph_hash


# ===========================================================================
# §17 — measured cost
# ===========================================================================
@pytest.mark.asyncio
async def test_projection_cost_is_measured_and_linear_in_the_graph():
    """§17. Reported, not merely bounded — a gate nobody can read the numbers of
    is a gate that drifts.

    The synthetic sizes exist because a real sealed scenario on a fresh database
    yields a handful of nodes, and O(n²) does not show up at n=10. The end-to-end
    row below is the real one: sealed-source load, assembly, projection and the
    SQL statement count for an actual sealed v2 scenario.
    """
    from app.services.state_graph.contracts import (
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
    from app.services.state_graph.hashing import compute_graph_hash

    def synthetic(size: int) -> TaxStateGraph:
        """One `TAX_STATE` header, `size` components and `size` resource nodes,
        so the projection has both a retained and an excluded family to walk."""
        scope = GraphScope(user_id=str(uuid.uuid4()), tax_year=TAX_YEAR,
                           view=GraphView.HISTORICAL)
        header = GraphNode(
            node_type=NodeType.TAX_STATE, source_kind="ioe.scenario_result",
            source_id="h", provenance=Provenance.ENGINE_COMPUTED,
            freshness=NodeFreshness.NOT_TRACKED)
        nodes = [header]
        edges = []
        for i in range(size):
            item = GraphNode(
                node_type=NodeType.TAX_STATE,
                source_kind="ioe.scenario_result.line_items",
                source_id=f"k:{i:06d}", provenance=Provenance.ENGINE_COMPUTED,
                freshness=NodeFreshness.NOT_TRACKED,
                attributes={"amount": "1.00"})
            nodes.append(item)
            edges.append(GraphEdge(EdgeType.DERIVED_FROM, item.key, header.key))
            nodes.append(GraphNode(
                node_type=NodeType.RESOURCE,
                source_kind="ioe.resource_ledger_entry", source_id=f"r{i:06d}",
                provenance=Provenance.ENGINE_COMPUTED,
                freshness=NodeFreshness.NOT_TRACKED))
        anchors = (GraphAnchor(artifact="ioe.scenario", artifact_id="a"),)
        summary = summarize(nodes, edges)
        return TaxStateGraph(
            scope=scope, anchors=anchors,
            nodes=tuple(sorted(nodes, key=lambda n: n.key)),
            edges=tuple(sorted(edges, key=lambda e: e.sort_key)),
            summary=summary,
            graph_hash=compute_graph_hash(
                scope=scope, anchors=anchors, nodes=nodes, edges=edges,
                summary=summary))

    report = []
    per_element = {}
    for label, size in (("small", 10), ("moderate", 200), ("stress", 2000)):
        graph = synthetic(size)
        # Median of repeated rounds, so one slow round is not the headline.
        timings = []
        for _ in range(5):
            start = time.perf_counter()
            projected = project_scenario_comparable_graph(graph)
            timings.append((time.perf_counter() - start) * 1000)
        timings.sort()
        elapsed = timings[len(timings) // 2]
        elements = len(graph.nodes) + len(graph.edges)
        per_element[label] = elapsed / elements
        report.append({
            "label": label,
            "nodes_before": len(graph.nodes),
            "nodes_after": len(projected.graph.nodes),
            "edges_before": len(graph.edges),
            "edges_after": len(projected.graph.edges),
            "projection_ms": round(elapsed, 3),
            "us_per_element": round(per_element[label] * 1000, 3),
        })

    print("\n12B1 §17 projection cost:")                            # noqa: T201
    for row in report:
        print(f"  {row}")                                           # noqa: T201

    # O(n + e): cost per element must not grow with n. A quadratic projection
    # would show `stress` costing many times more per element than `small`.
    # The ceiling is deliberately loose — this is a shape check, not a budget.
    assert per_element["stress"] < per_element["small"] * 8, per_element
    assert report[-1]["nodes_after"] < report[-1]["nodes_before"], (
        "the excluded family was not excluded at scale")


@pytest.mark.asyncio
async def test_the_end_to_end_historical_preparation_cost_is_measured():
    """§17's total: sealed-source load, graph assembly, projection, and the SQL
    statement count for the whole customer historical read."""
    uid, analysis_id = await _user_with_analysis()
    await _hold(uid, "T4")
    await _publish_rule_requiring("T4")
    outcome = await _seal_v2(uid, analysis_id)

    statements: list[str] = []

    def record(conn, cur, statement, parameters, context, executemany):
        statements.append(statement.lower())

    from app.database.session import engine

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        start = time.perf_counter()
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            baseline_bundle, counterfactual_bundle = await hs.load_sealed_sides(
                s, uid, outcome.scenario_id)
        load_ms = (time.perf_counter() - start) * 1000
        load_statements = len(statements)

        start = time.perf_counter()
        graphs = [
            assemble_historical_graph(baseline_bundle, user_id=uid),
            assemble_historical_graph(counterfactual_bundle, user_id=uid),
        ]
        assemble_ms = (time.perf_counter() - start) * 1000

        start = time.perf_counter()
        projected = [project_scenario_comparable_graph(g) for g in graphs]
        project_ms = (time.perf_counter() - start) * 1000
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    print("\n12B1 §17 historical preparation (both sides):")        # noqa: T201
    print(f"  sealed-source load : {load_ms:.2f} ms, "              # noqa: T201
          f"{load_statements} statements")
    print(f"  graph assembly     : {assemble_ms:.2f} ms")           # noqa: T201
    print(f"  projection         : {project_ms:.2f} ms")            # noqa: T201
    print(f"  nodes  : {[len(g.nodes) for g in graphs]} -> "        # noqa: T201
          f"{[len(p.graph.nodes) for p in projected]}")
    print(f"  edges  : {[len(g.edges) for g in graphs]} -> "        # noqa: T201
          f"{[len(p.graph.edges) for p in projected]}")

    # Assembly and projection are pure CPU over already-loaded values: NO
    # statement may be issued after the load returns.
    assert len(statements) == load_statements, (
        "assembly or projection issued a query")
    # The load is a fixed set of family reads, never one query per node.
    assert load_statements <= 25, load_statements


# ===========================================================================
# Determinism and idempotence over sealed state
# ===========================================================================
@pytest.mark.asyncio
async def test_the_projection_of_sealed_state_is_deterministic_and_idempotent():
    uid, analysis_id = await _user_with_analysis()
    await _hold(uid, "T4")
    await _publish_rule_requiring("T4")
    outcome = await _seal_v2(uid, analysis_id)

    first_baseline, first_counterfactual = await _projected_sides(
        uid, outcome.scenario_id)
    second_baseline, second_counterfactual = await _projected_sides(
        uid, outcome.scenario_id)

    assert first_baseline == second_baseline
    assert first_counterfactual == second_counterfactual

    for side in (first_baseline, first_counterfactual):
        assert project_scenario_comparable_graph(side.graph) == side


@pytest.mark.asyncio
async def test_a_v1_scenario_has_nothing_to_project():
    """Fails closed at the §16 boundary, before any projection exists to run.
    A v1 seal never carried counterfactual state, and building one from today's
    sources is exactly what this path exists to prevent."""
    from app.services.ioe.domain.integrity import DependencyUnavailable
    from app.services.ioe.domain.scenario import SCENARIO_RESULT_SCHEMA_V1

    uid, analysis_id = await _user_with_analysis()
    outcome = await ScenarioService(uid)._simulate(
        analysis_id,
        ScenarioSpec.parse(
            [{"lever_code": RRSP, "parameters": {"amount": Decimal("5000")}}]),
        result_schema_version=SCENARIO_RESULT_SCHEMA_V1)

    with pytest.raises(DependencyUnavailable):
        await _projected_sides(uid, outcome.scenario_id)
