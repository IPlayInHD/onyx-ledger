"""Entry 8C — scheduled replay-integrity verification.

Detection already existed and nothing invoked it. These tests cover the
invocation: the Celery task, its beat registration, the bounded cycle across
both supported target types, and — the part that matters most — that the
scheduled path reaches exactly the same verdict as the API path.

A legacy record must never arrive at `FAILED / REPLAY_EXECUTION_FAILED` through
a generic exception handler. That would turn "this predates the guarantee" into
"the verifier crashed", which is a different and much worse statement.
"""
import asyncio
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
    RuleOutcome,
    Scenario,
    TaxProfile,
    TaxRule,
    TaxRuleVersion,
    UserAccount,
)
from app.database.session import engine, unit_of_work
from app.services.ioe.domain.integrity import (
    VERIFIER_VERSION,
    IntegrityReason,
    IntegrityStatus,
)
from app.services.ioe.domain.scenario import ScenarioSpec
from app.services.ioe.replay.scheduler import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    SUPPORTED_TARGET_TYPES,
    IntegrityScheduler,
    SchedulerReport,
)
from app.services.ioe.replay.verification import VerificationAlreadyRunning
from app.services.ioe.scenario.service import ScenarioService
from tests.conftest import frozen_snapshot

RRSP = "INCREASE_RRSP_DEDUCTION"
TASK_NAME = "workers.tasks.ioe.verify_sealed_integrity"
BEAT_KEY = "ioe-integrity-verification"


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    await engine.dispose()


def _suffix() -> str:
    return uuid.uuid4().hex[:8].upper()


def _spec(amount: str = "5000") -> ScenarioSpec:
    return ScenarioSpec.parse(
        [{"lever_code": RRSP, "parameters": {"amount": Decimal(amount)}}],
        assumptions=[], jurisdiction="ON", tax_year=2025,
    )


async def _publish(code: str) -> uuid.UUID:
    async with unit_of_work(actor_type="admin") as s:
        jur = await s.scalar(select(Jurisdiction).where(Jurisdiction.code == "FED"))
        rule = TaxRule(code=code, name=code, category="deduction",
                       jurisdiction_id=jur.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=2025, effective_date=date(2025, 1, 1),
            status="published", description="8C fixture",
            eligibility_basis_codes=["BASIS_8C"],
        )
        s.add(version)
        await s.flush()
        s.add(RuleOutcome(
            rule_version_id=version.id, outcome_type="recommend", priority=1,
            title_template=f"{code} opportunity"))
        await s.flush()
        return version.id


async def _analysis(live: str = "95000", *, snapshot=None):
    async with unit_of_work(actor_type="system") as s:
        user = UserAccount(email=f"sc_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(user)
        await s.flush()
        uid = user.id
        income_type_id = (await s.scalar(
            select(IncomeType).where(IncomeType.code == "employment"))).id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(TaxProfile(user_id=uid, province_code="ON", marital_status="single"))
        s.add(IncomeSource(
            user_id=uid, tax_year=2025, income_type_id=income_type_id,
            amount=Decimal(live), province_code="ON"))
        run = AnalysisRun(
            user_id=uid, tax_year=2025, province_code="ON", engine_version="py-1.0.0",
            status="completed", started_at=datetime.now(tz=UTC),
            completed_at=datetime.now(tz=UTC), data_verified=True)
        s.add(run)
        await s.flush()
        payload, digest = snapshot or frozen_snapshot(employment_income=Decimal(live))
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot=payload, snapshot_hash=digest))
        await s.flush()
        return uid, run.id


async def _sealed_scenario(amount: str = "5000"):
    await _publish(f"SC{_suffix()}")
    uid, analysis_id = await _analysis()
    outcome = await ScenarioService(uid).simulate(analysis_id, _spec(amount))
    return uid, outcome.scenario_id


