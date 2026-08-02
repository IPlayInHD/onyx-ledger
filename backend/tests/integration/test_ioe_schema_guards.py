"""IOE schema guards — the structural controls the architecture depends on.

Proves at the DATABASE level (not merely in service code):
  * workflow headers only move through legal transitions,
  * result columns seal once a run completes,
  * immutable calculation evidence rejects UPDATE and DELETE,
  * append-only event logs reject mutation,
  * the partial unique indexes enforce idempotency and single-active weights,
  * RLS isolates one user's runs and scenarios from another's.
"""
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.database.models import (
    AnalysisRun,
    OptimizationCandidate,
    OptimizationRun,
    OptimizationRunEvent,
    Scenario,
    UserAccount,
    WeightConfig,
)
from app.database.session import unit_of_work


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


async def _user(s) -> uuid.UUID:
    u = UserAccount(email=f"ioe_{uuid.uuid4().hex[:8]}@test.ca", status="active")
    s.add(u)
    await s.flush()
    return u.id


async def _analysis(s, user_id: uuid.UUID) -> uuid.UUID:
    run = AnalysisRun(user_id=user_id, tax_year=2025, province_code="ON",
                      engine_version="py-1.0.0", status="completed",
                      started_at=datetime.now(tz=UTC), completed_at=datetime.now(tz=UTC))
    s.add(run)
    await s.flush()
    return run.id


async def _setup() -> tuple[uuid.UUID, uuid.UUID]:
    """A user (identity is not RLS-gated) plus an analysis created in that
    user's own context, since analysis_run enforces self-ownership."""
    async with unit_of_work(actor_type="system") as s:
        uid = await _user(s)
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        aid = await _analysis(s, uid)
    return uid, aid


async def _run(s, user_id: uuid.UUID, analysis_id: uuid.UUID, **kw) -> OptimizationRun:
    run = OptimizationRun(user_id=user_id, analysis_id=analysis_id, tax_year=2025, **kw)
    s.add(run)
    await s.flush()
    return run


# ---------------------------------------------------------------------------
# Workflow transitions
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_legal_transition_pending_running_completed():
    uid, aid = await _setup()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await _run(s, uid, aid)
        assert run.workflow_status == "pending"
        run.workflow_status = "running"
        await s.flush()
        run.workflow_status = "completed"
        run.completed_at = datetime.now(tz=UTC)
        await s.flush()
        assert run.workflow_status == "completed"


@pytest.mark.asyncio
async def test_illegal_transition_is_rejected_by_the_database():
    """pending → completed skips the running state and must be refused."""
    uid, aid = await _setup()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await _run(s, uid, aid)
        run.workflow_status = "completed"
        with pytest.raises(DBAPIError, match="illegal workflow transition"):
            await s.flush()


@pytest.mark.asyncio
async def test_terminal_status_cannot_change():
    uid, aid = await _setup()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await _run(s, uid, aid, workflow_status="pending")
        run.workflow_status = "failed"
        await s.flush()
        run.workflow_status = "running"          # failed is terminal
        with pytest.raises(DBAPIError, match="illegal workflow transition"):
            await s.flush()


@pytest.mark.asyncio
async def test_result_columns_seal_on_completion_but_freshness_stays_mutable():
    uid, aid = await _setup()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await _run(s, uid, aid)
        run.workflow_status = "running"
        await s.flush()
        run.workflow_status = "completed"
        run.optimization_result_hash = "sealedhash"
        run.portfolio_total_benefit = Decimal("1234.56")
        await s.flush()

        # freshness is a SEPARATE axis and must remain updatable (§24)
        run.freshness_status = "stale"
        run.stale_reason_codes = ["RULE_VERSION_REPLACED"]
        run.stale_at = datetime.now(tz=UTC)
        await s.flush()
        assert run.freshness_status == "stale"

        # but the computed result is now evidence and cannot drift
        run.portfolio_total_benefit = Decimal("9999.99")
        with pytest.raises(DBAPIError, match="sealed"):
            await s.flush()


# ---------------------------------------------------------------------------
# Immutable calculation evidence
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_calculation_evidence_rejects_update_and_delete():
    uid, aid = await _setup()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await _run(s, uid, aid)
        cand = OptimizationCandidate(
            run_id=run.id, opportunity_code="rrsp", eligibility_status="eligible",
            portfolio_membership="selected", standalone_potential=Decimal("100.00"),
        )
        s.add(cand)
        await s.flush()
        cand_id = cand.id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        with pytest.raises(DBAPIError, match="immutable calculation evidence"):
            await s.execute(text(
                "UPDATE ioe.optimization_candidate SET opportunity_code='tampered' WHERE id=:i"
            ), {"i": cand_id})

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        with pytest.raises(DBAPIError, match="immutable calculation evidence"):
            await s.execute(text("DELETE FROM ioe.optimization_candidate WHERE id=:i"),
                            {"i": cand_id})


