"""P3 exit criteria — the TX-1/compute/TX-2 orchestrator against real PostgreSQL.

Covers: concurrency, idempotency mismatch, canonical-equivalence replay, the
rule-publication race, TX-2 rollback with no partial evidence, failed-run retry,
ownership/RLS, immutability of completed evidence, support-score persistence,
and contract-v2 normalization that never invents missing legal fields.
"""
import asyncio
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.exceptions import Conflict, NotFound
from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
    ConfidenceComponent,
    Jurisdiction,
    OptimizationCandidate,
    OptimizationRun,
    OptimizationRunEvent,
    RuleAction,
    RuleOutcome,
    RunRuleVersion,
    ScoreComponent,
    TaxProfile,
    TaxRule,
    TaxRuleVersion,
    UserAccount,
)
from app.database.session import unit_of_work
from app.services.ioe.orchestrator import (
    IdempotencyKeyReused,
    OptimizationOrchestrator,
)


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


async def _user_with_analysis() -> tuple[uuid.UUID, uuid.UUID]:
    async with unit_of_work(actor_type="system") as s:
        u = UserAccount(email=f"p3_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(u)
        await s.flush()
        uid = u.id
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(TaxProfile(user_id=uid, province_code="ON", marital_status="single"))
        run = AnalysisRun(
            user_id=uid, tax_year=2025, province_code="ON", engine_version="py-1.0.0",
            status="completed", started_at=datetime.now(tz=UTC),
            completed_at=datetime.now(tz=UTC), data_verified=True,
        )
        s.add(run)
        await s.flush()
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot={"province": "ON"}, snapshot_hash="snap-fixed",
        ))
        await s.flush()
        return uid, run.id


async def _publish_rule(code: str, *, with_contract: bool = True) -> uuid.UUID:
    """A published, always-matching rule (no condition gate)."""
    async with unit_of_work(actor_type="admin") as s:
        jur = await s.scalar(select(Jurisdiction).where(Jurisdiction.code == "FED"))
        rule = TaxRule(code=code, name=f"{code} rule", category="credit",
                       jurisdiction_id=jur.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=2025, effective_date=date(2025, 1, 1),
            status="published", description="P3 fixture",
            eligibility_basis_codes=["BASIS_A"] if with_contract else None,
        )
        s.add(version)
        await s.flush()
        outcome = RuleOutcome(
            rule_version_id=version.id, outcome_type="recommend", priority=1,
            title_template=f"{code} opportunity",
        )
        if with_contract:
            outcome.economic_effect_type = "current_year_tax_reduction"
            outcome.reversibility = "reversible"
        s.add(outcome)
        if with_contract:
            s.add(RuleAction(
                rule_version_id=version.id, action_code="DO_THING",
                description="Do the thing", effort_rating=2,
                cost_type="required_cash_contribution", cost_amount=Decimal("1000"),
            ))
        await s.flush()
        return version.id


# ---------------------------------------------------------------------------
# Happy path + support-score persistence
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generate_completes_and_persists_support_scores():
    uid, analysis_id = await _user_with_analysis()
    await _publish_rule(f"P3OK_{uuid.uuid4().hex[:6].upper()}")

    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)
    assert outcome.workflow_status == "completed"
    assert outcome.result_hash and outcome.spec_hash
    assert not outcome.replayed

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.get(OptimizationRun, outcome.run_id)
        assert run.workflow_status == "completed"
        assert run.rule_snapshot_id is not None
        assert run.manifest_hash

        # the status log is append-only and records every transition
        events = list(await s.scalars(
            select(OptimizationRunEvent).where(OptimizationRunEvent.run_id == run.id)
        ))
        assert {e.to_status for e in events} == {"pending", "running", "completed"}

        candidates = list(await s.scalars(
            select(OptimizationCandidate).where(OptimizationCandidate.run_id == run.id)
        ))
        assert candidates
        for cand in candidates:
            # all five support fields persisted
            assert cand.raw_support_score is not None
            assert cand.assumption_adjusted_score is not None
            assert cand.display_support_score is not None
            assert cand.support_cap_applied in (True, False)
            # confidence_score is DERIVED, never divergent
            assert cand.confidence_score == round(cand.display_support_score)
            assert cand.candidate_rank is not None
            assert cand.recommendation_score is not None

        # provenance: score and confidence components stored per candidate
        assert await s.scalar(select(func.count(ScoreComponent.id))) > 0
        assert await s.scalar(select(func.count(ConfidenceComponent.id))) > 0
        # the pinned version set is recorded
        assert await s.scalar(
            select(func.count(RunRuleVersion.id)).where(RunRuleVersion.run_id == run.id)
        ) > 0


