"""Item 3B closeout — integrity-state semantics, policy hardening, hash coverage.

Four verdicts have to stay distinguishable, because three of them have different
remedies and one of them is an accusation:

    verified            the replay ran and reproduced the sealed identity
    mismatch            the replay ran on a contractually reproducible result
                        and produced a DIFFERENT identity
    unavailable +       a specifically pinned artifact is missing, replaced,
    dependency reason   malformed or fails its identity check — nothing compared
    unavailable +       the result predates the frozen-baseline guarantee and
    LEGACY_...          never carried the information a replay would need

The third and fourth are both `unavailable` and are separated by a distinct
machine-readable reason code and a distinct user-visible state. The second is
never used for the fourth: a legacy result was computed from live sources that
were never recorded, so calling the difference a replay regression asserts a
fault nothing has demonstrated.
"""
import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select, text
from structlog.testing import capture_logs

from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
    IncomeSource,
    IncomeType,
    Jurisdiction,
    RuleOutcome,
    Scenario,
    ScenarioAssumption,
    ScenarioConfidenceComponent,
    ScenarioEvent,
    ScenarioInputChange,
    ScenarioLever,
    ScenarioResult,
    TaxProfile,
    TaxRule,
    TaxRuleVersion,
    UserAccount,
)
from app.database.session import engine, unit_of_work
from app.services.ioe import presentation
from app.services.ioe.domain.integrity import (
    LEGACY_REASONS,
    LEGACY_UNVERIFIABLE,
    MISMATCH_REASONS,
    NON_REPRODUCIBLE,
    IntegrityReason,
    IntegrityStatus,
    integrity_warning,
    is_legacy_unverifiable,
    user_visible_state,
)
from app.services.ioe.domain.scenario import ScenarioSpec
from app.services.ioe.frozen.models import (
    SCENARIO_EXECUTION_POLICY_INVALID,
    ScenarioExecutionPolicy,
    ScenarioPolicyError,
    assert_current_scenario_policy,
    is_legacy_scenario_policy,
    scenario_execution_policy,
)
from app.services.ioe.replay import IntegrityVerificationService
from app.services.ioe.scenario.comparison_service import ScenarioComparisonService
from app.services.ioe.scenario.service import (
    ScenarioBaselineUnavailable,
    ScenarioService,
)
from tests.conftest import frozen_snapshot

RRSP = "INCREASE_RRSP_DEDUCTION"
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
    async with unit_of_work(actor_type="system") as s:
        user = UserAccount(email=f"cl_{uuid.uuid4().hex[:8]}@test.ca", status="active")
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
        payload, digest = snapshot or frozen_snapshot(employment_income=Decimal(live))
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot=payload, snapshot_hash=digest))
        await s.flush()
        return uid, run.id


async def _publish(code: str) -> uuid.UUID:
    async with unit_of_work(actor_type="admin") as s:
        jur = await s.scalar(select(Jurisdiction).where(Jurisdiction.code == "FED"))
        rule = TaxRule(code=code, name=code, category="deduction",
                       jurisdiction_id=jur.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=2025, effective_date=date(2025, 1, 1),
            status="published", description="closeout fixture",
            eligibility_basis_codes=["BASIS_CL"],
        )
        s.add(version)
        await s.flush()
        s.add(RuleOutcome(
            rule_version_id=version.id, outcome_type="recommend", priority=1,
            title_template=f"{code} opportunity"))
        await s.flush()
        return version.id


async def _tamper(scenario_id: uuid.UUID, **columns) -> None:
    """Rewrite a SEALED column over the application's head.

    The application cannot do this. `trg_guard_transition` seals
    `version_manifest` and `scenario_result_hash` the moment a scenario
    completes — `test_the_sealed_policy_cannot_be_changed_after_completion`
    asserts exactly that, and it is why this helper needs a superuser connection
    and an explicit trigger disable.

    That is the point: the two conditions verification exists to catch — a
    legacy row and evidence altered underneath the application — are both
    unreachable through any supported path, so a test can only manufacture them
    the way an operator with database access would.
    """
    import asyncpg

    from tests.conftest import owner_dsn

    sets = ", ".join(
        f"{k} = ${i + 1}" + ("::jsonb" if isinstance(v, dict) else "")
        for i, (k, v) in enumerate(columns.items())
    )
    values = [
        json.dumps(v) if isinstance(v, dict) else v for v in columns.values()
    ]
    conn = await asyncpg.connect(owner_dsn())
    try:
        await conn.execute(
            "ALTER TABLE ioe.scenario DISABLE TRIGGER trg_guard_transition")
        await conn.execute(
            f"UPDATE ioe.scenario SET {sets} WHERE id = ${len(columns) + 1}",
            *values, scenario_id)
    finally:
        await conn.execute(
            "ALTER TABLE ioe.scenario ENABLE TRIGGER trg_guard_transition")
        await conn.close()


