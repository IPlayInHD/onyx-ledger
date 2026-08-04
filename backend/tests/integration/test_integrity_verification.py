"""Production replay-integrity verification — release blocker 3.

These tests hold the running system to the claim that it can verify a sealed
result rather than merely assert determinism in a unit test. Every path is
exercised against real persisted evidence: verified, mismatch, unavailable,
concurrency, tenant isolation, and the privacy of what leaves the database.

The mismatch fixtures never weaken production immutability to manufacture a
failure. They corrupt a REPLAY-side double — a verifier that returns a
different hash, or a dependency the resolver cannot load — so the sealed rows
stay exactly as the system wrote them.
"""
import asyncio
import hashlib
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
    IncomeSource,
    IncomeType,
    IntegrityCheck,
    Jurisdiction,
    OptimizationRun,
    RuleAction,
    RuleOutcome,
    RuleSharedResource,
    Scenario,
    StrategyPortfolio,
    TaxProfile,
    TaxRule,
    TaxRuleVersion,
    UserAccount,
)
from app.database.session import engine, unit_of_work
from app.services.ioe.domain.integrity import (
    NON_REPRODUCIBLE,
    VERIFIER_VERSION,
    CheckStatus,
    EntityType,
    IntegrityReason,
    IntegrityStatus,
    integrity_warning,
    user_visible_state,
)
from app.services.ioe.domain.scenario import ScenarioSpec
from app.services.ioe.orchestrator import OptimizationOrchestrator
from app.services.ioe.replay import services as replay_services
from app.services.ioe.replay.scheduler import IntegrityScheduler
from app.services.ioe.replay.services import ReplayOutcome
from app.services.ioe.replay.verification import (
    IntegrityVerificationService,
    VerificationAlreadyRunning,
)
from app.services.ioe.scenario.service import ScenarioService
from app.services.tax_engine.core.engine import TaxInput

# Synthetic values that must never leave the database. Chosen to be searchable.
SYNTHETIC_SIN = "046454286"
SYNTHETIC_INCOME = "187654.33"
SYNTHETIC_ACCOUNT = "ACCT-99887766"
SYNTHETIC_DOCUMENT = "T4 slip for Jordan Blackwood, employer Northwind Ltd"


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    await engine.dispose()


def _suffix() -> str:
    return uuid.uuid4().hex[:8].upper()


async def _user_with_frozen_baseline(employment: str = SYNTHETIC_INCOME):
    """A user whose analysis carries a COMPLETE frozen baseline snapshot.

    `AnalysisService` writes every `TaxInput` field; a fixture that writes a
    stub cannot be replayed, which is a legitimate `unavailable` and is
    exercised separately.
    """
    async with unit_of_work(actor_type="system") as s:
        user = UserAccount(email=f"integ_{uuid.uuid4().hex[:8]}@test.ca", status="active")
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
            amount=Decimal(employment), province_code="ON",
        ))
        run = AnalysisRun(
            user_id=uid, tax_year=2025, province_code="ON", engine_version="py-1.0.0",
            status="completed", started_at=datetime.now(tz=UTC),
            completed_at=datetime.now(tz=UTC), data_verified=True,
        )
        s.add(run)
        await s.flush()
        inp = TaxInput(province="ON", year=2025, marital_status="single",
                       employment_income=Decimal(employment))
        snapshot = {k: str(v) for k, v in vars(inp).items()}
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot=snapshot,
            snapshot_hash=hashlib.sha256(
                json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
        ))
        await s.flush()
        return uid, run.id


async def _user_with_stub_baseline():
    """A user whose analysis snapshot cannot be rebuilt into a `TaxInput`."""
    async with unit_of_work(actor_type="system") as s:
        user = UserAccount(email=f"stub_{uuid.uuid4().hex[:8]}@test.ca", status="active")
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
            amount=Decimal("95000"), province_code="ON",
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
            snapshot_hash=f"stub-{uuid.uuid4().hex[:8]}",
        ))
        await s.flush()
        return uid, run.id


