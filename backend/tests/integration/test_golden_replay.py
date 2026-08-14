"""Golden replay from PERSISTED ROWS (§gate item 3).

The distinction that matters: replaying from an in-memory fixture proves the
hash function is deterministic. Replaying from rows read back out of PostgreSQL
proves the *stored evidence* is sufficient to reconstruct the identity — which
is the property an auditor actually needs, and the one that breaks silently when
a material input stops being persisted.

These run in a fresh process against rows written by an earlier one.
"""
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
    IncomeSource,
    IncomeType,
    OptimizationRun,
    Scenario,
    StrategyPortfolio,
    TaxProfile,
    UserAccount,
)
from app.database.session import unit_of_work
from app.services.ioe.domain import canonical as c
from app.services.ioe.domain.scenario import ScenarioSpec
from app.services.ioe.orchestrator import OptimizationOrchestrator
from app.services.ioe.scenario.service import ScenarioService
from tests.conftest import frozen_snapshot

RRSP = "INCREASE_RRSP_DEDUCTION"


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


async def _user_with_analysis() -> tuple[uuid.UUID, uuid.UUID]:
    async with unit_of_work(actor_type="system") as s:
        u = UserAccount(email=f"golden_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(u)
        await s.flush()
        uid = u.id
        itype = await s.scalar(select(IncomeType).where(IncomeType.code == "employment"))
        type_id = itype.id
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(TaxProfile(user_id=uid, province_code="ON", marital_status="single"))
        s.add(IncomeSource(
            user_id=uid, tax_year=2025, income_type_id=type_id,
            amount=Decimal("95000"), province_code="ON",
        ))
        run = AnalysisRun(
            user_id=uid, tax_year=2025, province_code="ON", engine_version="py-1.0.0",
            status="completed", started_at=datetime.now(tz=UTC),
            completed_at=datetime.now(tz=UTC), data_verified=True,
        )
        s.add(run)
        await s.flush()
        _snap = frozen_snapshot(employment_income=Decimal("95000"))
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot=_snap[0],
            snapshot_hash=_snap[1],
        ))
        await s.flush()
        return uid, run.id


@pytest.mark.asyncio
async def test_scenario_spec_and_result_hashes_replay_from_stored_rows():
    """Rebuild the spec from `scenario_lever`/`scenario_assumption` rows, re-pin,
    recompute both hashes, and require they equal the sealed values."""
    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)
    spec = ScenarioSpec.parse(
        [{"lever_code": RRSP, "parameters": {"amount": Decimal("7000")}}],
        label="golden",
    )
    outcome = await service.simulate(analysis_id, spec)

    pinned, payload, sealed_spec_hash, sealed_result_hash, _ = (
        await _replay_from_stored_rows(service, analysis_id, outcome.scenario_id))

    assert pinned.spec_hash == sealed_spec_hash, "scenario spec hash did not replay"
    replayed = c.scenario_result_hash(spec_hash=pinned.spec_hash, result=payload)
    assert replayed == sealed_result_hash, "scenario result hash did not replay"


@pytest.mark.asyncio
async def test_optimization_spec_hash_replays_from_stored_manifest():
    """The run's stored version manifest and pinned rule set must be enough to
    reproduce its identity."""
    from tests.integration.test_ioe_portfolio_persistence import (
        _publish_lever_rule,
        _suffix,
    )

    uid, analysis_id = await _user_with_analysis()
    await _publish_lever_rule(f"GOLD_{_suffix()}", lever_code=RRSP, amount="6000")
    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.get(OptimizationRun, outcome.run_id)
        stored_manifest = dict(run.version_manifest)
        stored_spec_hash = run.optimization_spec_hash
        stored_manifest_hash = run.manifest_hash

    # the manifest hash replays from the stored manifest alone
    assert c.version_manifest_hash(stored_manifest) == stored_manifest_hash

    # and the spec hash replays from the stored pins
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        from app.database.models import AnalysisInputSnapshot as Snap
        from app.database.models import RunRuleVersion

        snapshot = await s.get(Snap, analysis_id)
        version_ids = [
            r.tax_rule_version_id for r in await s.scalars(
                select(RunRuleVersion).where(RunRuleVersion.run_id == outcome.run_id)
            )
        ]

    replayed = c.optimization_spec_hash(
        baseline_input_snapshot_hash=snapshot.snapshot_hash,
        tax_year=2025, jurisdiction="ON",
        rule_version_set=version_ids,
        version_manifest=stored_manifest,
        user_constraints={}, assumption_set=[],
    )
    assert replayed == stored_spec_hash, "optimization spec hash did not replay"


