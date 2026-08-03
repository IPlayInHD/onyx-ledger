"""Batched TX-2 persistence — release blocker 2.

Every IOE result child used to be written one row per statement, so a single
optimization run issued 1,884 SQL statements at 75 candidates and the count grew
with the candidate population. These tests hold the batched implementation to
the acceptance targets and, just as importantly, to the guarantees batching must
not cost: atomicity, immutability, RLS, and complete evidence.

Statement counts come from `before_cursor_execute`, which fires for what is
actually sent to the driver — not from an estimate.
"""
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select, text

from app.database.base import uuid7
from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
    CalcFormula,
    CalcFormulaInput,
    CandidateCost,
    CandidateEconomicEffect,
    ConfidenceComponent,
    IncomeSource,
    IncomeType,
    Jurisdiction,
    OptimizationCandidate,
    OptimizationRun,
    PortfolioEvaluationStep,
    PortfolioExclusion,
    PortfolioMember,
    RecommendationRelationship,
    RuleAction,
    RuleOutcome,
    RuleSharedResource,
    ScoreComponent,
    StrategyPortfolio,
    TaxProfile,
    TaxRule,
    TaxRuleVersion,
    UserAccount,
)
from app.database.session import engine, unit_of_work
from app.services.ioe.orchestrator import OptimizationOrchestrator

# The gate's targets, restated here so a regression names the number it broke.
MAX_STATEMENTS_PER_RUN = 60
MAX_STATEMENTS_PER_CANDIDATE_AT_100 = 1.0


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    await engine.dispose()


class _Counter:
    """Statements sent to the driver, split the way the gate asks for them."""

    def __init__(self) -> None:
        self.total = 0
        self.reads = 0
        self.writes = 0
        self.other = 0
        self.by_table: dict[str, int] = {}

    def record(self, statement: str) -> None:
        body = " ".join(statement.split())
        verb = body.split(" ", 1)[0].upper()
        self.total += 1
        if verb == "SELECT":
            self.reads += 1
        elif verb in ("INSERT", "UPDATE", "DELETE"):
            self.writes += 1
        else:
            self.other += 1
        if verb == "INSERT":
            table = body.split("INSERT INTO ", 1)[-1].split(" ", 1)[0]
            self.by_table[table] = self.by_table.get(table, 0) + 1


@contextmanager
def counting():
    counter = _Counter()
    active = {"on": True}

    def _listen(conn, cursor, statement, parameters, context, executemany):
        if active["on"]:
            counter.record(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _listen)
    try:
        yield counter
    finally:
        active["on"] = False
        event.remove(engine.sync_engine, "before_cursor_execute", _listen)


def _suffix() -> str:
    return uuid.uuid4().hex[:8].upper()


async def _user_with_income() -> tuple[uuid.UUID, uuid.UUID]:
    async with unit_of_work(actor_type="system") as s:
        user = UserAccount(email=f"batch_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(user)
        await s.flush()
        uid = user.id
        income_type_id = (await s.scalar(
            select(IncomeType).where(IncomeType.code == "employment")
        )).id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(TaxProfile(user_id=uid, province_code="ON", marital_status="single"))
        s.add(IncomeSource(
            user_id=uid, tax_year=2025, income_type_id=income_type_id,
            amount=Decimal("140000"), province_code="ON",
        ))
        run = AnalysisRun(
            user_id=uid, tax_year=2025, province_code="ON", engine_version="py-1.0.0",
            status="completed", started_at=datetime.now(tz=UTC),
            completed_at=datetime.now(tz=UTC), data_verified=True,
        )
        s.add(run)
        await s.flush()
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot={"province": "ON"},
            snapshot_hash=f"snap-{uuid.uuid4().hex[:8]}",
        ))
        await s.flush()
        return uid, run.id


_LEVERS = [
    ("INCREASE_RRSP_DEDUCTION", "RRSP_ROOM"),
    ("INCREASE_FHSA_DEDUCTION", "FHSA_ROOM"),
    ("INCREASE_DONATIONS", None),
    ("INCREASE_MEDICAL_EXPENSES", "MEDICAL_POOL"),
]