async def _publish(code: str, lever_code: str, amount: str, resource: str | None):
    async with unit_of_work(actor_type="admin") as s:
        jur = await s.scalar(select(Jurisdiction).where(Jurisdiction.code == "FED"))
        rule = TaxRule(code=code, name=f"{code} rule", category="deduction",
                       jurisdiction_id=jur.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=2025, effective_date=date(2025, 1, 1),
            status="published", description="integrity fixture",
            eligibility_basis_codes=["BASIS_INTEGRITY"],
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
        if resource:
            s.add(RuleSharedResource(
                rule_version_id=version.id, resource_code=resource))
        await s.flush()


_LEVERS = [
    ("INCREASE_RRSP_DEDUCTION", "RRSP_ROOM"),
    ("INCREASE_FHSA_DEDUCTION", "FHSA_ROOM"),
    ("INCREASE_DONATIONS", None),
]


async def _sealed_optimization(rules: int = 3):
    tag = _suffix()
    for i in range(rules):
        lever, resource = _LEVERS[i % len(_LEVERS)]
        await _publish(f"INTG{tag}_{i}", lever, str(2000 + i * 400), resource)
    uid, analysis_id = await _user_with_frozen_baseline()
    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)
    return uid, analysis_id, outcome.run_id


async def _sealed_scenario(uid, analysis_id):
    outcome = await ScenarioService(uid).simulate(
        analysis_id,
        ScenarioSpec.parse([{"lever_code": "INCREASE_RRSP_DEDUCTION",
                             "parameters": {"amount": Decimal("5000")}}]),
    )
    return outcome.scenario_id


async def _portfolio_id(uid, run_id):
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        portfolio = await s.scalar(
            select(StrategyPortfolio).where(StrategyPortfolio.run_id == run_id))
        return portfolio.id if portfolio else None


# ---- verified replay --------------------------------------------------------
@pytest.mark.asyncio
async def test_a_persisted_optimization_replays_to_the_same_hash():
    uid, _, run_id = await _sealed_optimization()
    result = await IntegrityVerificationService(uid).verify("optimization", run_id)

    assert result.status is IntegrityStatus.VERIFIED
    assert result.reason_code is IntegrityReason.NONE
    assert result.integrity_state == "verified"

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.get(OptimizationRun, run_id)
        assert run.integrity_status == "verified"
        assert run.integrity_reason_code == "NONE"
        assert run.last_integrity_checked_at is not None
        assert run.latest_integrity_check_id == result.check_id

        check = await s.get(IntegrityCheck, result.check_id)
        assert check.status == CheckStatus.VERIFIED.value
        assert check.actual_result_hash == check.expected_result_hash
        assert check.expected_result_hash == run.optimization_result_hash
        assert check.verifier_version == VERIFIER_VERSION


@pytest.mark.asyncio
async def test_a_persisted_portfolio_replays_to_the_same_hash():
    uid, _, run_id = await _sealed_optimization()
    portfolio_id = await _portfolio_id(uid, run_id)
    assert portfolio_id is not None

    result = await IntegrityVerificationService(uid).verify("portfolio", portfolio_id)
    assert result.status is IntegrityStatus.VERIFIED

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        portfolio = await s.get(StrategyPortfolio, portfolio_id)
        assert portfolio.integrity_status == "verified"
        assert portfolio.portfolio_result_hash


@pytest.mark.asyncio
async def test_a_persisted_scenario_replays_to_the_same_hash():
    uid, analysis_id, _ = await _sealed_optimization()
    scenario_id = await _sealed_scenario(uid, analysis_id)

    result = await IntegrityVerificationService(uid).verify("scenario", scenario_id)
    assert result.status is IntegrityStatus.VERIFIED

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, scenario_id)
        assert scenario.integrity_status == "verified"
        # freshness is a separate axis and must not have moved
        assert scenario.freshness_status in ("current", "unknown", "stale")


@pytest.mark.asyncio
async def test_history_is_append_only_across_repeated_verification():
    uid, _, run_id = await _sealed_optimization()
    service = IntegrityVerificationService(uid)
    first = await service.verify("optimization", run_id)
    second = await service.verify("optimization", run_id)

    assert first.check_id != second.check_id
    history = await service.history("optimization", run_id)
    assert len(history) == 2
    assert {h.id for h in history} == {first.check_id, second.check_id}

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.get(OptimizationRun, run_id)
        # current metadata tracks the LATEST completed check
        assert run.latest_integrity_check_id == second.check_id


@pytest.mark.asyncio
async def test_a_completed_check_cannot_be_rewritten():
    uid, _, run_id = await _sealed_optimization()
    result = await IntegrityVerificationService(uid).verify("optimization", run_id)

    with pytest.raises(Exception, match="already terminal|immutable"):
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            await s.execute(
                text("UPDATE ioe.integrity_check SET status = 'mismatch' "
                     "WHERE id = :id"),
                {"id": result.check_id},
            )

    with pytest.raises(Exception, match="append-only"):
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            await s.execute(
                text("DELETE FROM ioe.integrity_check WHERE id = :id"),
                {"id": result.check_id},
            )


