"""Entry 12B — baseline historical source completeness.

THE DEFECT THIS CLOSES. `load_sealed_sides` built the baseline as
`_side(SIDE_BASELINE, (), (), ())`: a placeholder, not a read. The projected
baseline therefore carried a header-only TAX_STATE and no opportunities while
the counterfactual carried both — and a comparator run against that pair would
have reported every counterfactual line item as one the scenario ADDED. The
projection was right; the source was empty.

WHAT IS FIXED AND WHAT IS NOT. Baseline TAX_STATE now loads from the analysis
run the scenario pinned — already immutable, already the parent of the frozen
baseline every replay resolves. Baseline OPPORTUNITY has no frozen source at
all, and this suite proves that absence is RECORDED rather than rendered as a
zero, and that a side carrying it is refused for comparison outright.

EVERY FIXTURE ESTABLISHES ITS OWN RULE STATE. Candidates, deadlines and
evidence requirements all come from the pinned rules evaluation; a test that
asserts on them without publishing its own rule is asserting on whatever other
tests left in the shared database.
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
from app.services.ioe.domain.scenario import SCENARIO_RESULT_SCHEMA_V2, ScenarioSpec
from app.services.ioe.frozen.models import reconstruct_tax_input
from app.services.ioe.scenario import historical_source as hs
from app.services.ioe.scenario.historical_graph import assemble_historical_graph
from app.services.ioe.scenario.historical_source import (
    COMPARISON_REQUIRED_FAMILIES,
    SourceAuthority,
    assert_comparison_ready,
)
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
async def _user_with_baseline_analysis(
    *, employment: str = "95000"
) -> tuple[uuid.UUID, uuid.UUID]:
    """A completed analysis with its REAL engine line items persisted.

    The line items are the genuine engine output for the frozen snapshot, exactly
    as `AnalysisService.run` writes them — not fixture-invented numbers. That is
    what lets a baseline line item and its counterfactual counterpart be
    compared at all.
    """
    from app.services.tax_engine.core.engine import compute

    async with unit_of_work(actor_type="system") as s:
        account = UserAccount(
            email=f"bhs_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(account)
        await s.flush()
        uid = account.id
        income_type_id = (await s.scalar(
            select(IncomeType).where(IncomeType.code == "employment"))).id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(TaxProfile(user_id=uid, province_code="ON", marital_status="single"))
        s.add(IncomeSource(
            user_id=uid, tax_year=TAX_YEAR, income_type_id=income_type_id,
            amount=Decimal(employment), province_code="ON"))
        run = AnalysisRun(
            user_id=uid, tax_year=TAX_YEAR, province_code="ON",
            engine_version="py-1.0.0", status="completed",
            started_at=datetime.now(tz=UTC), completed_at=datetime.now(tz=UTC),
            data_verified=True)
        s.add(run)
        await s.flush()
        payload, digest = frozen_snapshot(employment_income=Decimal(employment))
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot=payload, snapshot_hash=digest))

        result = compute(reconstruct_tax_input(payload))
        for i, item in enumerate(result.line_items):
            s.add(AnalysisLineItem(
                analysis_id=run.id, kind=item["kind"], label=item["label"],
                amount=item["amount"], sort_order=i))
        await s.flush()
        return uid, run.id


async def _publish_rule(description: str = "12B baseline fixture") -> uuid.UUID:
    """This test's OWN governed rule, with an outcome, a required document and a
    deadline. Never inherited from a neighbouring test."""
    async with unit_of_work(actor_type="admin") as s:
        jurisdiction = await s.scalar(
            select(Jurisdiction).where(Jurisdiction.code == "FED"))
        code = f"BHS_{uuid.uuid4().hex[:8].upper()}"
        rule = TaxRule(code=code, name=code, category="deduction",
                       jurisdiction_id=jurisdiction.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=TAX_YEAR,
            effective_date=date(TAX_YEAR, 1, 1), status="published",
            description=description, eligibility_basis_codes=["BASIS_BHS"])
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
            object_key=f"{uid}/v2/{uuid.uuid4()}",
            content_hash=uuid.uuid4().hex, status="processed")
        s.add(document)
        await s.flush()
        return document.id


async def _seal(uid: uuid.UUID, analysis_id: uuid.UUID, amount: str = "5000"):
    return await ScenarioService(uid)._simulate(
        analysis_id,
        ScenarioSpec.parse(
            [{"lever_code": RRSP, "parameters": {"amount": Decimal(amount)}}]),
        result_schema_version=SCENARIO_RESULT_SCHEMA_V2)


async def _sides(uid: uuid.UUID, scenario_id: uuid.UUID):
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        return await hs.load_sealed_sides(s, uid, scenario_id)


async def _projected(uid: uuid.UUID, scenario_id: uuid.UUID):
    baseline, counterfactual = await _sides(uid, scenario_id)
    return (
        project_scenario_comparable_graph(
            assemble_historical_graph(baseline, user_id=uid)),
        project_scenario_comparable_graph(
            assemble_historical_graph(counterfactual, user_id=uid)),
    )


def _line_items(graph):
    return {
        (n.attributes["kind"], n.attributes["label"]): n.attributes["amount"]
        for n in graph.nodes_of(NodeType.TAX_STATE)
        if n.source_kind == "ioe.scenario_result.line_items"
    }


# ===========================================================================
# Baseline TAX_STATE authority
# ===========================================================================
@pytest.mark.asyncio
async def test_baseline_tax_state_loads_from_the_pinned_analysis():
    """THE CENTRAL FIX. The baseline's line items come from the analysis run the
    scenario pinned — not from a placeholder, and not from the counterfactual."""
    uid, analysis_id = await _user_with_baseline_analysis()
    await _hold(uid)
    await _publish_rule()
    outcome = await _seal(uid, analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        persisted = list(await s.scalars(select(AnalysisLineItem).where(
            AnalysisLineItem.analysis_id == analysis_id)))
    assert persisted, "fixture must persist authoritative baseline detail"

    baseline, _ = await _sides(uid, outcome.scenario_id)
    assert len(baseline.line_items) == len(persisted)
    assert baseline.authority["TAX_STATE"] is SourceAuthority.AUTHORITATIVE

    projected, _ = await _projected(uid, outcome.scenario_id)
    # Header plus one node per authoritative line item — no longer header-only.
    assert projected.graph.summary.nodes_by_type["TAX_STATE"] == (
        1 + len(persisted))
    assert _line_items(projected.graph)


@pytest.mark.asyncio
async def test_the_baseline_is_not_borrowed_from_the_counterfactual():
    """A lever that changes tax must leave the two sides genuinely different.
    Loading the baseline from its own source is only a fix if it is its OWN."""
    uid, analysis_id = await _user_with_baseline_analysis()
    await _publish_rule()
    outcome = await _seal(uid, analysis_id, amount="15000")

    baseline, counterfactual = await _projected(uid, outcome.scenario_id)
    left, right = _line_items(baseline.graph), _line_items(counterfactual.graph)

    assert left and right
    assert left != right, "the two sides carry identical tax state"
    # The deduction the lever moved differs; the income it did not touch does not.
    assert left[("income", "Total income")] == right[("income", "Total income")]
    assert left[("deduction", "Total deductions")] != (
        right[("deduction", "Total deductions")])


@pytest.mark.asyncio
async def test_the_baseline_tax_state_is_not_recomputed():
    """Loaded, never re-derived. The engine may not run on a customer read even
    though the rows it once produced are exactly what is being read."""
    import app.services.tax_engine.core.engine as engine_module

    uid, analysis_id = await _user_with_baseline_analysis()
    await _publish_rule()
    outcome = await _seal(uid, analysis_id)

    runs: list[int] = []
    real = engine_module.compute

    def counting(inp):
        runs.append(1)
        return real(inp)

    engine_module.compute = counting                    # type: ignore[assignment]
    try:
        baseline, _ = await _sides(uid, outcome.scenario_id)
    finally:
        engine_module.compute = real                    # type: ignore[assignment]

    assert runs == [], f"TaxEngineService ran {len(runs)} times"
    assert baseline.line_items, "and it still produced the baseline tax state"


# ===========================================================================
# §15 — the phantom-addition source defect
# ===========================================================================
@pytest.mark.asyncio
async def test_an_unchanged_tax_line_now_exists_on_both_sides():
    """THE DEFECT PROOF. `Total income` is untouched by an RRSP deduction lever.

    Before the fix the baseline carried no line items at all, so this item
    existed only on the counterfactual side and a comparator would have called
    it an addition. It must now be present on BOTH sides under one identity.

    No ADDED/UNCHANGED classification is performed here — that is the
    comparator's job and it does not exist yet. This proves only that the source
    graphs both contain the item.
    """
    uid, analysis_id = await _user_with_baseline_analysis()
    await _publish_rule()
    outcome = await _seal(uid, analysis_id)

    baseline, counterfactual = await _projected(uid, outcome.scenario_id)
    left, right = _line_items(baseline.graph), _line_items(counterfactual.graph)

    unchanged = ("income", "Total income")
    assert unchanged in left, "the baseline lost the item the lever never touched"
    assert unchanged in right
    assert left[unchanged] == right[unchanged], "same item, same sealed value"

    # And the identity is literally the same key on both sides, which is what a
    # comparator matches on.
    keys = {
        side: {n.key for n in side_graph.graph.nodes_of(NodeType.TAX_STATE)
               if n.source_kind == "ioe.scenario_result.line_items"}
        for side, side_graph in (("b", baseline), ("c", counterfactual))
    }
    assert keys["b"] & keys["c"], "the two sides share no line-item identity"


# ===========================================================================
# §11 / §12 — authoritative-empty versus missing-authority
# ===========================================================================
@pytest.mark.asyncio
async def test_baseline_opportunity_is_missing_authority_not_an_empty_set():
    """THE RECORDED BLOCKER. No frozen source for the baseline's opportunities
    exists, so the absence is reported as absence — never as "this scenario had
    none", which is a statement about the user's tax position."""
    uid, analysis_id = await _user_with_baseline_analysis()
    await _hold(uid)
    await _publish_rule()
    outcome = await _seal(uid, analysis_id)

    baseline, counterfactual = await _sides(uid, outcome.scenario_id)

    assert baseline.candidates == ()
    assert baseline.authority["OPPORTUNITY"] is SourceAuthority.MISSING_AUTHORITY
    assert baseline.authority["OPPORTUNITY"] is not (
        SourceAuthority.AUTHORITATIVE_EMPTY)
    # The counterfactual's opportunities WERE loaded, from the seal.
    assert counterfactual.authority["OPPORTUNITY"] is SourceAuthority.AUTHORITATIVE