async def _impact_formula(code: str) -> uuid.UUID:
    """A rule that authors a calculated impact, so the run produces economic
    effects. A rule WITHOUT one produces none by design — the architecture
    refuses to synthesize an impact — and a benchmark built only from those
    would never write `candidate_economic_effect` at all.

    Literal inputs only: `fact_key` is a foreign key into the fact dictionary,
    and this fixture is about persistence volume, not fact resolution.
    """
    async with unit_of_work(actor_type="admin") as s:
        formula = CalcFormula(
            code=code, expression="base rate *", expression_lang="rpn",
            description="batching fixture impact",
        )
        s.add(formula)
        await s.flush()
        s.add(CalcFormulaInput(
            formula_id=formula.id, param_name="base",
            fact_key=None, literal_value=Decimal("5000"),
        ))
        s.add(CalcFormulaInput(
            formula_id=formula.id, param_name="rate",
            fact_key=None, literal_value=Decimal("0.02"),
        ))
        await s.flush()
        return formula.id


async def _publish(code: str, *, lever_code: str, amount: str,
                   shared_resource_code: str | None,
                   impact_formula_id: uuid.UUID | None = None) -> uuid.UUID:
    async with unit_of_work(actor_type="admin") as s:
        jur = await s.scalar(select(Jurisdiction).where(Jurisdiction.code == "FED"))
        rule = TaxRule(code=code, name=f"{code} rule", category="deduction",
                       jurisdiction_id=jur.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=2025, effective_date=date(2025, 1, 1),
            status="published", description="batching fixture",
            eligibility_basis_codes=["BASIS_BATCH"],
        )
        s.add(version)
        await s.flush()
        s.add(RuleOutcome(
            rule_version_id=version.id, outcome_type="recommend", priority=1,
            title_template=f"{code} opportunity",
            economic_effect_type="current_year_tax_reduction",
            reversibility="reversible", portfolio_lever_code=lever_code,
            lever_parameters={"amount": "action.cost_amount"},
            impact_formula_id=impact_formula_id,
        ))
        s.add(RuleAction(
            rule_version_id=version.id, action_code="CONTRIBUTE",
            description="Contribute", effort_rating=2,
            cost_type="liquidity_commitment", cost_amount=Decimal(amount),
        ))
        if shared_resource_code:
            s.add(RuleSharedResource(
                rule_version_id=version.id, resource_code=shared_resource_code,
            ))
        await s.flush()
        return version.id


async def _run_with(rule_count: int) -> tuple[uuid.UUID, uuid.UUID, _Counter, int]:
    """Publish `rule_count` rules, run one optimization, count its statements."""
    uid, analysis_id = await _user_with_income()
    tag = _suffix()
    formula_id = await _impact_formula(f"BATCHFX_{tag}")
    for i in range(rule_count):
        lever, resource = _LEVERS[i % len(_LEVERS)]
        await _publish(f"BATCH{tag}_{i:03d}", lever_code=lever,
                       amount=str(400 + i * 20), shared_resource_code=resource,
                       impact_formula_id=formula_id)

    with counting() as counter:
        outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        candidates = await s.scalar(
            select(func.count()).select_from(OptimizationCandidate)
            .where(OptimizationCandidate.run_id == outcome.run_id)
        )
    return uid, outcome.run_id, counter, candidates


# ---- acceptance targets -----------------------------------------------------
@pytest.mark.asyncio
async def test_a_run_stays_under_sixty_statements():
    _, _, counter, candidates = await _run_with(40)
    assert candidates >= 40
    assert counter.total <= MAX_STATEMENTS_PER_RUN, (
        f"{counter.total} statements for {candidates} candidates "
        f"(reads={counter.reads}, writes={counter.writes}, other={counter.other})"
    )


@pytest.mark.asyncio
async def test_statements_per_candidate_at_one_hundred_candidates():
    _, _, counter, candidates = await _run_with(100)
    assert candidates >= 100
    per_candidate = counter.total / candidates
    assert per_candidate <= MAX_STATEMENTS_PER_CANDIDATE_AT_100, (
        f"{per_candidate:.2f} statements per candidate at {candidates} candidates"
    )