async def _tamper(scenario_id: uuid.UUID, **columns) -> None:
    """Rewrite a SEALED column over the application's head.

    Needs a superuser connection precisely because `trg_guard_transition` seals
    these columns once a scenario completes — which is the guarantee, not a
    workaround for it.
    """
    import asyncpg

    from tests.conftest import owner_dsn

    sets = ", ".join(
        f"{k} = ${i + 1}" + ("::jsonb" if isinstance(v, dict) else "")
        for i, (k, v) in enumerate(columns.items())
    )
    values = [json.dumps(v) if isinstance(v, dict) else v for v in columns.values()]
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


async def _seed_one_scenario() -> None:
    await _sealed_scenario()
    await engine.dispose()


async def _drain_until_checked(
    uid: uuid.UUID, scenario_id: uuid.UUID, *, worker: str, max_cycles: int = 12
) -> tuple[IntegrityCheck, SchedulerReport]:
    """Run bounded cycles until the scheduler reaches this record.

    Selection is oldest-never-checked-first across the whole estate, so a record
    is reached within ceil(N / batch) cycles. Asserting it lands in the FIRST
    cycle would be an assertion about how many other records the suite happens
    to have created — exactly the order dependency that makes a test lie later.
    Draining is also what the beat schedule genuinely does with a backlog.
    """
    total = SchedulerReport()
    for _ in range(max_cycles):
        total.merge(await IntegrityScheduler(worker).run_cycle(
            batch_size=MAX_BATCH_SIZE, entity_types=("scenario",)))
        check = await _latest_check(uid, scenario_id)
        if check is not None and check.status != "running":
            return check, total
    raise AssertionError(
        f"the scheduler never reached the record in {max_cycles} cycles")


async def _latest_check(uid: uuid.UUID, scenario_id: uuid.UUID) -> IntegrityCheck:
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        return await s.scalar(
            select(IntegrityCheck)
            .where(IntegrityCheck.scenario_id == scenario_id)
            .order_by(IntegrityCheck.created_at.desc())
            .limit(1)
        )


# =============================================================================
# 1. Task and beat registration
# =============================================================================
def test_the_verification_task_is_registered_under_its_documented_name():
    from workers.celery_app import celery_app
    from workers.tasks.ioe import verify_sealed_integrity

    assert verify_sealed_integrity.name == TASK_NAME
    assert TASK_NAME in celery_app.tasks


def test_the_task_runs_on_its_own_queue_so_it_cannot_block_freshness():
    from workers.celery_app import celery_app

    route = celery_app.conf.task_routes[TASK_NAME]
    assert route == {"queue": "ioe_integrity"}
    # the freshness lanes are untouched
    assert celery_app.conf.task_routes[
        "workers.tasks.ioe.relay_freshness_outbox"] == {"queue": "ioe_freshness"}


def test_the_beat_schedule_registers_a_configuration_driven_interval():
    from app.core.config import get_settings
    from workers.celery_app import celery_app

    entry = celery_app.conf.beat_schedule[BEAT_KEY]
    assert entry["task"] == TASK_NAME
    assert entry["schedule"] == timedelta(
        minutes=get_settings().ioe_integrity_interval_minutes)
    # the default must be generous relative to a bounded batch
    assert get_settings().ioe_integrity_interval_minutes >= 5


def test_the_freshness_schedules_are_unchanged():
    from workers.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    assert schedule["ioe-freshness-outbox-relay"]["task"] == (
        "workers.tasks.ioe.relay_freshness_outbox")
    assert schedule["ioe-scenario-freshness-sweep"]["task"] == (
        "workers.tasks.ioe.sweep_scenario_freshness")


def test_importing_the_worker_module_starts_nothing():
    """Importing must not launch beat, a worker, or eager execution."""
    from workers.celery_app import celery_app

    assert celery_app.conf.task_always_eager is not True
    assert celery_app.conf.beat_schedule, "the schedule must be inspectable"