@pytest.mark.asyncio
async def test_authoritative_empty_and_missing_authority_are_distinguishable():
    """A scenario with no assumptions has AUTHORITATIVE_EMPTY assumptions — a
    real answer from a source that was read. It must not look like the
    baseline's unread opportunities."""
    uid, analysis_id = await _user_with_baseline_analysis()
    await _publish_rule()
    outcome = await _seal(uid, analysis_id)

    baseline, _ = await _sides(uid, outcome.scenario_id)

    assert baseline.authority["ASSUMPTION"] is SourceAuthority.AUTHORITATIVE_EMPTY
    assert baseline.authority["OPPORTUNITY"] is SourceAuthority.MISSING_AUTHORITY
    assert baseline.authority["ASSUMPTION"] != baseline.authority["OPPORTUNITY"]
    # Both render as zero nodes; only the authority tells them apart.
    projected, _ = await _projected(uid, outcome.scenario_id)
    assert projected.graph.summary.nodes_by_type["ASSUMPTION"] == 0
    assert projected.graph.summary.nodes_by_type["OPPORTUNITY"] == 0


@pytest.mark.asyncio
async def test_resource_is_inapplicable_rather_than_missing():
    uid, analysis_id = await _user_with_baseline_analysis()
    await _publish_rule()
    outcome = await _seal(uid, analysis_id)

    baseline, counterfactual = await _sides(uid, outcome.scenario_id)
    for bundle in (baseline, counterfactual):
        assert bundle.authority["RESOURCE"] is SourceAuthority.NOT_APPLICABLE
        assert "RESOURCE" not in COMPARISON_REQUIRED_FAMILIES
        assert "RESOURCE" not in bundle.missing_authority()