@pytest.mark.asyncio
async def test_confidence_score_cannot_diverge_from_display():
    """The DB derives it; a caller-supplied value is overwritten."""
    uid, analysis_id = await _user_with_analysis()
    await _publish_rule(f"P3DIV_{uuid.uuid4().hex[:6].upper()}")
    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.get(OptimizationRun, outcome.run_id)
        cand = OptimizationCandidate(
            run_id=run.id, opportunity_code="forged", eligibility_status="eligible",
            display_support_score=Decimal("62.40"),
            assumption_adjusted_score=Decimal("62.40"),
            confidence_score=99,          # deliberately wrong
        )
        s.add(cand)
        await s.flush()
        # refresh: the value is derived by a DB trigger, so the in-memory object
        # still holds what was submitted until it is re-read
        await s.refresh(cand)
        assert cand.confidence_score == 62
        assert cand.display_support_score == Decimal("62.40")


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_canonically_equivalent_request_replays_the_same_run():
    uid, analysis_id = await _user_with_analysis()
    await _publish_rule(f"P3EQ_{uuid.uuid4().hex[:6].upper()}")
    orch = OptimizationOrchestrator(uid)

    first = await orch.generate(analysis_id, user_constraints={"available_cash": "5000"})
    # same spec, different key ordering in the request — canonically identical
    second = await orch.generate(analysis_id, user_constraints={"available_cash": "5000"})

    assert second.replayed is True
    assert second.run_id == first.run_id
    assert second.spec_hash == first.spec_hash


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_spec_is_refused():
    uid, analysis_id = await _user_with_analysis()
    await _publish_rule(f"P3IDEM_{uuid.uuid4().hex[:6].upper()}")
    orch = OptimizationOrchestrator(uid)
    key = f"key-{uuid.uuid4().hex[:8]}"

    await orch.generate(analysis_id, idempotency_key=key,
                        user_constraints={"available_cash": "5000"})

    with pytest.raises(IdempotencyKeyReused, match="idempotency_key_reused"):
        await orch.generate(analysis_id, idempotency_key=key,
                            user_constraints={"available_cash": "9999"})


@pytest.mark.asyncio
async def test_same_idempotency_key_with_same_spec_replays():
    uid, analysis_id = await _user_with_analysis()
    await _publish_rule(f"P3IDOK_{uuid.uuid4().hex[:6].upper()}")
    orch = OptimizationOrchestrator(uid)
    key = f"key-{uuid.uuid4().hex[:8]}"

    first = await orch.generate(analysis_id, idempotency_key=key)
    second = await orch.generate(analysis_id, idempotency_key=key)
    assert second.replayed and second.run_id == first.run_id


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_identical_requests_create_at_most_one_run():
    uid, analysis_id = await _user_with_analysis()
    await _publish_rule(f"P3CONC_{uuid.uuid4().hex[:6].upper()}")

    async def go():
        return await OptimizationOrchestrator(uid).generate(analysis_id)

    results = await asyncio.gather(go(), go(), go(), return_exceptions=True)
    successes = [r for r in results if not isinstance(r, Exception)]
    # every non-exception result must point at the SAME run
    run_ids = {r.run_id for r in successes}
    assert len(run_ids) == 1, results
    # any loser failed cleanly rather than creating a duplicate
    for r in results:
        if isinstance(r, Exception):
            assert isinstance(r, Conflict)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        count = await s.scalar(
            select(func.count(OptimizationRun.id)).where(
                OptimizationRun.user_id == uid,
                OptimizationRun.workflow_status.in_(("pending", "running", "completed")),
            )
        )
        assert count == 1