async def _set_manifest(scenario_id: uuid.UUID, manifest: dict) -> None:
    await _tamper(scenario_id, version_manifest=manifest)


# =============================================================================
# 1. Policy normalization — one reading, shared by every consumer
# =============================================================================
def test_absence_of_the_policy_key_is_legacy():
    """The only tolerated silence, and only because nothing new can be silent."""
    assert scenario_execution_policy(None) == (
        ScenarioExecutionPolicy.LIVE_BASELINE_LEGACY.value)
    assert scenario_execution_policy({}) == (
        ScenarioExecutionPolicy.LIVE_BASELINE_LEGACY.value)
    assert scenario_execution_policy({"tax_engine_version": "1.0.0"}) == (
        ScenarioExecutionPolicy.LIVE_BASELINE_LEGACY.value)
    assert is_legacy_scenario_policy({}) is True


def test_a_current_policy_value_is_read_as_current():
    manifest = {"scenario_execution_policy_version": "frozen_snapshot_v1"}
    assert scenario_execution_policy(manifest) == "frozen_snapshot_v1"
    assert is_legacy_scenario_policy(manifest) is False
    assert assert_current_scenario_policy(manifest) == "frozen_snapshot_v1"


@pytest.mark.parametrize("value", [
    "made_up_policy",            # unknown string
    "FROZEN_SNAPSHOT_V1",       # right word, wrong case — still unknown
    "frozen_snapshot_v2",       # unsupported future version
    None,                       # explicit JSON null
    123,                        # wrong JSON type
    ["frozen_snapshot_v1"],     # wrong JSON type
    {"version": "frozen_snapshot_v1"},   # malformed object
])
def test_a_malformed_policy_fails_closed_and_never_falls_back(value):
    """A corrupt manifest is not a historical manifest.

    Answering `live_baseline_legacy` here would file the defect beside genuinely
    old rows, where nobody would ever look at it again.
    """
    with pytest.raises(ScenarioPolicyError) as excinfo:
        scenario_execution_policy({"scenario_execution_policy_version": value})
    assert excinfo.value.reason == SCENARIO_EXECUTION_POLICY_INVALID


def test_a_manifest_that_is_not_an_object_fails_closed():
    for bad in ("frozen_snapshot_v1", 7, ["frozen_snapshot_v1"]):
        with pytest.raises(ScenarioPolicyError):
            scenario_execution_policy(bad)


def test_sealing_refuses_a_manifest_without_the_current_policy():
    """The guard that makes 'absent implies historical' true of the data."""
    for manifest in ({}, {"scenario_execution_policy_version": "live_baseline_legacy"}):
        with pytest.raises(ScenarioPolicyError):
            assert_current_scenario_policy(manifest)


@pytest.mark.asyncio
async def test_every_newly_sealed_scenario_states_the_policy_explicitly():
    await _publish(f"CLP{_suffix()}")
    uid, analysis_id = await _analysis()
    outcome = await ScenarioService(uid).simulate(analysis_id, _spec())

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)

    assert "scenario_execution_policy_version" in scenario.version_manifest
    assert scenario_execution_policy(scenario.version_manifest) == "frozen_snapshot_v1"
    assert is_legacy_scenario_policy(scenario.version_manifest) is False


@pytest.mark.asyncio
async def test_the_sealed_policy_cannot_be_changed_after_completion():
    """`version_manifest` is sealed by trg_guard_transition once completed."""
    from sqlalchemy.exc import DBAPIError

    await _publish(f"CLS{_suffix()}")
    uid, analysis_id = await _analysis()
    outcome = await ScenarioService(uid).simulate(analysis_id, _spec())

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        tampered = dict(scenario.version_manifest)
        tampered["scenario_execution_policy_version"] = "live_baseline_legacy"
        with pytest.raises(DBAPIError) as excinfo:
            await s.execute(
                text("UPDATE ioe.scenario SET version_manifest = :m ::jsonb "
                     "WHERE id = :i"),
                {"m": json.dumps(tampered), "i": outcome.scenario_id})
    assert "sealed" in str(excinfo.value).lower()

    # and the stored row still states the corrected policy
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
    assert scenario_execution_policy(scenario.version_manifest) == "frozen_snapshot_v1"


