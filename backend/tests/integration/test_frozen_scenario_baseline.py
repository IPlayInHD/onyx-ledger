"""Frozen scenario-baseline integrity — item 3B.

The same defect class item 3A removed from the optimizer survived in
`ScenarioService`: TX-1 pinned `baseline_input_snapshot_hash` and then rebuilt
the baseline from the user's *live* financial tables. The sequence

    pin snapshot A → live data becomes B → apply levers to B → seal under A

produced a stored `tax_delta` that claimed to be a delta from A while being a
delta from B. A delta whose baseline is unrecorded is not evidence.

These tests run that race against real PostgreSQL and assert the corrected
invariant: every scenario baseline, hypothetical input, support score,
comparison and sealed result derives exclusively from the pinned snapshot and
the baseline-result identity taken from it.
"""
import ast
import json
import pathlib
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
    RuleOutcome,
    Scenario,
    ScenarioResult,
    TaxProfile,
    TaxRule,
    TaxRuleVersion,
    UserAccount,
)
from app.database.session import engine, unit_of_work
from app.services.ioe.domain.integrity import IntegrityStatus
from app.services.ioe.domain.scenario import ScenarioSpec
from app.services.ioe.frozen.models import (
    PINNED_SCENARIO_SNAPSHOT_HASH_MISMATCH,
    PINNED_SCENARIO_SNAPSHOT_SCHEMA_UNSUPPORTED,
    PINNED_SCENARIO_SNAPSHOT_UNAVAILABLE,
    FrozenScenarioExecutionInput,
    ScenarioExecutionPolicy,
    ScenarioPolicyError,
    scenario_execution_policy,
    snapshot_hash,
)
from app.services.ioe.replay import IntegrityVerificationService
from app.services.ioe.scenario.service import (
    PinnedScenarioSpec,
    ScenarioBaselineUnavailable,
    ScenarioIdempotencyKeyReused,
    ScenarioService,
)
from tests.conftest import frozen_snapshot

RRSP = "INCREASE_RRSP_DEDUCTION"
SYNTHETIC_SIN = "046454286"
MONEY = Decimal("0.01")


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    await engine.dispose()


def _suffix() -> str:
    return uuid.uuid4().hex[:8].upper()


def _spec(amount: str = "5000", **kw) -> ScenarioSpec:
    return ScenarioSpec.parse(
        [{"lever_code": RRSP, "parameters": {"amount": Decimal(amount)}}],
        assumptions=[], jurisdiction="ON", tax_year=2025, **kw,
    )


async def _analysis(live: str = "95000", *, snapshot=None):
    """A completed analysis whose SNAPSHOT and LIVE figures can differ."""
    async with unit_of_work(actor_type="system") as s:
        user = UserAccount(email=f"sb_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(user)
        await s.flush()
        uid = user.id
        income_type_id = (await s.scalar(
            select(IncomeType).where(IncomeType.code == "employment"))).id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(TaxProfile(user_id=uid, province_code="ON", marital_status="single"))
        s.add(IncomeSource(
            user_id=uid, tax_year=2025, income_type_id=income_type_id,
            amount=Decimal(live), province_code="ON",
        ))
        run = AnalysisRun(
            user_id=uid, tax_year=2025, province_code="ON", engine_version="py-1.0.0",
            status="completed", started_at=datetime.now(tz=UTC),
            completed_at=datetime.now(tz=UTC), data_verified=True,
        )
        s.add(run)
        await s.flush()
        payload, digest = snapshot or frozen_snapshot(
            employment_income=Decimal(live))
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot=payload, snapshot_hash=digest))
        await s.flush()
        return uid, run.id, income_type_id


async def _publish(code: str) -> uuid.UUID:
    async with unit_of_work(actor_type="admin") as s:
        jur = await s.scalar(select(Jurisdiction).where(Jurisdiction.code == "FED"))
        rule = TaxRule(code=code, name=code, category="deduction",
                       jurisdiction_id=jur.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=2025, effective_date=date(2025, 1, 1),
            status="published", description="3B fixture",
            eligibility_basis_codes=["BASIS_3B"],
        )
        s.add(version)
        await s.flush()
        s.add(RuleOutcome(
            rule_version_id=version.id, outcome_type="recommend", priority=1,
            title_template=f"{code} opportunity",
        ))
        await s.flush()
        return version.id


async def _change_live_income(uid, amount: str) -> None:
    """Mutate the user's CURRENT financial state, as an edit would."""
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await s.execute(
            text("UPDATE finance.income_source SET amount = :amt "
                 "WHERE user_id = :uid AND tax_year = 2025"),
            {"amt": Decimal(amount), "uid": uid},
        )