@pytest.mark.asyncio
async def test_statement_count_does_not_grow_with_candidate_count():
    """The point of the whole item: cost is O(tables), not O(rows)."""
    _, _, small, small_candidates = await _run_with(5)
    _, _, large, large_candidates = await _run_with(100)

    # Published rules accumulate in a shared database, so the small run is only
    # "small" relative to the large one; what matters is that the population grew
    # substantially and the statement count did not follow it.
    assert large_candidates >= small_candidates + 100
    growth = abs(large.total - small.total) / small.total
    assert growth < 0.20, (
        f"{small.total} statements at {small_candidates} candidates → "
        f"{large.total} at {large_candidates} ({growth:.0%} growth)"
    )


@pytest.mark.asyncio
async def test_no_child_table_is_written_more_than_once_per_run():
    """One statement per table is the mechanism; anything else is a row loop
    that survived."""
    _, _, counter, _ = await _run_with(60)
    repeated = {
        table: count for table, count in counter.by_table.items()
        # the run's own status events are written at three distinct points in
        # the workflow and are not evidence children
        if count > 1 and not table.endswith("optimization_run_event")
    }
    assert repeated == {}, f"tables written more than once: {repeated}"


@pytest.mark.asyncio
async def test_read_and_write_counts_are_recorded_separately():
    """The gate asks for the split, so the split is asserted, not just the total."""
    _, _, counter, _ = await _run_with(40)
    assert counter.reads + counter.writes + counter.other == counter.total
    assert counter.reads > 0 and counter.writes > 0
    # reads must not scale with candidates either — they were the other N+1
    assert counter.reads <= 45, f"{counter.reads} reads"


# ---- what batching must not cost --------------------------------------------
@pytest.mark.asyncio
async def test_every_child_row_is_still_written():
    """Batching changes how rows are sent, never which rows exist."""
    uid, run_id, _, candidates = await _run_with(30)
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        portfolio = await s.scalar(
            select(StrategyPortfolio).where(StrategyPortfolio.run_id == run_id)
        )
        assert portfolio is not None

        async def count(model, *where):
            return await s.scalar(select(func.count()).select_from(model).where(*where))

        candidate_ids = [
            row.id for row in await s.scalars(
                select(OptimizationCandidate)
                .where(OptimizationCandidate.run_id == run_id)
            )
        ]
        assert len(candidate_ids) == candidates

        assert await count(ScoreComponent,
                           ScoreComponent.candidate_id.in_(candidate_ids)) > 0
        assert await count(ConfidenceComponent,
                           ConfidenceComponent.candidate_id.in_(candidate_ids)) > 0
        assert await count(CandidateCost,
                           CandidateCost.candidate_id.in_(candidate_ids)) > 0
        assert await count(CandidateEconomicEffect,
                           CandidateEconomicEffect.candidate_id.in_(candidate_ids)) > 0
        assert await count(RecommendationRelationship,
                           RecommendationRelationship.run_id == run_id) > 0
        assert await count(PortfolioEvaluationStep,
                           PortfolioEvaluationStep.portfolio_id == portfolio.id) > 0

        members = await count(PortfolioMember,
                              PortfolioMember.portfolio_id == portfolio.id)
        exclusions = await count(PortfolioExclusion,
                                 PortfolioExclusion.portfolio_id == portfolio.id)
        # every candidate is accounted for: selected, or excluded with a reason
        assert members + exclusions == candidates


@pytest.mark.asyncio
async def test_children_and_completion_still_commit_together():
    uid, run_id, _, candidates = await _run_with(20)
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.get(OptimizationRun, run_id)
        assert run.workflow_status == "completed"
        assert run.optimization_result_hash
        persisted = await s.scalar(
            select(func.count()).select_from(OptimizationCandidate)
            .where(OptimizationCandidate.run_id == run_id)
        )
    assert persisted == candidates