@pytest.mark.asyncio
async def test_event_log_is_append_only():
    uid, aid = await _setup()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await _run(s, uid, aid)
        ev = OptimizationRunEvent(run_id=run.id, from_status=None, to_status="pending")
        s.add(ev)
        await s.flush()
        ev_id = ev.id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        with pytest.raises(DBAPIError, match="immutable calculation evidence"):
            await s.execute(text(
                "UPDATE ioe.optimization_run_event SET to_status='forged' WHERE id=:i"
            ), {"i": ev_id})


@pytest.mark.asyncio
async def test_evidence_purge_context_permits_deletion():
    """Account erasure needs a sanctioned path; it is explicit, not implicit."""
    uid, aid = await _setup()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await _run(s, uid, aid)
        cand = OptimizationCandidate(
            run_id=run.id, opportunity_code="fhsa", eligibility_status="eligible",
        )
        s.add(cand)
        await s.flush()
        cand_id = cand.id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await s.execute(text("SELECT set_config('app.allow_evidence_purge','on',true)"))
        await s.execute(text("DELETE FROM ioe.optimization_candidate WHERE id=:i"),
                        {"i": cand_id})
        remaining = await s.scalar(
            select(OptimizationCandidate).where(OptimizationCandidate.id == cand_id)
        )
        assert remaining is None


# ---------------------------------------------------------------------------
# Idempotency / uniqueness
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_duplicate_idempotency_key_is_rejected():
    key = f"idem-{uuid.uuid4().hex[:8]}"
    uid, aid = await _setup()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await _run(s, uid, aid, idempotency_key=key)
        s.add(OptimizationRun(user_id=uid, analysis_id=aid, tax_year=2025,
                              idempotency_key=key))
        with pytest.raises(IntegrityError):
            await s.flush()


@pytest.mark.asyncio
async def test_duplicate_current_spec_hash_is_rejected():
    """Two concurrent equivalent computations cannot both create a run (§23)."""
    spec = f"spec-{uuid.uuid4().hex}"
    uid, aid = await _setup()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await _run(s, uid, aid, optimization_spec_hash=spec)
        s.add(OptimizationRun(user_id=uid, analysis_id=aid, tax_year=2025,
                              optimization_spec_hash=spec))
        with pytest.raises(IntegrityError):
            await s.flush()


@pytest.mark.asyncio
async def test_only_one_active_weight_config():
    async with unit_of_work(actor_type="admin") as s:
        v = uuid.uuid4().hex[:8]
        s.add(WeightConfig(version=f"w-{v}-a", weights={"confidence": 1.0},
                           checksum="a", is_active=True))
        await s.flush()
        s.add(WeightConfig(version=f"w-{v}-b", weights={"confidence": 1.0},
                           checksum="b", is_active=True))
        with pytest.raises(IntegrityError):
            await s.flush()


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rls_isolates_optimization_runs_and_scenarios_between_users():
    async with unit_of_work(actor_type="system") as s:
        user_b = await _user(s)
    user_a, analysis_a = await _setup()

    async with unit_of_work(user_id=user_a, actor_type="user") as s:
        run = await _run(s, user_a, analysis_a)
        scenario = Scenario(user_id=user_a, base_analysis_id=analysis_a, label="A's scenario")
        s.add(scenario)
        await s.flush()
        run_id, scenario_id = run.id, scenario.id

    # user A sees their own
    async with unit_of_work(user_id=user_a, actor_type="user") as s:
        assert await s.scalar(select(OptimizationRun).where(OptimizationRun.id == run_id))
        assert await s.scalar(select(Scenario).where(Scenario.id == scenario_id))

    # user B sees nothing of A's
    async with unit_of_work(user_id=user_b, actor_type="user") as s:
        assert await s.scalar(select(OptimizationRun).where(OptimizationRun.id == run_id)) is None
        assert await s.scalar(select(Scenario).where(Scenario.id == scenario_id)) is None


@pytest.mark.asyncio
async def test_scenario_archive_preserves_the_record():
    """'Delete' is an archive: evidence and audit survive (§17)."""
    uid, aid = await _setup()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        sc = Scenario(user_id=uid, base_analysis_id=aid, label="to archive")
        s.add(sc)
        await s.flush()
        sc.visibility_status = "archived"
        sc.archived_at = datetime.now(tz=UTC)
        await s.flush()
        sc_id = sc.id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        still_there = await s.scalar(select(Scenario).where(Scenario.id == sc_id))
        assert still_there is not None
        assert still_there.visibility_status == "archived"


@pytest.mark.asyncio
async def test_recommendation_accepts_new_lifecycle_states():
    """The widened CHECK is a superset: legacy and new states both validate."""
    from app.database.models import Recommendation

    uid, aid = await _setup()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        for status in ("generated", "new", "saved", "planned", "dismissed",
                       "unable_to_complete", "expired"):
            s.add(Recommendation(
                analysis_id=aid, user_id=uid, opportunity_code="x",
                title=f"rec {status}", status=status,
                calculation_basis="engine_determined", evidence_status="user_attested",
            ))
        await s.flush()   # must not raise