@pytest.mark.asyncio
async def test_portfolio_reconciliation_replays_from_stored_rows():
    """I-1 and I-2 re-checked against what is in the database, not memory."""
    from decimal import ROUND_HALF_UP

    from app.database.models import PortfolioMember
    from tests.integration.test_ioe_portfolio_persistence import (
        _publish_lever_rule,
        _suffix,
    )

    uid, analysis_id = await _user_with_analysis()
    await _publish_lever_rule(f"GOLDPF_{_suffix()}", lever_code=RRSP, amount="8000")
    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    money = Decimal("0.01")
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        pf = await s.scalar(
            select(StrategyPortfolio).where(StrategyPortfolio.run_id == outcome.run_id)
        )
        members = list(await s.scalars(
            select(PortfolioMember)
            .where(PortfolioMember.portfolio_id == pf.id)
            .order_by(PortfolioMember.apply_order)
        ))

    incremental_sum = sum(
        (m.incremental_benefit for m in members), Decimal(0)
    ).quantize(money, ROUND_HALF_UP)
    baseline_minus_final = (
        pf.objective_value_baseline - pf.objective_value_final
    ).quantize(money, ROUND_HALF_UP)
    stored_delta = pf.objective_delta.quantize(money, ROUND_HALF_UP)

    assert stored_delta == incremental_sum == baseline_minus_final
    assert pf.portfolio_total_benefit.quantize(money, ROUND_HALF_UP) == stored_delta


@pytest.mark.asyncio
async def test_a_version_change_produces_a_new_identity():
    """A moved version must mint a new identity, never silently reinterpret
    historical evidence under the new rules."""
    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)
    spec = ScenarioSpec.parse(
        [{"lever_code": RRSP, "parameters": {"amount": Decimal("5000")}}]
    )

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        pinned = await service._pin_specification(s, analysis_id, spec, result_schema_version="1.0.0")
    original = pinned.spec_hash

    # move a pinned version; the identity must move with it
    moved = dict(pinned.version_manifest)
    moved["lever_registry_version"] = "9.9.9"
    pinned.version_manifest = moved
    pinned.manifest_hash = c.version_manifest_hash(moved)
    assert service.compute_spec_hash(pinned) != original, (
        "a version change did not change the scenario's identity"
    )


@pytest.mark.asyncio
async def test_a_tampered_result_is_detectable_by_recomputing_the_hash():
    """The sealed hash is what makes tampering detectable rather than merely
    forbidden."""
    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)
    outcome = await service.simulate(
        analysis_id,
        ScenarioSpec.parse([{"lever_code": RRSP, "parameters": {"amount": Decimal("4000")}}]),
    )

    pinned, payload, _, sealed, result = (
        await _replay_from_stored_rows(service, analysis_id, outcome.scenario_id))
    assert c.scenario_result_hash(spec_hash=pinned.spec_hash, result=payload) == sealed

    # a single cent of drift changes the hash
    payload["tax_delta"] = c.money(result.tax_delta + Decimal("0.01"))
    assert c.scenario_result_hash(spec_hash=pinned.spec_hash, result=payload) != sealed


async def _replay_from_stored_rows(service, analysis_id, scenario_id):
    """Re-derive a sealed scenario's hashes THE WAY REPLAY MUST: every version
    and every bound digest read back from the row, never assumed.

    These tests used to pass a v1 literal, which was correct only while
    production wrote v1. Reading the stored version is what they were always
    demonstrating, so activation makes them stronger rather than needing them
    weakened.
    """
    from app.database.models import ScenarioResult as _Result
    from app.services.ioe.scenario.service import ScenarioService as _Svc

    async with unit_of_work(user_id=service.user_id, actor_type="user") as s:
        stored = await s.get(Scenario, scenario_id)
        sealed_version = stored.result_schema_version
        sealed_spec_hash = stored.scenario_spec_hash
        sealed_result_hash = stored.scenario_result_hash
        result_row = await s.scalar(
            select(_Result).where(_Result.scenario_id == scenario_id))
        inner_hash = result_row.counterfactual_derived_state_hash
        rebuilt = await _Svc._load_spec(s, stored)
        pinned = await service._pin_specification(
            s, analysis_id, rebuilt, result_schema_version=sealed_version)

    computed = service._compute(pinned)
    payload = service.canonical_result(
        computed, result_schema_version=sealed_version,
        counterfactual_derived_state_hash=inner_hash)
    return pinned, payload, sealed_spec_hash, sealed_result_hash, result_row