@pytest.mark.asyncio
async def test_every_required_family_carries_an_authority_verdict():
    uid, analysis_id = await _user_with_baseline_analysis()
    await _publish_rule()
    outcome = await _seal(uid, analysis_id)

    for bundle in await _sides(uid, outcome.scenario_id):
        for family in COMPARISON_REQUIRED_FAMILIES:
            assert family in bundle.authority, family
            assert isinstance(bundle.authority[family], SourceAuthority)


# ===========================================================================
# §13 — failure semantics
# ===========================================================================
@pytest.mark.asyncio
async def test_a_side_with_missing_authority_is_refused_for_comparison():
    """Fails closed through the existing taxonomy, and NOT as a mismatch:
    absence of a source is a known gap in what was sealed, not evidence that
    anything was tampered with."""
    uid, analysis_id = await _user_with_baseline_analysis()
    await _publish_rule()
    outcome = await _seal(uid, analysis_id)

    baseline, counterfactual = await _sides(uid, outcome.scenario_id)

    with pytest.raises(DependencyUnavailable) as caught:
        assert_comparison_ready(baseline)
    assert caught.value.reason is IntegrityReason.SEALED_EVIDENCE_INCOMPLETE
    assert "OPPORTUNITY" in baseline.missing_authority()

    # The counterfactual side is complete and is accepted.
    assert counterfactual.missing_authority() == ()
    assert_comparison_ready(counterfactual)