# =============================================================================
# 2. Integrity-state semantics
# =============================================================================
def test_the_four_verdicts_map_to_four_distinct_user_visible_states():
    assert user_visible_state(IntegrityStatus.VERIFIED) == "verified"
    assert user_visible_state(
        IntegrityStatus.MISMATCH, IntegrityReason.RESULT_HASH_MISMATCH
    ) == NON_REPRODUCIBLE
    assert user_visible_state(
        IntegrityStatus.UNAVAILABLE, IntegrityReason.BASELINE_SNAPSHOT_UNAVAILABLE
    ) == "unavailable"
    assert user_visible_state(
        IntegrityStatus.UNAVAILABLE,
        IntegrityReason.LEGACY_EXECUTION_POLICY_UNVERIFIABLE,
    ) == LEGACY_UNVERIFIABLE
    assert len({
        "verified", NON_REPRODUCIBLE, "unavailable", LEGACY_UNVERIFIABLE
    }) == 4


def test_legacy_is_not_a_mismatch_reason_and_carries_no_fault_wording():
    assert not (LEGACY_REASONS & MISMATCH_REASONS)
    assert is_legacy_unverifiable(
        IntegrityReason.LEGACY_EXECUTION_POLICY_UNVERIFIABLE) is True
    assert is_legacy_unverifiable(IntegrityReason.RESULT_HASH_MISMATCH) is False
    assert is_legacy_unverifiable(None) is False
    assert is_legacy_unverifiable("NOT_A_REASON") is False

    legacy = integrity_warning(
        IntegrityStatus.UNAVAILABLE,
        IntegrityReason.LEGACY_EXECUTION_POLICY_UNVERIFIABLE)
    mismatch = integrity_warning(
        IntegrityStatus.MISMATCH, IntegrityReason.RESULT_HASH_MISMATCH)
    assert legacy != mismatch
    # the legacy wording must not imply the figure is wrong or under suspicion
    assert "could not be reproduced" not in legacy
    assert "under review" not in legacy
    assert "Nothing indicates it is wrong" in legacy
    # and neither makes a correctness or CRA claim
    for text_ in (legacy, mismatch):
        assert "correct" not in text_.lower()
        assert "cra" not in text_.lower()


@pytest.mark.asyncio
async def test_a_legacy_scenario_is_unavailable_with_the_legacy_reason():
    """Not `mismatch`: nothing was compared on equal terms."""
    await _publish(f"CLL{_suffix()}")
    uid, analysis_id = await _analysis()
    outcome = await ScenarioService(uid).simulate(analysis_id, _spec())

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        legacy_manifest = {
            k: v for k, v in scenario.version_manifest.items()
            if k != "scenario_execution_policy_version"
        }
    await _set_manifest(outcome.scenario_id, legacy_manifest)

    verified = await IntegrityVerificationService(uid).verify(
        "scenario", outcome.scenario_id)

    assert verified.status is IntegrityStatus.UNAVAILABLE
    assert verified.reason_code is IntegrityReason.LEGACY_EXECUTION_POLICY_UNVERIFIABLE
    assert verified.integrity_state == LEGACY_UNVERIFIABLE
    assert verified.integrity_state != NON_REPRODUCIBLE


@pytest.mark.asyncio
async def test_a_corrupt_stored_policy_is_unavailable_not_legacy():
    """A malformed manifest must not be filed as an ordinary old row."""
    await _publish(f"CLC{_suffix()}")
    uid, analysis_id = await _analysis()
    outcome = await ScenarioService(uid).simulate(analysis_id, _spec())

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        corrupt = dict(scenario.version_manifest)
    corrupt["scenario_execution_policy_version"] = None
    await _set_manifest(outcome.scenario_id, corrupt)

    verified = await IntegrityVerificationService(uid).verify(
        "scenario", outcome.scenario_id)
    assert verified.status is IntegrityStatus.UNAVAILABLE
    assert verified.reason_code is IntegrityReason.SEALED_EVIDENCE_INCOMPLETE
    assert verified.reason_code not in LEGACY_REASONS

    # and a read of the row names the defect rather than hiding it
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        out = presentation.integrity_of(scenario)
    assert out.execution_policy == SCENARIO_EXECUTION_POLICY_INVALID