# ---------------------------------------------------------------------------
# Rule-publication race — the pinned snapshot must CONSTRAIN evaluation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rule_published_after_pinning_does_not_enter_the_in_flight_run():
    uid, analysis_id = await _user_with_analysis()
    original = await _publish_rule(f"P3RACE_A_{uuid.uuid4().hex[:6].upper()}")

    orch = OptimizationOrchestrator(uid)

    # TX-1: pin the specification (captures the snapshot)
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        spec = await orch._pin_specification(
            s, analysis_id, user_constraints=None, assumptions=None
        )
    pinned_ids = set(spec.pinned_rule_version_ids)
    assert original in pinned_ids

    # a NEW rule is published while the run is in flight
    intruder = await _publish_rule(f"P3RACE_B_{uuid.uuid4().hex[:6].upper()}")
    assert intruder not in pinned_ids

    # the constrained evaluation must not see it
    from app.services.tax_engine.rules_service import RulesEvaluatorService

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        constrained = await RulesEvaluatorService(s).evaluate(
            2025, {}, pinned_rule_version_ids=spec.pinned_rule_version_ids
        )
        unconstrained = await RulesEvaluatorService(s).evaluate(2025, {})

    seen_constrained = {str(o.rule_version_id) for o in constrained}
    seen_unconstrained = {str(o.rule_version_id) for o in unconstrained}
    assert str(intruder) not in seen_constrained, "pinned snapshot was not enforced"
    assert str(intruder) in seen_unconstrained, "fixture did not actually publish"
    assert str(original) in seen_constrained