def _tax_from(payload: dict) -> Decimal:
    from app.services.ioe.frozen.models import reconstruct_tax_input
    from app.services.tax_engine.core.engine import compute

    return compute(reconstruct_tax_input(payload)).total_payable.quantize(MONEY)


# ---- the race that exposed the defect ---------------------------------------
@pytest.mark.asyncio
async def test_a_live_financial_change_after_pinning_cannot_affect_a_scenario():
    """Snapshot A pinned, live data becomes B, the scenario still uses A."""
    await _publish(f"SBR{_suffix()}")
    uid, analysis_id, _ = await _analysis("95000")

    # A move large enough to change every tax number if it leaked in.
    await _change_live_income(uid, "310000")

    outcome = await ScenarioService(uid).simulate(analysis_id, _spec())
    assert outcome.workflow_status == "completed"

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        result = await s.scalar(
            select(ScenarioResult).where(
                ScenarioResult.scenario_id == outcome.scenario_id))
        snapshot = await s.get(AnalysisInputSnapshot, analysis_id)

    expected_baseline = _tax_from(snapshot.snapshot)
    # The baseline the delta was measured from is the SNAPSHOT's, not the live
    # 310,000 figure — which would have produced a far larger number.
    assert scenario.baseline_tax == expected_baseline
    assert result.baseline_tax == expected_baseline
    assert result.tax_delta == (expected_baseline - result.scenario_tax)
    assert scenario.version_manifest["scenario_execution_policy_version"] == (
        ScenarioExecutionPolicy.FROZEN_SNAPSHOT_V1.value)


@pytest.mark.asyncio
async def test_the_hypothetical_is_the_snapshot_plus_the_lever_and_nothing_else():
    """The scenario tax must equal the engine over snapshot + lever, exactly."""
    await _publish(f"SBH{_suffix()}")
    uid, analysis_id, _ = await _analysis("95000")
    await _change_live_income(uid, "310000")

    outcome = await ScenarioService(uid).simulate(analysis_id, _spec("7000"))

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        result = await s.scalar(
            select(ScenarioResult).where(
                ScenarioResult.scenario_id == outcome.scenario_id))
        snapshot = await s.get(AnalysisInputSnapshot, analysis_id)

    payload = json.loads(json.dumps(snapshot.snapshot))
    payload["inputs"]["rrsp_deduction"] = str(
        Decimal(payload["inputs"]["rrsp_deduction"] or "0") + Decimal("7000"))
    assert result.scenario_tax == _tax_from(payload)


@pytest.mark.asyncio
async def test_a_scenario_computed_against_a_changed_live_state_verifies():
    """The sealed identity describes the calculation that actually happened."""
    await _publish(f"SBV{_suffix()}")
    uid, analysis_id, _ = await _analysis("95000")
    await _change_live_income(uid, "310000")

    outcome = await ScenarioService(uid).simulate(analysis_id, _spec())
    verified = await IntegrityVerificationService(uid).verify(
        "scenario", outcome.scenario_id)

    assert verified.status is IntegrityStatus.VERIFIED
    assert verified.integrity_state == "verified"


@pytest.mark.asyncio
async def test_the_same_snapshot_under_two_live_states_gives_one_identity():
    """Snapshot A + live B and snapshot A + live C must agree exactly."""
    await _publish(f"SBS{_suffix()}")
    payload, digest = frozen_snapshot(employment_income=Decimal("120000"))

    identities = set()
    for live in ("77000", "265000"):
        uid, analysis_id, _ = await _analysis("120000", snapshot=(payload, digest))
        await _change_live_income(uid, live)
        outcome = await ScenarioService(uid).simulate(analysis_id, _spec())
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            row = await s.get(Scenario, outcome.scenario_id)
            result = await s.scalar(select(ScenarioResult).where(
                ScenarioResult.scenario_id == outcome.scenario_id))
            identities.add((
                row.scenario_spec_hash, row.scenario_result_hash,
                row.baseline_result_hash, str(result.display_support_score),
            ))

    assert len(identities) == 1, "the same frozen snapshot produced two identities"