# ---- mismatch ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_replay_that_produces_a_different_hash_is_a_mismatch(monkeypatch):
    """The sealed rows are untouched: a REPLAY double returns a different hash."""
    uid, _, run_id = await _sealed_optimization()

    async def divergent(self, entity_id):
        return ReplayOutcome(
            expected_hash="e" * 64, actual_hash="a" * 64,
            mismatch_reason=IntegrityReason.RESULT_HASH_MISMATCH,
        )

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.get(OptimizationRun, run_id)
        sealed_result_hash = run.optimization_result_hash
        sealed_spec_hash = run.optimization_spec_hash

    monkeypatch.setattr(
        replay_services.OptimizationReplayService, "replay", divergent)
    result = await IntegrityVerificationService(uid).verify("optimization", run_id)

    assert result.status is IntegrityStatus.MISMATCH
    assert result.reason_code is IntegrityReason.RESULT_HASH_MISMATCH
    assert result.integrity_state == NON_REPRODUCIBLE
    assert "could not be reproduced" in result.integrity_warning

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.get(OptimizationRun, run_id)
        # sealed evidence unchanged
        assert run.optimization_result_hash == sealed_result_hash
        assert run.optimization_spec_hash == sealed_spec_hash
        assert run.workflow_status == "completed"
        assert run.integrity_status == "mismatch"

        check = await s.get(IntegrityCheck, result.check_id)
        # the EXPECTED hash is the sealed one, never the observed one
        assert check.expected_result_hash == sealed_result_hash
        assert check.actual_result_hash == "a" * 64
        assert check.operational_event_id is not None


