"""Frozen-snapshot execution integrity — item 3A.

The defect: TX-1 pinned an immutable snapshot and put its hash into the spec
hash, and then the compute phase rebuilt tax inputs from the user's *live*
financial tables. The sequence

    pin snapshot A → live data becomes B → calculate from B → seal under A

produced a result whose identity claimed A and whose numbers were B.

These tests run that exact race and assert the corrected invariant: computation
reads only the pinned snapshot, there is no live fallback, and a newly sealed
run passes the real production replay verifier.
"""
import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select, text

from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
    IncomeSource,
    IncomeType,
    Jurisdiction,
    OptimizationRun,
    RuleAction,
    RuleOutcome,
    RuleSharedResource,
    StrategyPortfolio,
    TaxProfile,
    TaxRule,
    TaxRuleVersion,
    UserAccount,
)
from app.database.session import engine, unit_of_work
from app.services.ioe.domain.integrity import IntegrityStatus
from app.services.ioe.frozen.models import (
    FROZEN_INPUT_RECONSTRUCTION_FAILED,
    PINNED_SNAPSHOT_HASH_MISMATCH,
    PINNED_SNAPSHOT_INCOMPLETE,
    PINNED_SNAPSHOT_SCHEMA_UNSUPPORTED,
    PINNED_SNAPSHOT_UNAVAILABLE,
    FrozenSnapshotError,
    InputExecutionPolicy,
)
from app.services.ioe.orchestrator import OptimizationOrchestrator
from app.services.ioe.replay import IntegrityVerificationService
from tests.conftest import frozen_snapshot

SYNTHETIC_SIN = "046454286"
SYNTHETIC_ACCOUNT = "ACCT-55443322"


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    await engine.dispose()


def _suffix() -> str:
    return uuid.uuid4().hex[:8].upper()


async def _analysis(employment: str = "95000", *, snapshot=None):
    """A completed analysis with a COMPLETE frozen snapshot."""
    async with unit_of_work(actor_type="system") as s:
        user = UserAccount(email=f"fz_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(user)
        await s.flush()
        uid = user.id
        income_type_id = (await s.scalar(
            select(IncomeType).where(IncomeType.code == "employment"))).id

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
        payload, digest = snapshot or frozen_snapshot(
            employment_income=Decimal(employment))
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot=payload, snapshot_hash=digest))
        await s.flush()
        return uid, run.id, income_type_id


async def _publish(code, lever_code="INCREASE_RRSP_DEDUCTION",
                   amount="3000", resource="RRSP_ROOM"):
    async with unit_of_work(actor_type="admin") as s:
        jur = await s.scalar(select(Jurisdiction).where(Jurisdiction.code == "FED"))
        rule = TaxRule(code=code, name=f"{code} rule", category="deduction",
                       jurisdiction_id=jur.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=2025, effective_date=date(2025, 1, 1),
            status="published", description="frozen fixture",
            eligibility_basis_codes=["BASIS_FROZEN"],
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
        return version.id


async def _change_live_income(uid, income_type_id, amount: str):
    """Mutate the user's CURRENT financial state, as an edit would."""
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await s.execute(
            text("UPDATE finance.income_source SET amount = :amt "
                 "WHERE user_id = :uid AND tax_year = 2025"),
            {"amt": Decimal(amount), "uid": uid},
        )


# ---- the race that exposed the defect ---------------------------------------
@pytest.mark.asyncio
async def test_a_live_financial_change_after_tx1_cannot_affect_the_run():
    """Snapshot A pinned, live data becomes B, computation still uses A."""
    await _publish(f"FZR{_suffix()}")
    uid, analysis_id, income_type_id = await _analysis("95000")

    # The live figure moves by an amount that would change every tax number.
    await _change_live_income(uid, income_type_id, "450000")

    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)
    assert outcome.workflow_status == "completed"

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.get(OptimizationRun, outcome.run_id)
        portfolio = await s.scalar(
            select(StrategyPortfolio).where(StrategyPortfolio.run_id == run.id))
        snapshot = await s.get(AnalysisInputSnapshot, analysis_id)

    # the specification names snapshot A ...
    assert run.version_manifest["input_execution_policy_version"] == (
        InputExecutionPolicy.FROZEN_SNAPSHOT_V1.value)
    assert run.input_execution_policy_version == "frozen_snapshot_v1"

    # ... and the baseline the engine actually used is snapshot A's, not the
    # live 450,000 figure. A run computed from B would show a far larger tax.
    from app.services.ioe.frozen.models import reconstruct_tax_input
    from app.services.tax_engine.core.engine import compute

    expected = compute(reconstruct_tax_input(snapshot.snapshot)).total_payable
    assert portfolio.baseline_tax == expected.quantize(Decimal("0.01"))


