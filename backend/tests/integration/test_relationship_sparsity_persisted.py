"""Sparse relationship derivation, measured on persisted rows — blocker 1.

The unit tests prove the derivation is sparse. These prove the rows that
actually reach the database are, on a real orchestrator run against the real
tax engine: the acceptance target is a property of stored evidence, not of a
pure function called in isolation.
"""
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
    IncomeSource,
    IncomeType,
    Jurisdiction,
    OptimizationCandidate,
    RecommendationRelationship,
    RuleAction,
    RuleOutcome,
    RuleSharedResource,
    TaxProfile,
    TaxRule,
    TaxRuleVersion,
    UserAccount,
)
from app.database.session import unit_of_work
from app.services.ioe.domain import relationships as rel
from app.services.ioe.domain.enums import DerivationSource
from app.services.ioe.orchestrator import OptimizationOrchestrator

VALID_SOURCES = {s.value for s in DerivationSource}


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


def _suffix() -> str:
    return uuid.uuid4().hex[:8].upper()


async def _user_with_income() -> tuple[uuid.UUID, uuid.UUID]:
    async with unit_of_work(actor_type="system") as s:
        user = UserAccount(email=f"rels_{uuid.uuid4().hex[:8]}@test.ca", status="active")
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


async def _publish(code: str, *, lever_code: str, amount: str,
                   shared_resource_code: str | None = None) -> uuid.UUID:
    async with unit_of_work(actor_type="admin") as s:
        jur = await s.scalar(select(Jurisdiction).where(Jurisdiction.code == "FED"))
        rule = TaxRule(code=code, name=f"{code} rule", category="deduction",
                       jurisdiction_id=jur.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=2025, effective_date=date(2025, 1, 1),
            status="published", description="relationship density fixture",
            eligibility_basis_codes=["BASIS_RELS"],
        )
        s.add(version)
        await s.flush()
        s.add(RuleOutcome(
            rule_version_id=version.id, outcome_type="recommend", priority=1,
            title_template=f"{code} opportunity",
            economic_effect_type="current_year_tax_reduction",
            reversibility="reversible", portfolio_lever_code=lever_code,
            lever_parameters={"amount": "action.cost_amount"},
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


async def _dense_run() -> tuple[uuid.UUID, uuid.UUID]:
    """The worst case: many candidates on one pool, all writing one engine field.

    Under the pairwise derivation this is the population that produced 15.9
    relationship rows per candidate.
    """
    uid, analysis_id = await _user_with_income()
    tag = _suffix()
    for i in range(24):
        await _publish(
            f"RELS{tag}_{i:02d}", lever_code="INCREASE_RRSP_DEDUCTION",
            amount=str(400 + i * 25), shared_resource_code="RRSP_ROOM",
        )
    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)
    return uid, outcome.run_id


async def _counts(uid, run_id) -> tuple[int, int]:
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        candidates = await s.scalar(
            select(func.count()).select_from(OptimizationCandidate)
            .where(OptimizationCandidate.run_id == run_id)
        )
        edges = await s.scalar(
            select(func.count()).select_from(RecommendationRelationship)
            .where(RecommendationRelationship.run_id == run_id)
        )
        return candidates, edges


@pytest.mark.asyncio
async def test_persisted_relationship_rows_stay_within_four_per_candidate():
    uid, run_id = await _dense_run()
    candidates, edges = await _counts(uid, run_id)
    assert candidates >= 24, "fixture did not produce a dense enough population"
    assert edges <= rel.MAX_EDGES_PER_CANDIDATE * candidates, (
        f"{edges} relationship rows for {candidates} candidates "
        f"({edges / candidates:.1f} per candidate)"
    )


@pytest.mark.asyncio
async def test_every_persisted_edge_records_a_valid_derivation_source():
    uid, run_id = await _dense_run()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        rows = list(await s.scalars(
            select(RecommendationRelationship)
            .where(RecommendationRelationship.run_id == run_id)
        ))
    assert rows
    assert all(r.derivation_source in VALID_SOURCES for r in rows)
    # a pool edge is provenance-tagged as such, never as a rules contract fact
    pooled = [r for r in rows if r.shared_resource_code is not None]
    assert pooled
    assert all(r.derivation_source == "shared_resource" for r in pooled)


@pytest.mark.asyncio
async def test_the_database_refuses_an_unknown_derivation_source():
    """The CHECK constraint is the backstop for a future writer that invents a
    provenance value the domain does not define."""
    uid, run_id = await _dense_run()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        row = await s.scalar(
            select(RecommendationRelationship)
            .where(RecommendationRelationship.run_id == run_id).limit(1)
        )
        assert row is not None
        source_id, target_id = row.source_candidate_id, row.target_candidate_id

    with pytest.raises(Exception, match="ck_ioe_relationship_derivation_source"):
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            await s.execute(
                text(
                    "INSERT INTO ioe.recommendation_relationship "
                    "(run_id, source_candidate_id, target_candidate_id, "
                    " relationship_type, explanation_code, derivation_source) "
                    "VALUES (:run, :src, :tgt, 'overlaps', 'SAME_ENGINE_INPUT', 'guessed')"
                ),
                {"run": run_id, "src": source_id, "tgt": target_id},
            )


@pytest.mark.asyncio
async def test_a_symmetric_relationship_is_stored_once_per_pair_and_resource():
    uid, run_id = await _dense_run()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        rows = list(await s.scalars(
            select(RecommendationRelationship)
            .where(RecommendationRelationship.run_id == run_id)
        ))
    seen = set()
    for row in rows:
        if row.relationship_type not in {t.value for t in rel.SYMMETRIC_TYPES}:
            continue
        pair = tuple(sorted((str(row.source_candidate_id), str(row.target_candidate_id))))
        key = (*pair, row.relationship_type, row.shared_resource_code or "")
        assert key not in seen, f"duplicate symmetric edge {key}"
        seen.add(key)
    assert seen


@pytest.mark.asyncio
async def test_edge_density_does_not_grow_with_the_candidate_population():
    """Two runs of different sizes: the per-candidate edge ratio must not rise.

    A quadratic derivation shows a ratio that climbs with n; a grouped one holds
    roughly flat.
    """
    uid_small, analysis_small = await _user_with_income()
    tag = _suffix()
    for i in range(6):
        await _publish(f"RELSA{tag}_{i:02d}", lever_code="INCREASE_RRSP_DEDUCTION",
                       amount=str(500 + i * 25), shared_resource_code="RRSP_ROOM")
    small = await OptimizationOrchestrator(uid_small).generate(analysis_small)
    small_candidates, small_edges = await _counts(uid_small, small.run_id)

    uid_large, analysis_large = await _user_with_income()
    for i in range(30):
        await _publish(f"RELSB{tag}_{i:02d}", lever_code="INCREASE_RRSP_DEDUCTION",
                       amount=str(500 + i * 25), shared_resource_code="RRSP_ROOM")
    large = await OptimizationOrchestrator(uid_large).generate(analysis_large)
    large_candidates, large_edges = await _counts(uid_large, large.run_id)

    assert large_candidates > small_candidates
    small_ratio = small_edges / small_candidates
    large_ratio = large_edges / large_candidates
    assert large_ratio <= small_ratio + 0.5, (
        f"density grew from {small_ratio:.2f} to {large_ratio:.2f} per candidate"
    )
    assert large_ratio <= rel.MAX_EDGES_PER_CANDIDATE