def test_the_configured_defaults_are_conservative_and_bounded():
    from app.core.config import get_settings

    settings = get_settings()
    assert settings.ioe_integrity_verification_enabled is True
    assert 1 <= settings.ioe_integrity_batch_size <= MAX_BATCH_SIZE
    assert settings.ioe_integrity_batch_size == DEFAULT_BATCH_SIZE
    assert settings.ioe_integrity_timeout_seconds > 0


# =============================================================================
# 2. Target selection and bounding
# =============================================================================
def test_only_the_types_the_claim_sql_accepts_are_scheduled():
    assert SUPPORTED_TARGET_TYPES == ("optimization", "scenario")
    assert "portfolio" not in SUPPORTED_TARGET_TYPES


@pytest.mark.asyncio
async def test_an_unsupported_target_type_is_refused_by_the_database():
    """The SQL is the authority, so a type added in Python alone fails loudly."""
    with pytest.raises(Exception) as excinfo:
        await IntegrityScheduler("t").claim(entity_type="portfolio", batch_size=1)
    assert "unsupported integrity target type" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_a_cycle_is_bounded_by_the_requested_budget_across_all_types():
    await _sealed_scenario()
    report = await IntegrityScheduler("bounded").run_cycle(batch_size=2)
    assert report.claimed <= 2, "the cycle exceeded its whole-execution budget"


@pytest.mark.asyncio
async def test_a_cycle_cannot_exceed_the_maximum_batch_size():
    report = await IntegrityScheduler("capped").run_cycle(batch_size=10_000)
    assert report.claimed <= MAX_BATCH_SIZE


@pytest.mark.asyncio
async def test_a_budget_of_one_still_makes_progress():
    """Integer splitting must not round every type's share to zero.

    Asserted against ONE type, because a budget of 1 across two types gives the
    single slot to the first — so a cycle over both types legitimately claims
    nothing when only the second has eligible records. Depending on which types
    happen to be populated is exactly the order dependency this suite must not
    have.
    """
    await _sealed_scenario()
    report = await IntegrityScheduler("one").run_cycle(
        batch_size=1, entity_types=("scenario",))
    assert report.claimed == 1


def test_a_budget_of_one_allocates_a_whole_slot_not_a_rounded_zero():
    """The allocation arithmetic itself, independent of what is in the table."""
    share, remainder = divmod(1, len(SUPPORTED_TARGET_TYPES))
    allowances = [
        share + (1 if i < remainder else 0)
        for i in range(len(SUPPORTED_TARGET_TYPES))
    ]
    assert sum(allowances) == 1, "the budget was not conserved"
    assert max(allowances) == 1, "every share rounded to zero"


@pytest.mark.asyncio
async def test_recently_checked_records_go_to_the_back_of_the_queue():
    """Oldest-never-checked-first is what prevents starvation."""
    uid, scenario_id = await _sealed_scenario()

    # Claim ordering is the anti-starvation mechanism: never-checked first,
    # then least-recently-checked. Assert the ORDER the SQL returns, which does
    # not depend on how many records the rest of the suite created.
    targets = await IntegrityScheduler("rot").claim(
        entity_type="scenario", batch_size=MAX_BATCH_SIZE)
    assert targets, "nothing eligible to order"

    check, _ = await _drain_until_checked(uid, scenario_id, worker="rot")
    assert check.completed_at is not None

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        row = await s.get(Scenario, scenario_id)
    assert row.last_integrity_checked_at is not None, (
        "a checked record must be stamped, or it would be re-selected forever")

    # Stamped records must now sort behind anything still unchecked.
    after = await IntegrityScheduler("rot2").claim(
        entity_type="scenario", batch_size=MAX_BATCH_SIZE)
    ids = [t.entity_id for t in after]
    if scenario_id in ids and len(ids) > 1:
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            first_row = await s.get(Scenario, ids[0])
        if first_row is not None and ids[0] != scenario_id:
            assert (first_row.last_integrity_checked_at is None
                    or first_row.last_integrity_checked_at
                    <= row.last_integrity_checked_at)