@pytest.mark.asyncio
async def test_a_run_computed_against_a_changed_live_state_verifies_immediately():
    """The whole point: the sealed identity describes the actual calculation."""
    await _publish(f"FZV{_suffix()}")
    uid, analysis_id, income_type_id = await _analysis("95000")
    await _change_live_income(uid, income_type_id, "310000")

    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)
    result = await IntegrityVerificationService(uid).verify(
        "optimization", outcome.run_id)

    assert result.status is IntegrityStatus.VERIFIED
    assert result.integrity_state == "verified"


@pytest.mark.asyncio
async def test_the_same_snapshot_under_two_live_states_gives_one_result_hash():
    """Snapshot A + live B and snapshot A + live C must agree exactly."""
    await _publish(f"FZS{_suffix()}")
    payload, digest = frozen_snapshot(employment_income=Decimal("120000"))

    hashes = set()
    for live in ("77000", "265000"):
        uid, analysis_id, income_type_id = await _analysis(
            "120000", snapshot=(payload, digest))
        await _change_live_income(uid, income_type_id, live)
        outcome = await OptimizationOrchestrator(uid).generate(analysis_id)
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            run = await s.get(OptimizationRun, outcome.run_id)
            hashes.add((run.optimization_spec_hash, run.optimization_result_hash))

    assert len(hashes) == 1, "the same frozen snapshot produced two identities"


@pytest.mark.asyncio
async def test_a_materially_different_snapshot_gives_a_different_spec_hash():
    await _publish(f"FZD{_suffix()}")
    uid_a, analysis_a, _ = await _analysis(
        "95000", snapshot=frozen_snapshot(employment_income=Decimal("95000")))
    uid_b, analysis_b, _ = await _analysis(
        "95000", snapshot=frozen_snapshot(employment_income=Decimal("240000")))

    a = await OptimizationOrchestrator(uid_a).generate(analysis_a)
    b = await OptimizationOrchestrator(uid_b).generate(analysis_b)

    async with unit_of_work(user_id=uid_a, actor_type="user") as s:
        run_a = await s.get(OptimizationRun, a.run_id)
    async with unit_of_work(user_id=uid_b, actor_type="user") as s:
        run_b = await s.get(OptimizationRun, b.run_id)
    assert run_a.optimization_spec_hash != run_b.optimization_spec_hash


@pytest.mark.asyncio
async def test_a_rule_published_after_tx1_still_cannot_enter_the_run():
    """The pinned-version guarantee is unchanged by the frozen-input work."""
    await _publish(f"FZP{_suffix()}")
    uid, analysis_id, _ = await _analysis("95000")
    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    late = await _publish(f"FZLATE{_suffix()}")
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        pinned = {
            r[0] for r in await s.execute(text(
                "SELECT tax_rule_version_id FROM ioe.run_rule_version "
                "WHERE run_id = :run"), {"run": outcome.run_id})
        }
    assert late not in pinned


# ---- no live fallback -------------------------------------------------------
@pytest.mark.asyncio
async def test_no_mutable_source_table_is_queried_during_compute():
    """Defense in depth behind the import boundary: watch the actual SQL.

    TX-1 legitimately reads the snapshot and the analysis. The COMPUTE phase
    must touch no finance, profile or wealth table at all.
    """
    await _publish(f"FZQ{_suffix()}")
    uid, analysis_id, _ = await _analysis("95000")

    seen: list[str] = []
    watching = {"on": False}

    def _listen(conn, cursor, statement, parameters, context, executemany):
        if watching["on"]:
            seen.append(" ".join(statement.split()).lower())

    event.listen(engine.sync_engine, "before_cursor_execute", _listen)
    try:
        orchestrator = OptimizationOrchestrator(uid)
        async with unit_of_work(user_id=uid, actor_type="user") as session:
            spec = await orchestrator._pin_specification(
                session, analysis_id, user_constraints=None, assumptions=None)
        watching["on"] = True
        await orchestrator._compute(spec)
        watching["on"] = False
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _listen)

    forbidden = [
        sql for sql in seen
        if "finance." in sql or "profile." in sql or "wealth." in sql
    ]
    assert forbidden == [], f"compute queried mutable source tables: {forbidden[:2]}"