@pytest.mark.asyncio
async def test_a_materially_different_snapshot_gives_a_different_spec_hash():
    await _publish(f"SBD{_suffix()}")
    uid_a, analysis_a, _ = await _analysis(
        "95000", snapshot=frozen_snapshot(employment_income=Decimal("95000")))
    uid_b, analysis_b, _ = await _analysis(
        "95000", snapshot=frozen_snapshot(employment_income=Decimal("240000")))

    a = await ScenarioService(uid_a).simulate(analysis_a, _spec())
    b = await ScenarioService(uid_b).simulate(analysis_b, _spec())

    assert a.spec_hash != b.spec_hash
    async with unit_of_work(user_id=uid_a, actor_type="user") as s:
        row_a = await s.get(Scenario, a.scenario_id)
    async with unit_of_work(user_id=uid_b, actor_type="user") as s:
        row_b = await s.get(Scenario, b.scenario_id)
    assert row_a.baseline_result_hash != row_b.baseline_result_hash


@pytest.mark.asyncio
async def test_the_baseline_result_identity_is_derived_from_the_snapshot():
    await _publish(f"SBB{_suffix()}")
    uid, analysis_id, _ = await _analysis("95000")
    await _change_live_income(uid, "310000")

    outcome = await ScenarioService(uid).simulate(analysis_id, _spec())

    from app.services.ioe.frozen import baseline_result_pin

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        snapshot = await s.get(AnalysisInputSnapshot, analysis_id)

    assert scenario.baseline_result_hash == baseline_result_pin(
        _tax_from(snapshot.snapshot))
    assert scenario.version_manifest["baseline_result_hash"] == (
        scenario.baseline_result_hash)


# ---- no live fallback -------------------------------------------------------
@pytest.mark.asyncio
async def test_no_mutable_source_table_is_queried_while_simulating():
    """Defense in depth behind the import boundary: watch the actual SQL.

    Not just the compute phase — the WHOLE simulation. TX-1 legitimately reads
    the snapshot, the analysis and the rule registry; nothing in a scenario has
    any business touching finance, profile or wealth.
    """
    await _publish(f"SBQ{_suffix()}")
    uid, analysis_id, _ = await _analysis("95000")

    seen: list[str] = []

    def _listen(conn, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.split()).lower())

    event.listen(engine.sync_engine, "before_cursor_execute", _listen)
    try:
        await ScenarioService(uid).simulate(analysis_id, _spec())
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _listen)

    forbidden = [
        sql for sql in seen
        if "finance." in sql or "profile." in sql or "wealth." in sql
    ]
    assert forbidden == [], f"simulation queried mutable sources: {forbidden[:2]}"


@pytest.mark.asyncio
async def test_comparing_two_sealed_scenarios_reads_no_mutable_source():
    """A comparison is a statement about two sealed results, nothing more."""
    from app.services.ioe.scenario.comparison_service import ScenarioComparisonService

    await _publish(f"SBX{_suffix()}")
    uid, analysis_id, _ = await _analysis("95000")
    left = await ScenarioService(uid).simulate(analysis_id, _spec("4000"))
    right = await ScenarioService(uid).simulate(analysis_id, _spec("9000"))

    seen: list[str] = []

    def _listen(conn, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.split()).lower())

    event.listen(engine.sync_engine, "before_cursor_execute", _listen)
    try:
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            await ScenarioComparisonService(s, uid).compare(
                left.scenario_id, right.scenario_id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _listen)

    forbidden = [
        sql for sql in seen
        if "finance." in sql or "profile." in sql or "wealth." in sql
    ]
    assert forbidden == [], f"comparison queried mutable sources: {forbidden[:2]}"