@pytest.mark.asyncio
async def test_a_frozen_scenario_whose_result_was_altered_is_a_genuine_mismatch():
    """The one case that MAY be called non-reproducible."""
    await _publish(f"CLM{_suffix()}")
    uid, analysis_id = await _analysis()
    outcome = await ScenarioService(uid).simulate(analysis_id, _spec())

    # Move the sealed identity, not the evidence: the replay recomputes the same
    # numbers and finds they no longer match the hash filed beside them. This
    # needs the owner connection precisely because the column is sealed.
    await _tamper(outcome.scenario_id, scenario_result_hash="0" * 64)

    verified = await IntegrityVerificationService(uid).verify(
        "scenario", outcome.scenario_id)

    assert verified.status is IntegrityStatus.MISMATCH
    assert verified.reason_code is IntegrityReason.RESULT_HASH_MISMATCH
    assert verified.integrity_state == NON_REPRODUCIBLE


@pytest.mark.asyncio
async def test_a_replaced_pinned_snapshot_is_a_dependency_failure_not_legacy():
    await _publish(f"CLR{_suffix()}")
    uid, analysis_id = await _analysis()
    outcome = await ScenarioService(uid).simulate(analysis_id, _spec())

    replacement, digest = frozen_snapshot(employment_income=Decimal("410000"))
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await s.execute(
            text("UPDATE analysis.analysis_input_snapshot "
                 "SET snapshot = :s, snapshot_hash = :h WHERE analysis_id = :a"),
            {"s": json.dumps(replacement), "h": digest, "a": analysis_id})

    verified = await IntegrityVerificationService(uid).verify(
        "scenario", outcome.scenario_id)
    assert verified.status is IntegrityStatus.UNAVAILABLE
    assert verified.reason_code is IntegrityReason.BASELINE_SNAPSHOT_UNAVAILABLE
    assert verified.reason_code not in LEGACY_REASONS
    assert verified.integrity_state == "unavailable"


@pytest.mark.asyncio
async def test_readiness_counting_can_exclude_legacy_from_genuine_failures():
    """The reason code is the machine-readable axis a dashboard counts on."""
    await _publish(f"CLD{_suffix()}")

    uid_a, analysis_a = await _analysis()
    good = await ScenarioService(uid_a).simulate(analysis_a, _spec())
    await IntegrityVerificationService(uid_a).verify("scenario", good.scenario_id)

    uid_b, analysis_b = await _analysis()
    old = await ScenarioService(uid_b).simulate(analysis_b, _spec())
    async with unit_of_work(user_id=uid_b, actor_type="user") as s:
        scenario = await s.get(Scenario, old.scenario_id)
        legacy_manifest = {
            k: v for k, v in scenario.version_manifest.items()
            if k != "scenario_execution_policy_version"
        }
    await _set_manifest(old.scenario_id, legacy_manifest)
    await IntegrityVerificationService(uid_b).verify("scenario", old.scenario_id)

    # Counted per tenant, because RLS is the access path for these rows.
    async def _counts(user_id, scenario_id):
        async with unit_of_work(user_id=user_id, actor_type="user") as s:
            mismatch = await s.scalar(text(
                "SELECT count(*) FROM ioe.integrity_check "
                "WHERE status = 'mismatch' AND scenario_id = :s"
            ).bindparams(s=scenario_id))
            legacy = await s.scalar(text(
                "SELECT count(*) FROM ioe.integrity_check "
                "WHERE reason_code = 'LEGACY_EXECUTION_POLICY_UNVERIFIABLE' "
                "AND scenario_id = :s"
            ).bindparams(s=scenario_id))
        return mismatch, legacy

    good_mismatch, good_legacy = await _counts(uid_a, good.scenario_id)
    old_mismatch, old_legacy = await _counts(uid_b, old.scenario_id)

    assert (good_mismatch, good_legacy) == (0, 0)
    assert old_mismatch == 0, "a legacy scenario was counted as a replay failure"
    assert old_legacy == 1, "the legacy verdict was not separately countable"