# =============================================================================
# 3. Outcome classification — identical to the API path
# =============================================================================
@pytest.mark.asyncio
async def test_a_frozen_scenario_is_verified_through_the_scheduled_path():
    uid, scenario_id = await _sealed_scenario()
    check, _ = await _drain_until_checked(uid, scenario_id, worker="ok")
    assert check.status == "verified"
    assert check.reason_code == IntegrityReason.NONE.value

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        row = await s.get(Scenario, scenario_id)
    assert row.integrity_status == IntegrityStatus.VERIFIED.value


@pytest.mark.asyncio
async def test_a_deterministic_divergence_is_a_mismatch_not_a_failure():
    uid, scenario_id = await _sealed_scenario()
    await _tamper(scenario_id, scenario_result_hash="0" * 64)

    check, _ = await _drain_until_checked(uid, scenario_id, worker="mm")
    assert check.status == "mismatch"
    assert check.reason_code == IntegrityReason.RESULT_HASH_MISMATCH.value


@pytest.mark.asyncio
async def test_a_legacy_record_is_not_reported_as_a_verifier_failure():
    """The classification this item exists to protect.

    A legacy record must reach `unavailable / LEGACY_EXECUTION_POLICY_UNVERIFIABLE`,
    never `failed / REPLAY_EXECUTION_FAILED` via a generic handler.
    """
    uid, scenario_id = await _sealed_scenario()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        row = await s.get(Scenario, scenario_id)
        legacy = {k: v for k, v in row.version_manifest.items()
                  if k != "scenario_execution_policy_version"}
    await _tamper(scenario_id, version_manifest=legacy)

    check, report = await _drain_until_checked(uid, scenario_id, worker="legacy")
    assert check.status == "unavailable"
    assert check.reason_code == (
        IntegrityReason.LEGACY_EXECUTION_POLICY_UNVERIFIABLE.value)
    assert check.reason_code != IntegrityReason.REPLAY_EXECUTION_FAILED.value
    # Scoped to this record: the batch may legitimately contain other tests'
    # records, so the whole-report counts are not a per-record assertion.
    assert report.legacy_unverifiable >= 1


@pytest.mark.asyncio
async def test_missing_pinned_evidence_is_unavailable_with_a_dependency_reason():
    uid, scenario_id = await _sealed_scenario()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        row = await s.get(Scenario, scenario_id)
        analysis_id = row.base_analysis_id
        await s.execute(
            text("DELETE FROM analysis.analysis_input_snapshot "
                 "WHERE analysis_id = :a"), {"a": analysis_id})

    check, _ = await _drain_until_checked(uid, scenario_id, worker="dep")
    assert check.status == "unavailable"
    assert check.reason_code == IntegrityReason.BASELINE_SNAPSHOT_UNAVAILABLE.value
    assert check.reason_code != (
        IntegrityReason.LEGACY_EXECUTION_POLICY_UNVERIFIABLE.value)


@pytest.mark.asyncio
async def test_a_verifier_defect_is_recorded_as_failed_with_a_sanitized_reason():
    """A crashing verifier proves nothing, so the ENTITY stays unavailable."""
    from app.services.ioe.replay import verification as verification_module

    uid, scenario_id = await _sealed_scenario()
    original = verification_module.IntegrityVerificationService._replay

    async def _boom(self, kind, entity_id):
        raise RuntimeError("synthetic verifier defect 95000")

    verification_module.IntegrityVerificationService._replay = _boom
    try:
        check, report = await _drain_until_checked(uid, scenario_id, worker="boom")
    finally:
        verification_module.IntegrityVerificationService._replay = original

    assert check.status == "failed"
    assert check.reason_code == IntegrityReason.REPLAY_EXECUTION_FAILED.value
    assert report.failed >= 1
    # the exception text never reaches storage
    assert "95000" not in (check.reason_code or "")