@pytest.mark.asyncio
async def test_the_compute_path_holds_no_user_keyed_input_builder():
    """A structural check, not a comment: the frozen input exposes no route
    back to mutable state."""
    from app.services.ioe.frozen.models import FrozenAnalysisInput

    fields = set(FrozenAnalysisInput.__dataclass_fields__)
    for banned in ("session", "repository", "loader", "engine_service"):
        assert not any(banned in name for name in fields)
    # frozen: a downstream service cannot swap the baseline underneath itself
    assert FrozenAnalysisInput.__dataclass_params__.frozen


@pytest.mark.asyncio
async def test_the_orchestrator_never_calls_a_live_input_builder():
    """AST-level: the optimization module must not name the live builder."""
    import ast
    import pathlib

    source = pathlib.Path("app/services/ioe/orchestrator.py").read_text()
    tree = ast.parse(source)
    called = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "build_input_from_live_sources" not in called
    assert "build_input" not in called
    assert "_build_input_live" not in called


# ---- snapshot integrity, failing closed -------------------------------------
@pytest.mark.asyncio
async def test_a_missing_snapshot_fails_closed_with_no_engine_run():
    await _publish(f"FZM{_suffix()}")
    uid, analysis_id, _ = await _analysis("95000")
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await s.execute(
            text("DELETE FROM analysis.analysis_input_snapshot "
                 "WHERE analysis_id = :a"), {"a": analysis_id})

    with pytest.raises(FrozenSnapshotError) as excinfo:
        await OptimizationOrchestrator(uid).generate(analysis_id)
    assert excinfo.value.reason == PINNED_SNAPSHOT_UNAVAILABLE

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        runs = await s.scalar(
            select(func.count()).select_from(OptimizationRun)
            .where(OptimizationRun.user_id == uid))
    assert runs == 0, "a workflow header was created despite failing closed"


@pytest.mark.asyncio
async def test_a_corrupted_snapshot_fails_hash_verification():
    await _publish(f"FZC{_suffix()}")
    uid, analysis_id, _ = await _analysis("95000")
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        row = await s.get(AnalysisInputSnapshot, analysis_id)
        tampered = json.loads(json.dumps(row.snapshot))
        tampered["inputs"]["employment_income"] = "999999"
        await s.execute(
            text("UPDATE analysis.analysis_input_snapshot SET snapshot = :s "
                 "WHERE analysis_id = :a"),
            {"s": json.dumps(tampered), "a": analysis_id},
        )

    with pytest.raises(FrozenSnapshotError) as excinfo:
        await OptimizationOrchestrator(uid).generate(analysis_id)
    assert excinfo.value.reason == PINNED_SNAPSHOT_HASH_MISMATCH


@pytest.mark.asyncio
async def test_an_incomplete_snapshot_fails_closed():
    from app.services.ioe.frozen.models import snapshot_hash

    payload, _ = frozen_snapshot(employment_income=Decimal("95000"))
    del payload["inputs"]["rrsp_deduction"]
    await _publish(f"FZI{_suffix()}")
    uid, analysis_id, _ = await _analysis(
        "95000", snapshot=(payload, snapshot_hash(payload)))

    with pytest.raises(FrozenSnapshotError) as excinfo:
        await OptimizationOrchestrator(uid).generate(analysis_id)
    assert excinfo.value.reason == PINNED_SNAPSHOT_INCOMPLETE


@pytest.mark.asyncio
async def test_an_unsupported_snapshot_schema_fails_closed():
    from app.services.ioe.frozen.models import snapshot_hash

    payload, _ = frozen_snapshot(employment_income=Decimal("95000"))
    payload["schema_version"] = "99.0.0"
    await _publish(f"FZU{_suffix()}")
    uid, analysis_id, _ = await _analysis(
        "95000", snapshot=(payload, snapshot_hash(payload)))

    with pytest.raises(FrozenSnapshotError) as excinfo:
        await OptimizationOrchestrator(uid).generate(analysis_id)
    assert excinfo.value.reason == PINNED_SNAPSHOT_SCHEMA_UNSUPPORTED