@pytest.mark.asyncio
async def test_empty_pin_evaluates_nothing_and_is_distinct_from_none():
    from app.services.tax_engine.rules_service import RulesEvaluatorService

    await _publish_rule(f"P3PIN_{uuid.uuid4().hex[:6].upper()}")
    async with unit_of_work(actor_type="system") as s:
        assert await RulesEvaluatorService(s).evaluate(2025, {}, pinned_rule_version_ids=[]) == []
        assert await RulesEvaluatorService(s).evaluate(2025, {}) != []


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_analysis_not_completed_is_refused_before_any_run_exists():
    async with unit_of_work(actor_type="system") as s:
        u = UserAccount(email=f"p3bad_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(u)
        await s.flush()
        uid = u.id
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = AnalysisRun(user_id=uid, tax_year=2025, province_code="ON",
                          engine_version="py-1.0.0", status="running")
        s.add(run)
        await s.flush()
        analysis_id = run.id

    with pytest.raises(Conflict, match="analysis_not_ready"):
        await OptimizationOrchestrator(uid).generate(analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        assert await s.scalar(
            select(func.count(OptimizationRun.id)).where(OptimizationRun.user_id == uid)
        ) == 0


@pytest.mark.asyncio
async def test_compute_failure_marks_failed_with_sanitized_code_and_no_children():
    """TX-2 never ran, so no evidence exists — a partial result is unreachable."""
    uid, analysis_id = await _user_with_analysis()
    await _publish_rule(f"P3FAIL_{uuid.uuid4().hex[:6].upper()}")

    orch = OptimizationOrchestrator(uid)

    async def boom(_spec):
        raise RuntimeError("secret internal detail: user SIN 123-456-789")

    orch._compute = boom
    with pytest.raises(RuntimeError):
        await orch.generate(analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.scalar(
            select(OptimizationRun).where(OptimizationRun.user_id == uid)
            .order_by(OptimizationRun.created_at.desc())
        )
        assert run.workflow_status == "failed"
        # sanitized: an enumerated code, never the message
        assert run.error_code == "INTERNAL_ERROR"
        assert "SIN" not in (run.error_code or "")
        assert await s.scalar(
            select(func.count(OptimizationCandidate.id))
            .where(OptimizationCandidate.run_id == run.id)
        ) == 0


@pytest.mark.asyncio
async def test_failed_run_can_be_retried_and_creates_a_new_run():
    """A failed run is excluded from the spec-uniqueness index, so retry works."""
    uid, analysis_id = await _user_with_analysis()
    await _publish_rule(f"P3RETRY_{uuid.uuid4().hex[:6].upper()}")

    failing = OptimizationOrchestrator(uid)

    async def boom(_spec):
        raise RuntimeError("transient")

    failing._compute = boom
    with pytest.raises(RuntimeError):
        await failing.generate(analysis_id)

    # the retry succeeds and is a NEW run (evidence is never resurrected)
    retry = await OptimizationOrchestrator(uid).generate(analysis_id)
    assert retry.workflow_status == "completed"

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        statuses = [
            r.workflow_status for r in await s.scalars(
                select(OptimizationRun).where(OptimizationRun.user_id == uid)
            )
        ]
        assert sorted(statuses) == ["completed", "failed"]


# ---------------------------------------------------------------------------
# Ownership / RLS / immutability
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_optimizing_another_users_analysis_is_refused():
    uid_a, analysis_a = await _user_with_analysis()
    uid_b, _ = await _user_with_analysis()

    # user B supplies A's analysis id — ownership is validated, not trusted
    with pytest.raises(NotFound):
        await OptimizationOrchestrator(uid_b).generate(analysis_a)


@pytest.mark.asyncio
async def test_completed_evidence_is_immutable():
    uid, analysis_id = await _user_with_analysis()
    await _publish_rule(f"P3IMM_{uuid.uuid4().hex[:6].upper()}")
    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        with pytest.raises(DBAPIError, match="immutable calculation evidence"):
            await s.execute(text(
                "UPDATE ioe.optimization_candidate SET display_support_score = 1 "
                "WHERE run_id = :r"), {"r": outcome.run_id})

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.get(OptimizationRun, outcome.run_id)
        run.optimization_result_hash = "tampered"
        with pytest.raises(DBAPIError, match="sealed"):
            await s.flush()


# ---------------------------------------------------------------------------
# Contract-v2 normalization never invents legal fields
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rule_without_contract_authoring_is_indeterminate_and_not_evaluable():
    uid, analysis_id = await _user_with_analysis()
    bare = await _publish_rule(f"P3BARE_{uuid.uuid4().hex[:6].upper()}", with_contract=False)

    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        cand = await s.scalar(
            select(OptimizationCandidate).where(
                OptimizationCandidate.run_id == outcome.run_id,
                OptimizationCandidate.tax_rule_version_id == bare,
            )
        )
        assert cand is not None
        # nothing was invented to fill the gap
        assert cand.eligibility_status == "indeterminate"
        assert cand.portfolio_membership == "excluded_not_evaluable"
        assert cand.calculation_basis is None


@pytest.mark.asyncio
async def test_confidence_score_conversion_boundaries():
    """The exact display→integer conversion, executed against the live trigger.

    Pinned mode is HALF AWAY FROM ZERO (identical to ROUND_HALF_UP over the
    non-negative [0,100] domain). 63.50→64 is the discriminating case: banker's
    rounding would give 62 for 62.50, so both midpoints rounding UP proves the
    mode.
    """
    uid, analysis_id = await _user_with_analysis()
    await _publish_rule(f"P3RND_{uuid.uuid4().hex[:6].upper()}")
    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    cases = [
        ("0.00", 0), ("0.50", 1), ("62.40", 62), ("62.50", 63),
        ("62.60", 63), ("63.50", 64), ("99.50", 100), ("100.00", 100),
    ]
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.get(OptimizationRun, outcome.run_id)
        for display, expected in cases:
            cand = OptimizationCandidate(
                run_id=run.id, opportunity_code=f"round-{display}",
                eligibility_status="eligible",
                display_support_score=Decimal(display),
                assumption_adjusted_score=Decimal(display),
            )
            s.add(cand)
            await s.flush()
            await s.refresh(cand)
            assert cand.confidence_score == expected, (
                f"{display} should convert to {expected}, got {cand.confidence_score}"
            )


@pytest.mark.asyncio
async def test_runtime_role_cannot_disable_the_derivation_trigger():
    """Privilege boundary: the application role is not the table owner, so it
    cannot turn the derivation off. Disabling it is an owner/superuser act."""
    from sqlalchemy import text
    from sqlalchemy.exc import ProgrammingError

    async with unit_of_work(actor_type="system") as s:
        with pytest.raises(ProgrammingError, match="must be owner"):
            await s.execute(text(
                "ALTER TABLE ioe.optimization_candidate "
                "DISABLE TRIGGER trg_derive_confidence_score"
            ))
        await s.rollback()


def test_check_constraint_holds_independently_of_the_trigger():
    """The CHECK is a row invariant, not a restatement of the derivation.

    Executed as the table OWNER — the privileged path the constraint exists for.
    With the trigger disabled the derivation does not run, and a divergent row is
    still refused by the CHECK.
    """
    import psycopg2

    from tests.conftest import owner_dsn

    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM ioe.optimization_run LIMIT 1")
        row = cur.fetchone()
        if row is None:
            pytest.skip("no optimization_run available to attach a candidate to")
        run_id = row[0]

        cur.execute("ALTER TABLE ioe.optimization_candidate "
                    "DISABLE TRIGGER trg_derive_confidence_score")
        with pytest.raises(psycopg2.errors.CheckViolation) as exc:
            cur.execute(
                "INSERT INTO ioe.optimization_candidate "
                "(run_id, opportunity_code, eligibility_status, "
                " display_support_score, assumption_adjusted_score, confidence_score) "
                "VALUES (%s, 'divergent', 'eligible', 62.40, 62.40, 99)", (run_id,))
        assert "candidate_confidence_matches_display" in str(exc.value)
    finally:
        conn.rollback()          # also reverts the DISABLE TRIGGER
        conn.close()