@pytest.mark.asyncio
async def test_rendering_a_side_still_works_while_comparison_is_refused():
    """Reading what was sealed stays legal with a family missing; COMPARING two
    sides does not. Collapsing the two would have made the §17 historical read
    fail for every scenario in the system."""
    uid, analysis_id = await _user_with_baseline_analysis()
    await _hold(uid)
    await _publish_rule()
    outcome = await _seal(uid, analysis_id)

    baseline, _ = await _projected(uid, outcome.scenario_id)
    assert baseline.graph.nodes_of(NodeType.FACT)
    assert baseline.graph.nodes_of(NodeType.TAX_STATE)


@pytest.mark.asyncio
async def test_a_purged_baseline_analysis_is_missing_authority_not_empty():
    """The account-deletion workflow removes analysis line items while
    Decision B retains the sealed scenario. "The run had no line items" and "the
    run is no longer there" must not collapse into one answer."""
    uid, analysis_id = await _user_with_baseline_analysis()
    await _publish_rule()
    outcome = await _seal(uid, analysis_id)

    before, _ = await _sides(uid, outcome.scenario_id)
    assert before.authority["TAX_STATE"] is SourceAuthority.AUTHORITATIVE

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.get(AnalysisRun, analysis_id)
        run.status = "failed"          # no longer a completed, sealed baseline

    after, _ = await _sides(uid, outcome.scenario_id)
    assert after.line_items == ()
    assert after.authority["TAX_STATE"] is SourceAuthority.MISSING_AUTHORITY
    with pytest.raises(DependencyUnavailable):
        assert_comparison_ready(after)