@pytest.mark.asyncio
async def test_a_snapshot_describing_another_tax_year_fails_closed():
    from app.services.ioe.frozen.models import snapshot_hash

    payload, _ = frozen_snapshot(
        tax_year=2024, employment_income=Decimal("95000"))
    await _publish(f"FZY{_suffix()}")
    uid, analysis_id, _ = await _analysis(
        "95000", snapshot=(payload, snapshot_hash(payload)))

    with pytest.raises(FrozenSnapshotError) as excinfo:
        await OptimizationOrchestrator(uid).generate(analysis_id)
    assert excinfo.value.reason in (
        PINNED_SNAPSHOT_INCOMPLETE, FROZEN_INPUT_RECONSTRUCTION_FAILED)


# ---- idempotency ------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_same_key_with_a_different_snapshot_is_refused():
    from app.services.ioe.orchestrator import IdempotencyKeyReused

    await _publish(f"FZK{_suffix()}")
    key = f"key-{_suffix()}"
    uid_a, analysis_a, _ = await _analysis(
        "95000", snapshot=frozen_snapshot(employment_income=Decimal("95000")))
    await OptimizationOrchestrator(uid_a).generate(analysis_a, idempotency_key=key)

    # same user, same key, a materially different frozen specification
    async with unit_of_work(user_id=uid_a, actor_type="user") as s:
        run = AnalysisRun(
            user_id=uid_a, tax_year=2025, province_code="ON",
            engine_version="py-1.0.0", status="completed",
            started_at=datetime.now(tz=UTC), completed_at=datetime.now(tz=UTC),
            data_verified=True)
        s.add(run)
        await s.flush()
        payload, digest = frozen_snapshot(employment_income=Decimal("310000"))
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot=payload, snapshot_hash=digest))
        await s.flush()
        second_analysis = run.id

    with pytest.raises(IdempotencyKeyReused):
        await OptimizationOrchestrator(uid_a).generate(
            second_analysis, idempotency_key=key)


@pytest.mark.asyncio
async def test_a_canonically_equivalent_frozen_request_replays():
    await _publish(f"FZE{_suffix()}")
    uid, analysis_id, _ = await _analysis("95000")
    key = f"key-{_suffix()}"
    first = await OptimizationOrchestrator(uid).generate(
        analysis_id, idempotency_key=key)
    second = await OptimizationOrchestrator(uid).generate(
        analysis_id, idempotency_key=key)
    assert first.run_id == second.run_id
    assert second.replayed is True


# ---- privacy ----------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_snapshot_payload_is_copied_into_evidence_or_logs(caplog):
    await _publish(f"FZZ{_suffix()}")
    payload, digest = frozen_snapshot(employment_income=Decimal("187654.33"))
    payload["inputs"]["other_income"] = SYNTHETIC_SIN
    from app.services.ioe.frozen.models import snapshot_hash

    uid, analysis_id, _ = await _analysis(
        "95000", snapshot=(payload, snapshot_hash(payload)))

    with caplog.at_level("DEBUG"):
        outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    emitted = "\n".join(r.getMessage() for r in caplog.records)
    emitted += json.dumps([getattr(r, "__dict__", {}) for r in caplog.records],
                          default=str)
    assert SYNTHETIC_SIN not in emitted
    assert SYNTHETIC_ACCOUNT not in emitted

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.get(OptimizationRun, outcome.run_id)
        manifest = json.dumps(run.version_manifest)
        constraints = json.dumps(run.user_constraints or {})
    # the manifest carries versions and hashes, never the payload
    assert SYNTHETIC_SIN not in manifest
    assert SYNTHETIC_SIN not in constraints
    assert "inputs" not in manifest


@pytest.mark.asyncio
async def test_a_snapshot_failure_reason_carries_no_payload():
    await _publish(f"FZN{_suffix()}")
    uid, analysis_id, _ = await _analysis("95000")
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await s.execute(
            text("DELETE FROM analysis.analysis_input_snapshot "
                 "WHERE analysis_id = :a"), {"a": analysis_id})

    with pytest.raises(FrozenSnapshotError) as excinfo:
        await OptimizationOrchestrator(uid).generate(analysis_id)
    # the whole error is a closed reason code
    assert str(excinfo.value) == excinfo.value.reason
    assert excinfo.value.reason.isupper()