@pytest.mark.asyncio
async def test_a_failure_inside_tx2_leaves_no_evidence_at_all():
    """Atomicity is the guarantee batching most easily breaks.

    The portfolio write is forced to fail after the candidates have been
    batched. Nothing may survive: no candidates, no relationships, no portfolio,
    and the run must record a sanitized failure rather than completing.
    """
    uid, analysis_id = await _user_with_income()
    tag = _suffix()
    formula_id = await _impact_formula(f"FAILFX_{tag}")
    for i in range(10):
        lever, resource = _LEVERS[i % len(_LEVERS)]
        await _publish(f"FAIL{tag}_{i:02d}", lever_code=lever,
                       amount=str(500 + i * 20), shared_resource_code=resource,
                       impact_formula_id=formula_id)

    from app.services.ioe.portfolio import service as portfolio_service

    original = portfolio_service.PortfolioEvaluationService.persist

    async def exploding(self, session, run_id, portfolio, candidate_ids):
        raise RuntimeError("forced failure after candidate batch")

    portfolio_service.PortfolioEvaluationService.persist = exploding
    try:
        # TX-3 records the sanitized failure and the exception still propagates:
        # the caller is told the run produced no result.
        with pytest.raises(RuntimeError, match="forced failure"):
            await OptimizationOrchestrator(uid).generate(analysis_id)
    finally:
        portfolio_service.PortfolioEvaluationService.persist = original

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.scalar(
            select(OptimizationRun)
            .where(OptimizationRun.user_id == uid)
            .order_by(OptimizationRun.created_at.desc())
            .limit(1)
        )
        assert run.workflow_status == "failed"
        assert run.error_code == "PERSISTENCE_FAILED"
        assert run.optimization_result_hash is None
        for model, column in (
            (OptimizationCandidate, OptimizationCandidate.run_id),
            (RecommendationRelationship, RecommendationRelationship.run_id),
            (StrategyPortfolio, StrategyPortfolio.run_id),
        ):
            leftover = await s.scalar(
                select(func.count()).select_from(model).where(column == run.id)
            )
            assert leftover == 0, f"{model.__name__} rows survived a failed TX-2"


@pytest.mark.asyncio
async def test_batched_rows_are_still_immutable():
    """The append-only trigger fires per row, so it must still reject an UPDATE
    on a row that arrived in a batch."""
    uid, run_id, _, _ = await _run_with(10)
    with pytest.raises(Exception, match="immutable"):
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            await s.execute(
                text("UPDATE ioe.optimization_candidate SET opportunity_code = 'x' "
                     "WHERE run_id = :run"),
                {"run": run_id},
            )


@pytest.mark.asyncio
async def test_batched_rows_are_not_visible_to_another_tenant():
    """RLS is enforced per row by the database, so a batched INSERT is subject to
    exactly the same policy as a single one."""
    uid, run_id, _, _ = await _run_with(10)
    other_uid, _ = await _user_with_income()
    async with unit_of_work(user_id=other_uid, actor_type="user") as s:
        visible = await s.scalar(
            select(func.count()).select_from(OptimizationCandidate)
            .where(OptimizationCandidate.run_id == run_id)
        )
    assert visible == 0


@pytest.mark.asyncio
async def test_application_generated_ids_match_the_database_default_shape():
    """Batching supplies candidate ids so children need no per-row RETURNING.
    Those ids must be indistinguishable from `ref.uuid_generate_v7()` output."""
    async with unit_of_work(actor_type="system") as s:
        server_side = [
            uuid.UUID(str(await s.scalar(text("SELECT ref.uuid_generate_v7()"))))
            for _ in range(5)
        ]
    app_side = [uuid7() for _ in range(5)]

    for value in server_side + app_side:
        assert value.version == 7
        assert (value.bytes[8] & 0xC0) == 0x80          # RFC-4122 variant

    # both encode a 48-bit millisecond timestamp in the leading bytes, so ids
    # from either source interleave in the same order
    def millis(value: uuid.UUID) -> int:
        return int.from_bytes(value.bytes[:6], "big")

    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    for value in app_side + server_side:
        assert abs(millis(value) - now_ms) < 60_000

    assert len(set(app_side)) == len(app_side)


@pytest.mark.asyncio
async def test_bulk_insert_is_a_no_op_on_empty_input():
    """A run with no exclusions must not raise; SQLAlchemy rejects an empty
    parameter list, so the helper has to short-circuit."""
    from app.database.bulk import bulk_insert

    async with unit_of_work(actor_type="system") as s:
        assert await bulk_insert(s, ScoreComponent, []) == 0