# ===========================================================================
# §7 — pinned rule versions
# ===========================================================================
@pytest.mark.asyncio
async def test_a_rule_published_after_the_seal_does_not_reach_the_baseline():
    """R1 → R2. The historical sides stay on R1, and no resolver runs."""
    import app.services.tax_engine.rules_service as rules_module

    uid, analysis_id = await _user_with_baseline_analysis()
    await _hold(uid)
    r1 = await _publish_rule("R1")
    outcome = await _seal(uid, analysis_id)

    before_baseline, before_counterfactual = await _projected(
        uid, outcome.scenario_id)
    pinned = {
        n.attributes.get("tax_rule_version_id")
        for n in before_counterfactual.graph.nodes_of(NodeType.OPPORTUNITY)
    }
    assert str(r1) in pinned, "the seal did not pin R1"

    r2 = await _publish_rule("R2")

    evaluations: list[int] = []
    real_evaluate = rules_module.RulesEvaluatorService.evaluate

    async def counting(self, *a, **kw):
        evaluations.append(1)
        return await real_evaluate(self, *a, **kw)

    rules_module.RulesEvaluatorService.evaluate = counting  # type: ignore[method-assign]
    try:
        after_baseline, after_counterfactual = await _projected(
            uid, outcome.scenario_id)
    finally:
        rules_module.RulesEvaluatorService.evaluate = real_evaluate  # type: ignore[method-assign]

    assert evaluations == [], "a rule evaluation ran on a historical read"
    assert after_baseline.graph.graph_hash == before_baseline.graph.graph_hash
    assert after_counterfactual.graph.graph_hash == (
        before_counterfactual.graph.graph_hash)
    after_pinned = {
        n.attributes.get("tax_rule_version_id")
        for n in after_counterfactual.graph.nodes_of(NodeType.OPPORTUNITY)
    }
    assert str(r2) not in after_pinned, "R2 silently replaced R1"
    assert after_pinned == pinned