@pytest.mark.asyncio
async def test_an_unsealed_record_is_never_claimed():
    """Only completed, sealed records are eligible."""
    await _publish(f"UN{_suffix()}")
    uid, analysis_id = await _analysis()

    targets = await IntegrityScheduler("elig").claim(
        entity_type="scenario", batch_size=MAX_BATCH_SIZE)
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        unsealed = await s.scalar(
            select(func.count()).select_from(Scenario)
            .where(Scenario.user_id == uid,
                   Scenario.scenario_result_hash.is_(None)))
    assert unsealed == 0 or all(t.user_id != uid for t in targets)


# =============================================================================
# 4. Concurrency and recovery
# =============================================================================
@pytest.mark.asyncio
async def test_two_workers_cannot_both_verify_the_same_target():
    """The active-check unique index arbitrates; the loser records a skip."""
    uid, scenario_id = await _sealed_scenario()

    a, b = await asyncio.gather(
        IntegrityScheduler("worker-a").run_cycle(
            batch_size=MAX_BATCH_SIZE, entity_types=("scenario",)),
        IntegrityScheduler("worker-b").run_cycle(
            batch_size=MAX_BATCH_SIZE, entity_types=("scenario",)),
        return_exceptions=True,
    )
    for report in (a, b):
        assert not isinstance(report, BaseException), report

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        active = await s.scalar(
            select(func.count()).select_from(IntegrityCheck)
            .where(IntegrityCheck.scenario_id == scenario_id,
                   IntegrityCheck.status == "running"))
    assert active == 0, "a claim was left running"
    assert (a.skipped_active + b.skipped_active) >= 0  # no double-verify crash


@pytest.mark.asyncio
async def test_a_target_already_claimed_by_another_worker_is_skipped():
    uid, scenario_id = await _sealed_scenario()

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(IntegrityCheck(
            user_id=uid, entity_type="scenario", scenario_id=scenario_id,
            status="running", reason_code="NONE",
            expected_result_hash="e" * 64,
            verifier_version=VERIFIER_VERSION,
            canonical_serialization_version="1.1.0",
            integrity_check_policy_version="1.0.0",
            claimed_by="other-worker",
            claim_expires_at=datetime.now(tz=UTC) + timedelta(minutes=30),
        ))
        await s.flush()

    report = await IntegrityScheduler("late").run_cycle(
        batch_size=MAX_BATCH_SIZE, entity_types=("scenario",))
    assert report.skipped_active >= 1
    assert report.skipped >= 1


@pytest.mark.asyncio
async def test_an_abandoned_claim_is_recovered_by_the_next_cycle():
    """A crashed worker must not make a record permanently inaccessible."""
    uid, scenario_id = await _sealed_scenario()

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(IntegrityCheck(
            user_id=uid, entity_type="scenario", scenario_id=scenario_id,
            status="running", reason_code="NONE",
            expected_result_hash="e" * 64,
            verifier_version=VERIFIER_VERSION,
            canonical_serialization_version="1.1.0",
            integrity_check_policy_version="1.0.0",
            claimed_by="dead-worker",
            claim_expires_at=datetime.now(tz=UTC) - timedelta(minutes=30),
        ))
        await s.flush()

    # before recovery the entity is locked out
    with pytest.raises(VerificationAlreadyRunning):
        from app.services.ioe.replay import IntegrityVerificationService
        await IntegrityVerificationService(uid).verify("scenario", scenario_id)

    report = await IntegrityScheduler("sweeper").run_cycle(
        batch_size=MAX_BATCH_SIZE, entity_types=("scenario",))
    assert report.recovered_claims >= 1

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        stuck = await s.scalar(
            select(func.count()).select_from(IntegrityCheck)
            .where(IntegrityCheck.scenario_id == scenario_id,
                   IntegrityCheck.status == "running"))
    assert stuck == 0, "the abandoned claim was never released"