@pytest.mark.asyncio
async def test_bulk_insert_chunks_below_the_bind_parameter_limit():
    """PostgreSQL caps a statement at 32,767 bound values. A large batch must be
    split rather than failing at the protocol level.

    `score_component` is unique on (candidate_id, factor_code), so the rows are
    spread across a synthetic factor code per row — the shape under test is the
    chunking arithmetic, not the score model.
    """
    from app.database.bulk import MAX_BIND_PARAMS, bulk_insert

    uid, run_id, _, _ = await _run_with(5)
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        candidate_id = (await s.scalar(
            select(OptimizationCandidate)
            .where(OptimizationCandidate.run_id == run_id).limit(1)
        )).id

    columns = 6                                   # the score-component row shape
    total = MAX_BIND_PARAMS // columns + 500
    rows = [
        {
            "candidate_id": candidate_id, "factor_code": f"SYNTH_{i:06d}",
            "raw_value": Decimal("1"), "normalized_value": Decimal("0.5"),
            "weight": Decimal("0.1"), "contribution": Decimal("0.05"),
        }
        for i in range(total)
    ]

    with counting() as counter:
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            written = await bulk_insert(s, ScoreComponent, rows)

    assert written == total
    inserts = counter.by_table.get("ioe.score_component", 0)
    assert inserts == 2, f"expected exactly two chunks, got {inserts}"


@pytest.mark.asyncio
async def test_bulk_insert_normalizes_rows_to_one_column_list():
    """A key missing from one row becomes an explicit NULL. Without this the row
    would land on a different column list and split the batch."""
    from app.database.bulk import bulk_insert

    uid, run_id, _, _ = await _run_with(5)
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        candidate_id = (await s.scalar(
            select(OptimizationCandidate)
            .where(OptimizationCandidate.run_id == run_id).limit(1)
        )).id

    with counting() as counter:
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            await bulk_insert(s, ConfidenceComponent, [
                {"candidate_id": candidate_id, "factor_code": "SYNTH_A",
                 "value": Decimal("1"), "weight": Decimal("1"),
                 "contribution": Decimal("1"), "reason_code": "R"},
                # no reason_code key at all
                {"candidate_id": candidate_id, "factor_code": "SYNTH_B",
                 "value": Decimal("1"), "weight": Decimal("1"),
                 "contribution": Decimal("1")},
                # reason_code present but null
                {"candidate_id": candidate_id, "factor_code": "SYNTH_C",
                 "value": Decimal("1"), "weight": Decimal("1"),
                 "contribution": Decimal("1"), "reason_code": None},
            ])

    assert counter.by_table.get("ioe.confidence_component", 0) == 1
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        stored = {
            row.factor_code: row.reason_code for row in await s.scalars(
                select(ConfidenceComponent)
                .where(ConfidenceComponent.candidate_id == candidate_id)
                .where(ConfidenceComponent.factor_code.in_(
                    ["SYNTH_A", "SYNTH_B", "SYNTH_C"]))
            )
        }
    assert stored == {"SYNTH_A": "R", "SYNTH_B": None, "SYNTH_C": None}


@pytest.mark.asyncio
async def test_children_reference_the_candidate_ids_that_were_stored():
    """The id mapping is the part batching could silently corrupt: a child could
    point at a candidate that is not the one it was computed from."""
    uid, run_id, _, _ = await _run_with(25)
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        candidate_ids = {
            row.id for row in await s.scalars(
                select(OptimizationCandidate)
                .where(OptimizationCandidate.run_id == run_id)
            )
        }
        edges = list(await s.scalars(
            select(RecommendationRelationship)
            .where(RecommendationRelationship.run_id == run_id)
        ))
        assert edges
        for edge in edges:
            assert edge.source_candidate_id in candidate_ids
            assert edge.target_candidate_id in candidate_ids

        portfolio = await s.scalar(
            select(StrategyPortfolio).where(StrategyPortfolio.run_id == run_id)
        )
        members = list(await s.scalars(
            select(PortfolioMember).where(PortfolioMember.portfolio_id == portfolio.id)
        ))
        for member in members:
            assert member.candidate_id in candidate_ids

        for model, column in (
            (ScoreComponent, ScoreComponent.candidate_id),
            (ConfidenceComponent, ConfidenceComponent.candidate_id),
            (CandidateCost, CandidateCost.candidate_id),
            (CandidateEconomicEffect, CandidateEconomicEffect.candidate_id),
        ):
            attached = await s.scalar(
                select(func.count()).select_from(model)
                .where(column.in_(candidate_ids))
            )
            assert attached > 0, f"{model.__name__} rows lost their candidate"