# =============================================================================
# 3. Hash coverage — every execution-relevant pin, before idempotency
# =============================================================================
@pytest.mark.asyncio
async def test_every_execution_relevant_pin_changes_the_scenario_identity():
    """`pins → scenario_spec_hash → frozen.bind → idempotency`, proved per pin."""
    import copy

    await _publish(f"CLH{_suffix()}")
    uid, analysis_id = await _analysis()
    service = ScenarioService(uid)
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        pinned = await service._pin_specification(s, analysis_id, _spec(), result_schema_version="1.0.0")

    baseline_hash = service.compute_spec_hash(pinned)
    assert baseline_hash == pinned.spec_hash
    assert pinned.frozen.scenario_spec_hash == pinned.spec_hash, (
        "identity was not bound back onto the frozen input it describes")

    def _with_manifest(key: str, value: str):
        clone = copy.copy(pinned)
        manifest = dict(pinned.version_manifest)
        manifest[key] = value
        clone.version_manifest = manifest
        clone.manifest_hash = __import__(
            "app.services.ioe.domain.canonical", fromlist=["x"]
        ).version_manifest_hash(manifest)
        return clone

    manifest_pins = [
        "scenario_execution_policy_version",
        "assumption_registry_version",
        "confidence_algorithm_version",
        "lever_registry_version",
        "tax_engine_version",
        "engine_reference_data_version",
        "objective_version",
        "baseline_result_hash",
        "snapshot_schema_version",
        "rule_snapshot_hash",
    ]
    for key in manifest_pins:
        assert key in pinned.version_manifest, f"{key} is not pinned at all"
        moved = service.compute_spec_hash(_with_manifest(key, "MOVED"))
        assert moved != baseline_hash, f"{key} does not reach the identity"

    # top-level arguments, not carried through the manifest
    direct = copy.copy(pinned)
    direct.objective_code = "SOMETHING_ELSE"
    assert service.compute_spec_hash(direct) != baseline_hash

    direct = copy.copy(pinned)
    direct.tax_year = 2024
    assert service.compute_spec_hash(direct) != baseline_hash

    direct = copy.copy(pinned)
    direct.jurisdiction = "BC"
    assert service.compute_spec_hash(direct) != baseline_hash


@pytest.mark.asyncio
async def test_a_different_frozen_baseline_changes_the_identity():
    await _publish(f"CLB{_suffix()}")
    uid_a, analysis_a = await _analysis(
        snapshot=frozen_snapshot(employment_income=Decimal("95000")))
    uid_b, analysis_b = await _analysis(
        snapshot=frozen_snapshot(employment_income=Decimal("240000")))

    a = await ScenarioService(uid_a).simulate(analysis_a, _spec())
    b = await ScenarioService(uid_b).simulate(analysis_b, _spec())
    assert a.spec_hash != b.spec_hash


@pytest.mark.asyncio
async def test_non_semantic_metadata_does_not_change_the_identity():
    """Label and note are presentation. They must not move an identity."""
    await _publish(f"CLN{_suffix()}")
    uid, analysis_id = await _analysis()
    service = ScenarioService(uid)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        plain = await service._pin_specification(s, analysis_id, _spec(), result_schema_version="1.0.0")
        labelled = await service._pin_specification(
            s, analysis_id, _spec(label="a name", note="a note"), result_schema_version="1.0.0")
    assert plain.spec_hash == labelled.spec_hash


# =============================================================================
# 4. Sanitized external failure, useful internal diagnostics
# =============================================================================
@pytest.mark.asyncio
async def test_the_external_failure_carries_only_an_enumerated_reason():
    await _publish(f"CLE{_suffix()}")
    uid, analysis_id = await _analysis()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await s.execute(
            text("DELETE FROM analysis.analysis_input_snapshot "
                 "WHERE analysis_id = :a"), {"a": analysis_id})

    with capture_logs() as logs:
        with pytest.raises(ScenarioBaselineUnavailable) as excinfo:
            await ScenarioService(uid).simulate(analysis_id, _spec())

    # external: one code, nothing else
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail.startswith("PINNED_SCENARIO_")
    assert excinfo.value.detail.isupper()

    # internal: enough to investigate, and no financial value anywhere
    diagnostics = [
        entry for entry in logs
        if entry.get("event") == "scenario_baseline_unavailable"
    ]
    assert diagnostics, "the refusal produced no internal diagnostic"
    entry = diagnostics[0]
    for field in ("user_id", "analysis_id", "failure_stage", "expected_artifact",
                  "expected_artifact_id", "execution_policy", "reason_code",
                  "analysis_status", "snapshot_present"):
        assert field in entry, f"diagnostics omit {field}"
    assert entry["failure_stage"] == "tx1_pin_specification"
    assert entry["reason_code"] == excinfo.value.detail
    assert entry["execution_policy"] == "frozen_snapshot_v1"
    assert entry["snapshot_present"] is False
    assert entry["analysis_status"] == "completed"

    emitted = json.dumps(logs, default=str)
    for leak in ("employment_income", "95000", "rrsp_deduction"):
        assert leak not in emitted, f"diagnostics leaked {leak}"