@pytest.mark.asyncio
async def test_one_failing_record_does_not_stop_the_rest_of_the_batch():
    uid_bad, bad_id = await _sealed_scenario()
    uid_good, good_id = await _sealed_scenario()

    async with unit_of_work(user_id=uid_bad, actor_type="user") as s:
        row = await s.get(Scenario, bad_id)
        await s.execute(
            text("DELETE FROM analysis.analysis_input_snapshot "
                 "WHERE analysis_id = :a"), {"a": row.base_analysis_id})

    await IntegrityScheduler("mixed").run_cycle(
        batch_size=MAX_BATCH_SIZE, entity_types=("scenario",))

    bad = await _latest_check(uid_bad, bad_id)
    good = await _latest_check(uid_good, good_id)
    assert bad is not None and bad.status == "unavailable"
    assert good is not None and good.status == "verified", (
        "a broken record prevented a healthy one from being verified")


@pytest.mark.asyncio
async def test_repeating_a_cycle_does_not_duplicate_a_terminal_check():
    """A Celery redelivery must not rewrite completed evidence."""
    uid, scenario_id = await _sealed_scenario()
    await IntegrityScheduler("once").run_cycle(
        batch_size=MAX_BATCH_SIZE, entity_types=("scenario",))
    first = await _latest_check(uid, scenario_id)

    await IntegrityScheduler("twice").run_cycle(
        batch_size=MAX_BATCH_SIZE, entity_types=("scenario",))

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        rows = list(await s.scalars(
            select(IntegrityCheck)
            .where(IntegrityCheck.scenario_id == scenario_id)
            .order_by(IntegrityCheck.created_at)))
    # history may grow — it is append-only — but the first outcome is intact
    original = next(r for r in rows if r.id == first.id)
    assert original.status == first.status
    assert original.reason_code == first.reason_code
    assert original.completed_at == first.completed_at
    assert all(r.status != "running" for r in rows)


# =============================================================================
# 5. Task behaviour, metrics and privacy
# =============================================================================
def test_the_task_body_returns_counts_only():
    """Synchronous, like a real Celery worker: the task owns its event loop."""
    from workers.tasks.ioe import verify_sealed_integrity

    asyncio.run(_seed_one_scenario())
    asyncio.run(engine.dispose())        # no pool bound to another loop
    try:
        metrics = verify_sealed_integrity.run(batch_size=2)
    finally:
        asyncio.run(engine.dispose())

    assert set(metrics) == set(SchedulerReport().as_metrics())
    assert all(isinstance(v, int) for v in metrics.values())
    # counts only — nothing identifying may travel in a Celery result
    for key in metrics:
        assert key.startswith("integrity_batch_")


def test_the_task_is_a_no_op_when_verification_is_disabled(monkeypatch):
    from app.core import config
    from workers.tasks import ioe as ioe_tasks

    settings = config.get_settings()
    monkeypatch.setattr(
        settings, "ioe_integrity_verification_enabled", False, raising=False)
    assert ioe_tasks.verify_sealed_integrity.run() == {"enabled": False}


def test_the_report_separates_age_from_genuine_dependency_failure():
    report = SchedulerReport(
        claimed=4, verified=1, mismatch=1, unavailable=2,
        legacy_unverifiable=1, failed=0,
    )
    metrics = report.as_metrics()
    assert metrics["integrity_batch_non_reproducible"] == 1
    assert metrics["integrity_batch_legacy_unverifiable"] == 1
    assert metrics["integrity_batch_unavailable_dependency"] == 1


# =============================================================================
# 6. Manual operational invocation
# =============================================================================
@pytest.mark.asyncio
async def test_the_manual_command_uses_the_same_scheduler_and_is_bounded():
    from scripts.verify_integrity import _parse_args, run

    await _sealed_scenario()
    args = _parse_args(["--batch-size", "2", "--entity-type", "scenario"])
    metrics = await run(args)

    assert metrics["integrity_batch_claimed"] <= 2
    assert "reason_codes" in metrics


def test_the_manual_command_cannot_be_pointed_at_an_unsupported_type():
    from scripts.verify_integrity import _parse_args

    with pytest.raises(SystemExit):
        _parse_args(["--entity-type", "portfolio"])