@pytest.mark.asyncio
async def test_the_scenario_module_never_calls_a_live_input_builder():
    """AST-level: the scenario service must not name a live builder."""
    source = pathlib.Path("app/services/ioe/scenario/service.py").read_text()
    called = {
        node.func.attr for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "build_input_from_live_sources" not in called
    assert "build_input" not in called
    assert "_build_input_live" not in called


@pytest.mark.asyncio
async def test_the_pinned_scenario_spec_carries_no_second_input_channel():
    """Structural, not a comment: there is one baseline and it is frozen."""
    fields = set(FrozenScenarioExecutionInput.__dataclass_fields__)
    for banned in ("session", "repository", "loader", "engine_service"):
        assert not any(banned in name for name in fields)
    assert FrozenScenarioExecutionInput.__dataclass_params__.frozen

    # The pinned spec holds no raw-input dict that could drift from the
    # snapshot its hash names.
    assert "baseline_inputs" not in PinnedScenarioSpec.__dataclass_fields__
    assert "frozen" in PinnedScenarioSpec.__dataclass_fields__


@pytest.mark.asyncio
async def test_the_frozen_baseline_is_never_mutated_by_a_lever():
    await _publish(f"SBM{_suffix()}")
    uid, analysis_id, _ = await _analysis("95000")

    service = ScenarioService(uid)
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        pinned = await service._pin_specification(s, analysis_id, _spec("8000"))

    before = pinned.frozen.baseline_clone()
    service._compute(pinned)
    assert pinned.frozen.baseline_clone() == before
    assert pinned.frozen.baseline_tax == pinned.baseline_tax


# ---- failing closed ---------------------------------------------------------
@pytest.mark.asyncio
async def test_a_missing_snapshot_fails_closed_with_no_scenario_row():
    await _publish(f"SBN{_suffix()}")
    uid, analysis_id, _ = await _analysis("95000")
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await s.execute(
            text("DELETE FROM analysis.analysis_input_snapshot "
                 "WHERE analysis_id = :a"), {"a": analysis_id})

    with pytest.raises(ScenarioBaselineUnavailable) as excinfo:
        await ScenarioService(uid).simulate(analysis_id, _spec())
    assert excinfo.value.detail == PINNED_SCENARIO_SNAPSHOT_UNAVAILABLE
    assert excinfo.value.status_code == 409

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        count = await s.scalar(
            select(func.count()).select_from(Scenario)
            .where(Scenario.user_id == uid))
    assert count == 0, "a scenario header was created despite failing closed"


@pytest.mark.asyncio
async def test_a_corrupted_snapshot_fails_hash_verification():
    await _publish(f"SBC{_suffix()}")
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

    with pytest.raises(ScenarioBaselineUnavailable) as excinfo:
        await ScenarioService(uid).simulate(analysis_id, _spec())
    assert excinfo.value.detail == PINNED_SCENARIO_SNAPSHOT_HASH_MISMATCH


@pytest.mark.asyncio
async def test_an_unsupported_snapshot_schema_fails_closed():
    payload, _ = frozen_snapshot(employment_income=Decimal("95000"))
    payload["schema_version"] = "99.0.0"
    await _publish(f"SBU{_suffix()}")
    uid, analysis_id, _ = await _analysis(
        "95000", snapshot=(payload, snapshot_hash(payload)))

    with pytest.raises(ScenarioBaselineUnavailable) as excinfo:
        await ScenarioService(uid).simulate(analysis_id, _spec())
    assert excinfo.value.detail == PINNED_SCENARIO_SNAPSHOT_SCHEMA_UNSUPPORTED


@pytest.mark.asyncio
async def test_an_incomplete_snapshot_fails_closed():
    payload, _ = frozen_snapshot(employment_income=Decimal("95000"))
    del payload["inputs"]["rrsp_deduction"]
    await _publish(f"SBI{_suffix()}")
    uid, analysis_id, _ = await _analysis(
        "95000", snapshot=(payload, snapshot_hash(payload)))

    with pytest.raises(ScenarioBaselineUnavailable) as excinfo:
        await ScenarioService(uid).simulate(analysis_id, _spec())
    assert excinfo.value.detail == PINNED_SCENARIO_SNAPSHOT_UNAVAILABLE


@pytest.mark.asyncio
async def test_a_refusal_carries_only_a_closed_reason_code():
    await _publish(f"SBF{_suffix()}")
    uid, analysis_id, _ = await _analysis("95000")
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await s.execute(
            text("DELETE FROM analysis.analysis_input_snapshot "
                 "WHERE analysis_id = :a"), {"a": analysis_id})

    with pytest.raises(ScenarioBaselineUnavailable) as excinfo:
        await ScenarioService(uid).simulate(analysis_id, _spec())
    assert excinfo.value.detail.isupper()
    assert excinfo.value.detail.startswith("PINNED_SCENARIO_")


# ---- idempotency ------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_same_key_with_a_different_snapshot_is_refused():
    await _publish(f"SBK{_suffix()}")
    key = f"key-{_suffix()}"
    uid, analysis_id, _ = await _analysis(
        "95000", snapshot=frozen_snapshot(employment_income=Decimal("95000")))
    await ScenarioService(uid).simulate(analysis_id, _spec(), idempotency_key=key)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = AnalysisRun(
            user_id=uid, tax_year=2025, province_code="ON",
            engine_version="py-1.0.0", status="completed",
            started_at=datetime.now(tz=UTC), completed_at=datetime.now(tz=UTC),
            data_verified=True)
        s.add(run)
        await s.flush()
        payload, digest = frozen_snapshot(employment_income=Decimal("310000"))
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot=payload, snapshot_hash=digest))
        await s.flush()
        second = run.id

    with pytest.raises(ScenarioIdempotencyKeyReused):
        await ScenarioService(uid).simulate(second, _spec(), idempotency_key=key)