# ===========================================================================
# §14 / §16 — the complete read, and no live fallback
# ===========================================================================
@pytest.mark.asyncio
async def test_the_corrected_read_runs_no_engine_evaluator_or_optimizer():
    import app.services.ioe.portfolio.eligibility as eligibility
    import app.services.ioe.portfolio.service as portfolio
    import app.services.tax_engine.core.engine as engine_module
    import app.services.tax_engine.rules_service as rules_module

    uid, analysis_id = await _user_with_baseline_analysis()
    await _hold(uid)
    await _publish_rule()
    outcome = await _seal(uid, analysis_id)

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

    for module in (engine_module, portfolio, eligibility):
        if getattr(module, "compute", None) is not None:
            module.compute = counting_compute          # type: ignore[assignment]
    rules_module.RulesEvaluatorService.evaluate = counting_evaluate  # type: ignore[method-assign]
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        baseline, counterfactual = await _projected(uid, outcome.scenario_id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
        for module in (engine_module, portfolio, eligibility):
            if getattr(module, "compute", None) is not None:
                module.compute = real_compute          # type: ignore[assignment]
        rules_module.RulesEvaluatorService.evaluate = real_evaluate  # type: ignore[method-assign]

    assert engine_runs == []
    assert evaluations == []
    assert [x for x in statements if "docs.document" in x] == []
    assert [x for x in statements
            if "optimization_run" in x or "optimization_candidate" in x] == []
    assert [x for x in statements if "reco.recommendation" in x] == []
    assert baseline.graph.nodes_of(NodeType.TAX_STATE)
    assert counterfactual.graph.nodes_of(NodeType.OPPORTUNITY)


@pytest.mark.asyncio
async def test_the_corrected_baseline_does_not_move_when_live_state_moves():
    """§16. Seal, then churn current financials, current rules and current
    documents. The baseline tax state now has real content to drift, and must
    not."""
    uid, analysis_id = await _user_with_baseline_analysis()
    await _hold(uid)
    await _publish_rule()
    outcome = await _seal(uid, analysis_id)

    before_baseline, before_counterfactual = await _projected(
        uid, outcome.scenario_id)
    before_items = _line_items(before_baseline.graph)
    assert before_items, "nothing to prove without baseline content"

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        row = await s.scalar(
            select(IncomeSource).where(IncomeSource.user_id == uid))
        row.amount = Decimal("250000")
    await _publish_rule("post-seal rule")
    await _hold(uid, "RRSP")

    after_baseline, after_counterfactual = await _projected(
        uid, outcome.scenario_id)

    assert _line_items(after_baseline.graph) == before_items
    assert after_baseline.graph.graph_hash == before_baseline.graph.graph_hash
    assert after_counterfactual.graph.graph_hash == (
        before_counterfactual.graph.graph_hash)
    assert after_baseline.node_applicability == before_baseline.node_applicability


# ===========================================================================
# §19 — tenant isolation on the newly consumed surface
# ===========================================================================
@pytest.mark.asyncio
async def test_another_tenant_cannot_read_this_baseline():
    """`analysis.analysis_line_item` is newly consumed by this path. A second
    tenant must not reach it, and must not learn the scenario exists."""
    uid, analysis_id = await _user_with_baseline_analysis()
    await _publish_rule()
    outcome = await _seal(uid, analysis_id)

    other, _ = await _user_with_baseline_analysis()

    with pytest.raises(DependencyUnavailable) as caught:
        async with unit_of_work(user_id=other, actor_type="user") as s:
            await hs.load_sealed_sides(s, other, outcome.scenario_id)
    # The same verdict a genuinely absent scenario produces — no existence
    # oracle that distinguishes "not yours" from "not there".
    assert caught.value.reason is IntegrityReason.SEALED_EVIDENCE_INCOMPLETE

    with pytest.raises(DependencyUnavailable) as absent:
        async with unit_of_work(user_id=other, actor_type="user") as s:
            await hs.load_sealed_sides(s, other, uuid.uuid4())
    assert absent.value.reason is caught.value.reason

    async with unit_of_work(user_id=other, actor_type="user") as s:
        leaked = list(await s.scalars(select(AnalysisLineItem).where(
            AnalysisLineItem.analysis_id == analysis_id)))
    assert leaked == [], "another tenant read this tenant's baseline detail"


# ===========================================================================
# §20 — measured cost
# ===========================================================================
@pytest.mark.asyncio
async def test_the_corrected_preparation_cost_is_measured_and_bounded():
    uid, analysis_id = await _user_with_baseline_analysis()
    await _hold(uid)
    await _publish_rule()
    outcome = await _seal(uid, analysis_id)

    statements: list[str] = []

    def record(conn, cur, statement, parameters, context, executemany):
        statements.append(statement.lower())

    from app.database.session import engine

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        start = time.perf_counter()
        baseline_bundle, counterfactual_bundle = await _sides(
            uid, outcome.scenario_id)
        load_ms = (time.perf_counter() - start) * 1000
        load_statements = len(statements)

        start = time.perf_counter()
        graphs = [assemble_historical_graph(b, user_id=uid)
                  for b in (baseline_bundle, counterfactual_bundle)]
        assemble_ms = (time.perf_counter() - start) * 1000

        start = time.perf_counter()
        projected = [project_scenario_comparable_graph(g) for g in graphs]
        project_ms = (time.perf_counter() - start) * 1000
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    print("\n12B corrected historical preparation:")               # noqa: T201
    print(f"  load       : {load_ms:.2f} ms, "                     # noqa: T201
          f"{load_statements} statements")
    print(f"  assembly   : {assemble_ms:.2f} ms")                  # noqa: T201
    print(f"  projection : {project_ms:.2f} ms")                   # noqa: T201
    print(f"  baseline       TAX_STATE={projected[0].graph.summary.nodes_by_type['TAX_STATE']}"  # noqa: T201
          f" OPPORTUNITY={projected[0].graph.summary.nodes_by_type['OPPORTUNITY']}")
    print(f"  counterfactual TAX_STATE={projected[1].graph.summary.nodes_by_type['TAX_STATE']}"  # noqa: T201
          f" OPPORTUNITY={projected[1].graph.summary.nodes_by_type['OPPORTUNITY']}")

    assert len(statements) == load_statements, "assembly or projection queried"
    # One bounded read per family — never one per line item.
    assert load_statements <= 25, load_statements
    baseline_items = projected[0].graph.summary.nodes_by_type["TAX_STATE"] - 1
    assert baseline_items >= 6, baseline_items
    assert load_statements < baseline_items * 2, (
        "statement count is growing with line-item count")