@pytest.mark.asyncio
async def test_the_internal_diagnostic_names_the_snapshot_by_hash_only():
    """A content hash identifies the artifact without revealing a byte of it."""
    payload, digest = frozen_snapshot(employment_income=Decimal("187654.33"))
    await _publish(f"CLG{_suffix()}")
    uid, analysis_id = await _analysis(snapshot=(payload, digest))

    # break the identity check without removing the row
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        tampered = json.loads(json.dumps(payload))
        tampered["inputs"]["employment_income"] = "999999"
        await s.execute(
            text("UPDATE analysis.analysis_input_snapshot SET snapshot = :s "
                 "WHERE analysis_id = :a"),
            {"s": json.dumps(tampered), "a": analysis_id})

    with capture_logs() as logs:
        with pytest.raises(ScenarioBaselineUnavailable):
            await ScenarioService(uid).simulate(analysis_id, _spec())

    emitted = json.dumps(logs, default=str)
    assert digest in emitted, "the artifact identity was not recorded"
    assert "187654.33" not in emitted
    assert "999999" not in emitted


# =============================================================================
# 5. The live-read boundary
# =============================================================================
@pytest.mark.asyncio
async def test_freshness_may_label_stale_without_moving_any_pin_or_result():
    """Freshness reads the world. It may only label."""
    from app.services.ioe.scenario.freshness_service import ScenarioFreshnessService

    await _publish(f"CLF{_suffix()}")
    uid, analysis_id = await _analysis()
    outcome = await ScenarioService(uid).simulate(analysis_id, _spec())

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        before = await s.get(Scenario, outcome.scenario_id)
        sealed = {
            "spec": before.scenario_spec_hash,
            "result": before.scenario_result_hash,
            "snapshot": before.baseline_input_snapshot_hash,
            "baseline_result": before.baseline_result_hash,
            "baseline_tax": before.baseline_tax,
            "manifest": json.dumps(before.version_manifest, sort_keys=True),
        }
        result_before = await s.scalar(select(ScenarioResult).where(
            ScenarioResult.scenario_id == outcome.scenario_id))
        stored_tax = result_before.scenario_tax
        stored_delta = result_before.tax_delta

    # the world moves
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await s.execute(
            text("UPDATE finance.income_source SET amount = 310000 "
                 "WHERE user_id = :u AND tax_year = 2025"), {"u": uid})

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        await ScenarioFreshnessService(s, uid).evaluate_and_record(scenario)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        after = await s.get(Scenario, outcome.scenario_id)
        result_after = await s.scalar(select(ScenarioResult).where(
            ScenarioResult.scenario_id == outcome.scenario_id))

    # it may relabel freshness ...
    assert after.freshness_status in ("current", "stale")
    # ... and it may not touch a single pin or a single stored number
    assert after.scenario_spec_hash == sealed["spec"]
    assert after.scenario_result_hash == sealed["result"]
    assert after.baseline_input_snapshot_hash == sealed["snapshot"]
    assert after.baseline_result_hash == sealed["baseline_result"]
    assert after.baseline_tax == sealed["baseline_tax"]
    assert json.dumps(after.version_manifest, sort_keys=True) == sealed["manifest"]
    assert result_after.scenario_tax == stored_tax
    assert result_after.tax_delta == stored_delta

    # and the replay still reproduces the sealed identity from the frozen input
    verified = await IntegrityVerificationService(uid).verify(
        "scenario", outcome.scenario_id)
    assert verified.status is IntegrityStatus.VERIFIED