@pytest.mark.asyncio
async def test_a_canonically_equivalent_frozen_request_replays():
    await _publish(f"SBE{_suffix()}")
    uid, analysis_id, _ = await _analysis("95000")
    key = f"key-{_suffix()}"
    first = await ScenarioService(uid).simulate(
        analysis_id, _spec(), idempotency_key=key)
    # a different label must not change identity — and neither must live data
    await _change_live_income(uid, "310000")
    second = await ScenarioService(uid).simulate(
        analysis_id, _spec(label="renamed"), idempotency_key=key)

    assert first.scenario_id == second.scenario_id
    assert second.replayed is True


# ---- replay -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_replaced_snapshot_is_unavailable_rather_than_a_mismatch():
    """Nothing was compared, so the verdict must not read as non-reproducible."""
    await _publish(f"SBP{_suffix()}")
    uid, analysis_id, _ = await _analysis("95000")
    outcome = await ScenarioService(uid).simulate(analysis_id, _spec())

    replacement, digest = frozen_snapshot(employment_income=Decimal("410000"))
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await s.execute(
            text("UPDATE analysis.analysis_input_snapshot "
                 "SET snapshot = :s, snapshot_hash = :h WHERE analysis_id = :a"),
            {"s": json.dumps(replacement), "h": digest, "a": analysis_id},
        )

    verified = await IntegrityVerificationService(uid).verify(
        "scenario", outcome.scenario_id)
    assert verified.status is IntegrityStatus.UNAVAILABLE
    assert verified.integrity_state != "non_reproducible"


# ---- legacy classification --------------------------------------------------
@pytest.mark.asyncio
async def test_a_manifest_without_the_policy_key_reads_as_legacy():
    """Silence is legacy. A historical scenario never inherits the guarantee."""
    assert scenario_execution_policy({}) == (
        ScenarioExecutionPolicy.LIVE_BASELINE_LEGACY.value)
    assert scenario_execution_policy(None) == (
        ScenarioExecutionPolicy.LIVE_BASELINE_LEGACY.value)
    assert scenario_execution_policy(
        {"scenario_execution_policy_version": "frozen_snapshot_v1"}
    ) == ScenarioExecutionPolicy.FROZEN_SNAPSHOT_V1.value
    # A malformed value is NOT silently read as legacy — see the closeout suite,
    # `test_a_malformed_policy_fails_closed_and_never_falls_back`.
    with pytest.raises(ScenarioPolicyError):
        scenario_execution_policy({"scenario_execution_policy_version": "made_up"})


@pytest.mark.asyncio
async def test_a_corrected_scenario_reports_its_policy_to_a_reader():
    from app.services.ioe import presentation

    await _publish(f"SBL{_suffix()}")
    uid, analysis_id, _ = await _analysis("95000")
    outcome = await ScenarioService(uid).simulate(analysis_id, _spec())

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        integrity = presentation.integrity_of(scenario)
    assert integrity.execution_policy == (
        ScenarioExecutionPolicy.FROZEN_SNAPSHOT_V1.value)


# ---- privacy ----------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_snapshot_payload_reaches_logs_or_sealed_evidence(caplog):
    await _publish(f"SBZ{_suffix()}")
    payload, _ = frozen_snapshot(employment_income=Decimal("187654.33"))
    payload["inputs"]["other_income"] = SYNTHETIC_SIN
    uid, analysis_id, _ = await _analysis(
        "95000", snapshot=(payload, snapshot_hash(payload)))

    with caplog.at_level("DEBUG"):
        outcome = await ScenarioService(uid).simulate(analysis_id, _spec())

    emitted = "\n".join(r.getMessage() for r in caplog.records)
    emitted += json.dumps([getattr(r, "__dict__", {}) for r in caplog.records],
                          default=str)
    assert SYNTHETIC_SIN not in emitted

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        changes = (await s.execute(text(
            "SELECT old_value, new_value FROM ioe.scenario_input_change "
            "WHERE scenario_id = :s"), {"s": outcome.scenario_id})).all()

    manifest = json.dumps(scenario.version_manifest)
    assert SYNTHETIC_SIN not in manifest
    assert "inputs" not in manifest
    # The applied-change trace records only the fields a lever touched.
    assert SYNTHETIC_SIN not in json.dumps([list(map(str, r)) for r in changes])