@pytest.mark.asyncio
async def test_a_mismatch_does_not_create_a_replacement_run(monkeypatch):
    uid, _, run_id = await _sealed_optimization()

    async def divergent(self, entity_id):
        return ReplayOutcome(expected_hash="e" * 64, actual_hash="a" * 64)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        before = await s.scalar(
            select(func.count()).select_from(OptimizationRun)
            .where(OptimizationRun.user_id == uid))

    monkeypatch.setattr(
        replay_services.OptimizationReplayService, "replay", divergent)
    await IntegrityVerificationService(uid).verify("optimization", run_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        after = await s.scalar(
            select(func.count()).select_from(OptimizationRun)
            .where(OptimizationRun.user_id == uid))
    assert after == before


@pytest.mark.asyncio
async def test_repeated_mismatch_handling_is_idempotent(monkeypatch):
    uid, _, run_id = await _sealed_optimization()

    async def divergent(self, entity_id):
        return ReplayOutcome(expected_hash="e" * 64, actual_hash="a" * 64)

    monkeypatch.setattr(
        replay_services.OptimizationReplayService, "replay", divergent)
    service = IntegrityVerificationService(uid)
    first = await service.verify("optimization", run_id)
    second = await service.verify("optimization", run_id)

    assert first.status is second.status is IntegrityStatus.MISMATCH
    assert len(await service.history("optimization", run_id)) == 2


@pytest.mark.asyncio
async def test_a_prior_verification_survives_a_later_mismatch(monkeypatch):
    uid, _, run_id = await _sealed_optimization()
    service = IntegrityVerificationService(uid)
    verified = await service.verify("optimization", run_id)
    assert verified.status is IntegrityStatus.VERIFIED

    async def divergent(self, entity_id):
        return ReplayOutcome(expected_hash="e" * 64, actual_hash="a" * 64)

    monkeypatch.setattr(
        replay_services.OptimizationReplayService, "replay", divergent)
    mismatched = await service.verify("optimization", run_id)

    history = {h.id: h.status for h in await service.history("optimization", run_id)}
    assert history[verified.check_id] == CheckStatus.VERIFIED.value
    assert history[mismatched.check_id] == CheckStatus.MISMATCH.value

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.get(OptimizationRun, run_id)
        assert run.integrity_status == "mismatch"          # the LATEST verdict


# ---- unavailable ------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_missing_baseline_snapshot_is_unavailable_not_mismatch():
    tag = _suffix()
    await _publish(f"STUB{tag}", "INCREASE_RRSP_DEDUCTION", "3000", "RRSP_ROOM")
    uid, analysis_id = await _user_with_stub_baseline()
    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    result = await IntegrityVerificationService(uid).verify(
        "optimization", outcome.run_id)
    assert result.status is IntegrityStatus.UNAVAILABLE
    assert result.reason_code is IntegrityReason.BASELINE_SNAPSHOT_UNAVAILABLE
    assert "could not currently be replay-verified" in result.integrity_warning


@pytest.mark.asyncio
async def test_a_pinned_engine_version_that_is_not_running_is_unavailable(monkeypatch):
    uid, _, run_id = await _sealed_optimization()
    monkeypatch.setattr(
        "app.services.ioe.replay.resolver.EXECUTABLE_VERSIONS",
        {"tax_engine_version": (
            "py-99.0.0", IntegrityReason.PINNED_ENGINE_VERSION_UNAVAILABLE)},
    )
    result = await IntegrityVerificationService(uid).verify("optimization", run_id)
    assert result.status is IntegrityStatus.UNAVAILABLE
    assert result.reason_code is IntegrityReason.PINNED_ENGINE_VERSION_UNAVAILABLE


@pytest.mark.asyncio
async def test_an_unsupported_canonical_serialization_version_is_unavailable(monkeypatch):
    uid, _, run_id = await _sealed_optimization()
    monkeypatch.setattr(
        "app.services.ioe.replay.resolver.SUPPORTED_CANONICAL_SERIALIZATION_VERSIONS",
        frozenset({"0.0.1"}),
    )
    result = await IntegrityVerificationService(uid).verify("optimization", run_id)
    assert result.status is IntegrityStatus.UNAVAILABLE
    assert result.reason_code is (
        IntegrityReason.CANONICAL_SERIALIZATION_VERSION_UNSUPPORTED)


@pytest.mark.asyncio
async def test_a_missing_rule_snapshot_pin_is_unavailable():
    uid, _, run_id = await _sealed_optimization()
    # Remove the pin under the purge context the schema already defines for
    # evidence removal, so production immutability is not weakened here.
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await s.execute(text("SELECT set_config('app.allow_evidence_purge','on',true)"))
        await s.execute(
            text("DELETE FROM ioe.run_rule_snapshot WHERE run_id = :run"),
            {"run": run_id},
        )

    result = await IntegrityVerificationService(uid).verify("optimization", run_id)
    assert result.status is IntegrityStatus.UNAVAILABLE
    assert result.reason_code is IntegrityReason.PINNED_RULE_SNAPSHOT_UNAVAILABLE


@pytest.mark.asyncio
async def test_an_unavailable_outcome_preserves_the_sealed_result():
    tag = _suffix()
    await _publish(f"PRES{tag}", "INCREASE_RRSP_DEDUCTION", "3000", "RRSP_ROOM")
    uid, analysis_id = await _user_with_stub_baseline()
    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.get(OptimizationRun, outcome.run_id)
        sealed = run.optimization_result_hash

    await IntegrityVerificationService(uid).verify("optimization", outcome.run_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.get(OptimizationRun, outcome.run_id)
        assert run.optimization_result_hash == sealed
        assert run.workflow_status == "completed"
        check = await s.get(IntegrityCheck, run.latest_integrity_check_id)
        assert check.actual_result_hash is None      # nothing was compared


@pytest.mark.asyncio
async def test_a_verifier_exception_stores_only_a_sanitized_reason(monkeypatch):
    uid, _, run_id = await _sealed_optimization()

    async def explode(self, entity_id):
        raise RuntimeError(
            f"boom income={SYNTHETIC_INCOME} sin={SYNTHETIC_SIN}")

    monkeypatch.setattr(
        replay_services.OptimizationReplayService, "replay", explode)
    result = await IntegrityVerificationService(uid).verify("optimization", run_id)

    # a broken verifier proves nothing about the result
    assert result.status is IntegrityStatus.UNAVAILABLE
    assert result.reason_code is IntegrityReason.REPLAY_EXECUTION_FAILED

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        check = await s.get(IntegrityCheck, result.check_id)
        stored = json.dumps({
            "reason": check.reason_code, "status": check.status,
            "actual": check.actual_result_hash,
        })
    assert SYNTHETIC_INCOME not in stored
    assert SYNTHETIC_SIN not in stored
    assert "boom" not in stored


# ---- concurrency ------------------------------------------------------------
@pytest.mark.asyncio
async def test_three_simultaneous_checks_produce_one_active_computation():
    uid, _, run_id = await _sealed_optimization()

    async def attempt(worker: str):
        try:
            return await IntegrityVerificationService(
                uid, worker_id=worker).verify("optimization", run_id)
        except VerificationAlreadyRunning:
            return None

    results = await asyncio.gather(*(attempt(f"w{i}") for i in range(3)))
    completed = [r for r in results if r is not None]
    assert len(completed) == 1, "more than one worker computed the same check"

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        rows = list(await s.scalars(
            select(IntegrityCheck).where(IntegrityCheck.optimization_run_id == run_id)))
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_an_abandoned_claim_is_recoverable():
    uid, _, run_id = await _sealed_optimization()

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        check = IntegrityCheck(
            user_id=uid, entity_type="optimization", optimization_run_id=run_id,
            status="running", reason_code="NONE",
            expected_result_hash="e" * 64,
            verifier_version=VERIFIER_VERSION,
            canonical_serialization_version="1.1.0",
            integrity_check_policy_version="1.0.0",
            claimed_by="dead-worker",
            claim_expires_at=datetime.now(tz=UTC) - timedelta(minutes=30),
        )
        s.add(check)
        await s.flush()
        abandoned_id = check.id

    # the active-check index is held, so a new verification is refused
    with pytest.raises(VerificationAlreadyRunning):
        await IntegrityVerificationService(uid).verify("optimization", run_id)

    recovered = await IntegrityScheduler("sweeper").recover_stale_claims()
    assert recovered >= 1

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        row = await s.get(IntegrityCheck, abandoned_id)
        assert row.status == CheckStatus.FAILED.value
        assert row.reason_code == IntegrityReason.REPLAY_EXECUTION_FAILED.value

    # and the entity is verifiable again, with the abandoned attempt retained
    result = await IntegrityVerificationService(uid).verify("optimization", run_id)
    assert result.status is IntegrityStatus.VERIFIED
    history = await IntegrityVerificationService(uid).history("optimization", run_id)
    assert abandoned_id in {h.id for h in history}


@pytest.mark.asyncio
async def test_a_different_worker_cannot_complete_another_workers_check():
    uid, _, run_id = await _sealed_optimization()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        check = IntegrityCheck(
            user_id=uid, entity_type="optimization", optimization_run_id=run_id,
            status="running", reason_code="NONE", expected_result_hash="e" * 64,
            verifier_version="other-verifier",
            canonical_serialization_version="1.1.0",
            integrity_check_policy_version="1.0.0",
            claimed_by="worker-a",
            claim_expires_at=datetime.now(tz=UTC) + timedelta(minutes=10),
        )
        s.add(check)
        await s.flush()
        check_id = check.id

    with pytest.raises(Exception, match="claimed by another worker"):
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            await s.execute(
                text("UPDATE ioe.integrity_check "
                     "SET status='verified', reason_code='NONE', "
                     "    actual_result_hash=expected_result_hash, "
                     "    completed_at=now(), claimed_by='worker-b' "
                     "WHERE id = :id"),
                {"id": check_id},
            )


# ---- security ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_user_cannot_verify_another_users_entity():
    uid_a, _, run_id = await _sealed_optimization()
    uid_b, _ = await _user_with_frozen_baseline()

    from app.core.exceptions import NotFound

    with pytest.raises(NotFound):
        await IntegrityVerificationService(uid_b).verify("optimization", run_id)


@pytest.mark.asyncio
async def test_integrity_history_is_tenant_isolated():
    uid_a, _, run_id = await _sealed_optimization()
    await IntegrityVerificationService(uid_a).verify("optimization", run_id)
    uid_b, _ = await _user_with_frozen_baseline()

    history = await IntegrityVerificationService(uid_b).history("optimization", run_id)
    assert history == []


@pytest.mark.asyncio
async def test_integrity_checks_deny_by_default_without_tenant_context():
    uid, _, run_id = await _sealed_optimization()
    await IntegrityVerificationService(uid).verify("optimization", run_id)

    async with unit_of_work(actor_type="system") as s:      # no app.user_id
        visible = await s.scalar(
            select(func.count()).select_from(IntegrityCheck))
    assert visible == 0


@pytest.mark.asyncio
async def test_the_integrity_table_is_in_the_rls_inventory():
    async with unit_of_work(actor_type="system") as s:
        row = (await s.execute(text(
            "SELECT c.relrowsecurity, c.relforcerowsecurity, "
            "       (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) "
            "  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            " WHERE n.nspname='ioe' AND c.relname='integrity_check'"
        ))).one()
    enabled, forced, policies = row
    assert enabled and forced
    assert policies >= 1


@pytest.mark.asyncio
async def test_the_scheduling_keyhole_is_not_executable_by_public():
    async with unit_of_work(actor_type="system") as s:
        rows = (await s.execute(text(
            "SELECT p.proname, "
            "       has_function_privilege('public', p.oid, 'EXECUTE'), "
            "       p.prosecdef, "
            "       pg_get_function_identity_arguments(p.oid), "
            "       array_to_string(p.proconfig, ',') "
            "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            " WHERE n.nspname='ioe' AND p.proname IN "
            "       ('claim_integrity_targets','recover_stale_integrity_checks')"
        ))).all()
    assert len(rows) == 2
    for name, public_exec, secdef, _args, config in rows:
        assert not public_exec, f"{name} is executable by PUBLIC"
        assert secdef, f"{name} must be SECURITY DEFINER"
        assert "search_path=" in (config or ""), f"{name} has no fixed search_path"
        assert "pg_catalog" in (config or ""), f"{name} omits pg_catalog"


@pytest.mark.asyncio
async def test_the_scheduler_claim_returns_identifiers_only():
    uid, _, run_id = await _sealed_optimization()
    targets = await IntegrityScheduler("probe").claim(batch_size=5)
    assert targets
    for target in targets:
        # the dataclass has exactly three fields, all identifiers
        assert set(vars(target)) == {"entity_type", "entity_id", "user_id"}
        payload = json.dumps(vars(target), default=str)
        assert SYNTHETIC_INCOME not in payload
        assert SYNTHETIC_SIN not in payload


@pytest.mark.asyncio
async def test_the_scheduler_batch_is_bounded_in_sql():
    await _sealed_optimization()
    async with unit_of_work(actor_type="system") as s:
        rows = (await s.execute(text(
            "SELECT count(*) FROM ioe.claim_integrity_targets(10000, 'optimization')"
        ))).scalar()
    assert rows <= 50


# ---- privacy ----------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_synthetic_sensitive_value_reaches_an_operational_event(caplog):
    uid, _, run_id = await _sealed_optimization()

    with caplog.at_level("DEBUG"):
        await IntegrityVerificationService(uid).verify("optimization", run_id)

    emitted = "\n".join(record.getMessage() for record in caplog.records)
    emitted += json.dumps([getattr(r, "__dict__", {}) for r in caplog.records],
                          default=str)
    for secret in (SYNTHETIC_SIN, SYNTHETIC_ACCOUNT, SYNTHETIC_DOCUMENT):
        assert secret not in emitted
    # the income is the user's actual employment figure and must not be logged
    assert SYNTHETIC_INCOME not in emitted


@pytest.mark.asyncio
async def test_stored_integrity_fields_carry_no_financial_values():
    uid, _, run_id = await _sealed_optimization()
    await IntegrityVerificationService(uid).verify("optimization", run_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        rows = list(await s.scalars(
            select(IntegrityCheck).where(IntegrityCheck.optimization_run_id == run_id)))
    dumped = json.dumps([
        {
            "status": r.status, "reason": r.reason_code,
            "expected": r.expected_result_hash, "actual": r.actual_result_hash,
            "verifier": r.verifier_version, "claimed_by": r.claimed_by,
        }
        for r in rows
    ])
    for secret in (SYNTHETIC_INCOME, SYNTHETIC_SIN, SYNTHETIC_ACCOUNT):
        assert secret not in dumped


# ---- state model ------------------------------------------------------------
def test_a_mismatch_is_surfaced_as_non_reproducible():
    assert user_visible_state(IntegrityStatus.MISMATCH) == NON_REPRODUCIBLE
    assert user_visible_state(IntegrityStatus.VERIFIED) == "verified"
    assert user_visible_state(IntegrityStatus.UNAVAILABLE) == "unavailable"
    assert user_visible_state(IntegrityStatus.NOT_CHECKED) == "not_checked"
    # a mismatch must never be describable as ordinary staleness
    assert "stale" not in integrity_warning(IntegrityStatus.MISMATCH).lower()


def test_integrity_wording_makes_no_correctness_or_cra_claim():
    for status in IntegrityStatus:
        text_out = integrity_warning(status).lower()
        for forbidden in ("cra", "correct", "guarantee", "accepted", "legal"):
            assert forbidden not in text_out, f"{status} claims '{forbidden}'"


def test_every_entity_type_is_supported():
    assert {e.value for e in EntityType} == {"optimization", "portfolio", "scenario"}