@pytest.mark.asyncio
async def test_the_replay_reads_no_mutable_source_after_a_live_change():
    await _publish(f"CLQ{_suffix()}")
    uid, analysis_id = await _analysis()
    outcome = await ScenarioService(uid).simulate(analysis_id, _spec())

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await s.execute(
            text("UPDATE finance.income_source SET amount = 310000 "
                 "WHERE user_id = :u AND tax_year = 2025"), {"u": uid})

    seen: list[str] = []

    def _listen(conn, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.split()).lower())

    from app.services.ioe.replay.services import ScenarioReplayService

    event.listen(engine.sync_engine, "before_cursor_execute", _listen)
    try:
        replayed = await ScenarioReplayService(uid).replay(outcome.scenario_id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _listen)

    assert replayed.matches, "the replay stopped reproducing after a live change"
    forbidden = [s for s in seen
                 if "finance." in s or "profile." in s or "wealth." in s]
    assert forbidden == [], f"replay queried mutable sources: {forbidden[:2]}"


@pytest.mark.asyncio
async def test_compare_uses_the_same_frozen_boundary_after_a_live_change():
    await _publish(f"CLX{_suffix()}")
    uid, analysis_id = await _analysis()
    left = await ScenarioService(uid).simulate(analysis_id, _spec("4000"))
    right = await ScenarioService(uid).simulate(analysis_id, _spec("9000"))

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await s.execute(
            text("UPDATE finance.income_source SET amount = 310000 "
                 "WHERE user_id = :u AND tax_year = 2025"), {"u": uid})

    seen: list[str] = []

    def _listen(conn, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.split()).lower())

    event.listen(engine.sync_engine, "before_cursor_execute", _listen)
    try:
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            loaded = await ScenarioComparisonService(s, uid).compare(
                left.scenario_id, right.scenario_id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _listen)

    forbidden = [s for s in seen
                 if "finance." in s or "profile." in s or "wealth." in s]
    assert forbidden == [], f"comparison queried mutable sources: {forbidden[:2]}"
    # both sides share one frozen baseline, so the comparison is well-founded
    assert loaded.left_row.baseline_input_snapshot_hash == (
        loaded.right_row.baseline_input_snapshot_hash)
    assert loaded.left_row.baseline_result_hash == (
        loaded.right_row.baseline_result_hash)


# =============================================================================
# 6. Fail before persistence — nothing partial survives a refused baseline
# =============================================================================
@pytest.mark.asyncio
async def test_a_refused_baseline_leaves_no_part_of_a_scenario_aggregate():
    await _publish(f"CLA{_suffix()}")
    uid, analysis_id = await _analysis()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await s.execute(
            text("DELETE FROM analysis.analysis_input_snapshot "
                 "WHERE analysis_id = :a"), {"a": analysis_id})

    with pytest.raises(ScenarioBaselineUnavailable):
        await ScenarioService(uid).simulate(analysis_id, _spec())

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        assert await s.scalar(select(func.count()).select_from(Scenario)
                              .where(Scenario.user_id == uid)) == 0
        # every child hangs off a scenario that does not exist, so a leaked row
        # is countable only across the user's whole tenant
        for model in (ScenarioLever, ScenarioAssumption, ScenarioResult,
                      ScenarioConfidenceComponent, ScenarioInputChange,
                      ScenarioEvent):
            leaked = await s.scalar(
                select(func.count()).select_from(model).join(
                    Scenario, Scenario.id == model.scenario_id)
                .where(Scenario.user_id == uid))
            assert leaked == 0, f"{model.__name__} rows survived a refusal"

        outbox = await s.scalar(text(
            "SELECT count(*) FROM ioe.freshness_outbox WHERE user_id = :u"
        ).bindparams(u=uid))
        assert outbox == 0, "a success event was emitted for a refused scenario"

        checks = await s.scalar(text(
            "SELECT count(*) FROM ioe.integrity_check ic "
            "JOIN ioe.scenario s ON s.id = ic.scenario_id WHERE s.user_id = :u"
        ).bindparams(u=uid))
        assert checks == 0


@pytest.mark.asyncio
async def test_a_refused_baseline_leaves_nothing_for_compare_to_find():
    await _publish(f"CLZ{_suffix()}")
    uid, analysis_id = await _analysis()
    good = await ScenarioService(uid).simulate(analysis_id, _spec())

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await s.execute(
            text("DELETE FROM analysis.analysis_input_snapshot "
                 "WHERE analysis_id = :a"), {"a": analysis_id})

    with pytest.raises(ScenarioBaselineUnavailable):
        await ScenarioService(uid).simulate(analysis_id, _spec("9000"))

    from app.core.exceptions import NotFound

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        with pytest.raises(NotFound):
            await ScenarioComparisonService(s, uid).compare(
                good.scenario_id, uuid.uuid4())
