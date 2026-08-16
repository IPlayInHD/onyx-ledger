"""P5 exit tests — scenario simulation, comparison, archive, freshness.

Covers the mandated criteria against real PostgreSQL: profile immutability,
atomic composite-lever failure, canonical idempotency, idempotency mismatch,
hash replay, label/note exclusion from hashes, incompatible comparison
rejection, archive visibility, stale-result labelling, refresh/supersession,
rule-publication races, cross-user isolation, and absence of raw input
duplication.
"""
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.core.exceptions import Conflict, NotFound
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
    ScenarioInputChange,
    ScenarioLever,
    ScenarioResult,
    TaxProfile,
    TaxRule,
    TaxRuleVersion,
    UserAccount,
)
from app.database.session import unit_of_work
from app.services.ioe.domain.freshness import (
    ComparableScenario,
    ScenariosNotComparable,
    compare,
)
from app.services.ioe.domain.scenario import ScenarioSpec, ScenarioSpecError
from app.services.ioe.scenario.service import (
    ScenarioIdempotencyKeyReused,
    ScenarioService,
)
from tests.conftest import frozen_snapshot

RRSP = "INCREASE_RRSP_DEDUCTION"
FHSA = "INCREASE_FHSA_DEDUCTION"
EMPLOYMENT = "95000"


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


def _lever(code=RRSP, amount="5000"):
    return {"lever_code": code, "parameters": {"amount": Decimal(amount)}}


async def _user_with_analysis(
    employment: str = EMPLOYMENT, *, tax_year: int = 2025
) -> tuple[uuid.UUID, uuid.UUID]:
    async with unit_of_work(actor_type="system") as s:
        u = UserAccount(email=f"p5_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(u)
        await s.flush()
        uid = u.id
        income_type = await s.scalar(
            select(IncomeType).where(IncomeType.code == "employment")
        )
        income_type_id = income_type.id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(TaxProfile(user_id=uid, province_code="ON", marital_status="single"))
        s.add(IncomeSource(
            user_id=uid, tax_year=tax_year, income_type_id=income_type_id,
            amount=Decimal(employment), province_code="ON",
        ))
        run = AnalysisRun(
            user_id=uid, tax_year=tax_year, province_code="ON",
            engine_version="py-1.0.0", status="completed",
            started_at=datetime.now(tz=UTC), completed_at=datetime.now(tz=UTC),
            data_verified=True,
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


async def _publish_rule(code: str, *, tax_year: int = 2025) -> uuid.UUID:
    async with unit_of_work(actor_type="admin") as s:
        jur = await s.scalar(select(Jurisdiction).where(Jurisdiction.code == "FED"))
        rule = TaxRule(code=code, name=code, category="deduction",
                       jurisdiction_id=jur.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=tax_year, effective_date=date(tax_year, 1, 1),
            status="published", description="P5 fixture",
            eligibility_basis_codes=["BASIS_P5"],
        )
        s.add(version)
        await s.flush()
        s.add(RuleOutcome(
            rule_version_id=version.id, outcome_type="recommend", priority=1,
            title_template=f"{code} opportunity",
        ))
        await s.flush()
        return version.id


def _suffix() -> str:
    return uuid.uuid4().hex[:6].upper()


# ---------------------------------------------------------------------------
# Profile immutability — a scenario never writes production data
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_simulating_does_not_modify_the_users_profile_or_income():
    uid, analysis_id = await _user_with_analysis()

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        before_income = [
            (r.id, r.amount) for r in await s.scalars(
                select(IncomeSource).where(IncomeSource.user_id == uid)
            )
        ]
        before_profile = await s.scalar(
            select(TaxProfile).where(TaxProfile.user_id == uid)
        )
        before_province = before_profile.province_code

    spec = ScenarioSpec.parse([_lever(RRSP, "12000")])
    outcome = await ScenarioService(uid).simulate(analysis_id, spec)
    assert outcome.workflow_status == "completed"
    assert outcome.tax_delta > 0, "the fixture must actually move the tax"

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        after_income = [
            (r.id, r.amount) for r in await s.scalars(
                select(IncomeSource).where(IncomeSource.user_id == uid)
            )
        ]
        after_profile = await s.scalar(
            select(TaxProfile).where(TaxProfile.user_id == uid)
        )
    assert after_income == before_income
    assert after_profile.province_code == before_province
    # the hypothetical deduction exists nowhere in the user's real data
    assert all(amount == Decimal(EMPLOYMENT) for _id, amount in after_income)


@pytest.mark.asyncio
async def test_two_scenarios_on_one_baseline_do_not_influence_each_other():
    """Each hypothetical is applied to its own clone of the frozen input."""
    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)

    small = await service.simulate(analysis_id, ScenarioSpec.parse([_lever(RRSP, "2000")]))
    large = await service.simulate(analysis_id, ScenarioSpec.parse([_lever(RRSP, "9000")]))
    repeat_small = await service.simulate(
        analysis_id, ScenarioSpec.parse([_lever(RRSP, "2000")])
    )

    assert large.tax_delta > small.tax_delta
    # re-running the first is a canonical replay, not a differently-based result
    assert repeat_small.replayed is True
    assert repeat_small.scenario_id == small.scenario_id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        for outcome in (small, large):
            result = await s.scalar(
                select(ScenarioResult).where(
                    ScenarioResult.scenario_id == outcome.scenario_id
                )
            )
            # every scenario measured against the SAME frozen baseline
            assert result.baseline_tax == (await s.scalar(
                select(Scenario.baseline_tax).where(Scenario.id == outcome.scenario_id)
            ))


# ---------------------------------------------------------------------------
# Atomic composite-lever failure
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_composite_lever_failure_leaves_no_partial_scenario_result():
    """A failure part-way through must not produce a result computed from a
    half-applied set of levers."""
    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)
    spec = ScenarioSpec.parse([_lever(RRSP, "5000"), _lever(FHSA, "3000")])

    from app.services.ioe.domain import levers as lever_registry

    original_apply = lever_registry.apply_lever
    calls = {"n": 0}

    def failing_apply(inputs, code, params, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:          # the SECOND lever of the composite fails
            raise lever_registry.LeverValidationError("simulated mid-composite failure")
        return original_apply(inputs, code, params, **kwargs)

    lever_registry.apply_lever = failing_apply
    try:
        with pytest.raises(lever_registry.LeverValidationError):
            await service.simulate(analysis_id, spec)
    finally:
        lever_registry.apply_lever = original_apply

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.scalar(
            select(Scenario).where(Scenario.user_id == uid)
            .order_by(Scenario.created_at.desc())
        )
        assert scenario.workflow_status == "failed"
        assert scenario.error_code == "LEVER_APPLICATION_FAILED"
        # no result, and no half-applied change trace
        assert await s.scalar(
            select(func.count(ScenarioResult.id))
            .where(ScenarioResult.scenario_id == scenario.id)
        ) == 0
        assert await s.scalar(
            select(func.count(ScenarioInputChange.id))
            .where(ScenarioInputChange.scenario_id == scenario.id)
        ) == 0
        # but the pinned SPEC survives, because that is what was hashed
        assert await s.scalar(
            select(func.count(ScenarioLever.id))
            .where(ScenarioLever.scenario_id == scenario.id)
        ) == 2


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_canonically_equivalent_requests_replay_the_same_scenario():
    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)

    first = await service.simulate(analysis_id, ScenarioSpec.parse([_lever()]))
    # same levers, different label and note — the same scenario
    second = await service.simulate(
        analysis_id,
        ScenarioSpec.parse([_lever()], label="renamed", note="a note"),
    )
    assert second.replayed is True
    assert second.scenario_id == first.scenario_id
    assert second.spec_hash == first.spec_hash


@pytest.mark.asyncio
async def test_the_same_key_with_a_different_spec_is_refused():
    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)
    key = f"key-{uuid.uuid4().hex[:8]}"

    await service.simulate(
        analysis_id, ScenarioSpec.parse([_lever(RRSP, "5000")]), idempotency_key=key
    )
    with pytest.raises(ScenarioIdempotencyKeyReused, match="idempotency_key_reused"):
        await service.simulate(
            analysis_id, ScenarioSpec.parse([_lever(RRSP, "9000")]),
            idempotency_key=key,
        )


@pytest.mark.asyncio
async def test_the_same_key_with_the_same_spec_replays():
    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)
    key = f"key-{uuid.uuid4().hex[:8]}"
    spec = ScenarioSpec.parse([_lever()])

    first = await service.simulate(analysis_id, spec, idempotency_key=key)
    second = await service.simulate(analysis_id, spec, idempotency_key=key)
    assert second.replayed and second.scenario_id == first.scenario_id


# ---------------------------------------------------------------------------
# Hash replay + label/note exclusion
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_historical_replay_reproduces_both_hashes():
    """Recompute the spec and result hashes from STORED columns and require
    they match what was sealed."""
    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)
    spec = ScenarioSpec.parse([_lever(RRSP, "7000"), _lever(FHSA, "2000")])
    outcome = await service.simulate(analysis_id, spec)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        stored_spec_hash = scenario.scenario_spec_hash
        stored_result_hash = scenario.scenario_result_hash
        rebuilt_spec = await ScenarioService._load_spec(s, scenario)
        # THE VERSION AND THE BOUND DIGEST COME FROM THE ROW. A literal here was
        # only ever right while production wrote v1; reading them back is what
        # this test was always demonstrating.
        sealed_version = scenario.result_schema_version
        sealed_inner = (await s.scalar(
            select(ScenarioResult).where(
                ScenarioResult.scenario_id == outcome.scenario_id)
        )).counterfactual_derived_state_hash
        pinned = await service._pin_specification(
            s, analysis_id, rebuilt_spec, result_schema_version=sealed_version)

    assert pinned.spec_hash == stored_spec_hash, "spec hash did not replay"

    computed = service._compute(pinned)
    replayed_result_hash = __import__(
        "app.services.ioe.domain.canonical", fromlist=["c"]
    ).scenario_result_hash(
        spec_hash=pinned.spec_hash,
        result=service.canonical_result(
            computed, result_schema_version=sealed_version,
            counterfactual_derived_state_hash=sealed_inner),
    )
    assert replayed_result_hash == stored_result_hash, "result hash did not replay"


@pytest.mark.asyncio
async def test_renaming_a_scenario_changes_neither_hash():
    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)
    outcome = await service.simulate(
        analysis_id, ScenarioSpec.parse([_lever()], label="first name")
    )

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        before = (scenario.scenario_spec_hash, scenario.scenario_result_hash)
        scenario.label = "a completely different name"
        scenario.note = "and a note added afterwards"
        await s.flush()

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        assert (scenario.scenario_spec_hash, scenario.scenario_result_hash) == before
        assert scenario.label == "a completely different name"


@pytest.mark.asyncio
async def test_sealed_hashes_cannot_be_edited_once_completed():
    uid, analysis_id = await _user_with_analysis()
    outcome = await ScenarioService(uid).simulate(
        analysis_id, ScenarioSpec.parse([_lever()])
    )

    from sqlalchemy.exc import DBAPIError

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        scenario.scenario_result_hash = "tampered"
        with pytest.raises(DBAPIError, match="sealed"):
            await s.flush()

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        with pytest.raises(DBAPIError, match="immutable calculation evidence"):
            await s.execute(
                text("UPDATE ioe.scenario_result SET tax_delta = 1 "
                     "WHERE scenario_id = :sid"),
                {"sid": outcome.scenario_id},
            )


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
async def _comparable(session, scenario_id) -> ComparableScenario:
    scenario = await session.get(Scenario, scenario_id)
    result = await session.scalar(
        select(ScenarioResult).where(ScenarioResult.scenario_id == scenario_id)
    )
    return ComparableScenario(
        scenario_id=scenario.id,
        base_analysis_id=scenario.base_analysis_id,
        tax_year=scenario.tax_year,
        jurisdiction=scenario.jurisdiction,
        objective_code=scenario.objective_code,
        objective_version=scenario.objective_version,
        result_schema_version=scenario.result_schema_version,
        baseline_input_snapshot_hash=scenario.baseline_input_snapshot_hash,
        objective_value_baseline=result.objective_value_baseline,
        objective_value_scenario=result.objective_value_scenario,
        objective_delta=result.objective_delta,
        scenario_tax=result.scenario_tax,
        label=scenario.label,
        freshness_status=scenario.freshness_status,
        stale_reason_code=scenario.stale_reason_code,
    )


@pytest.mark.asyncio
async def test_two_scenarios_on_the_same_baseline_compare():
    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)
    a = await service.simulate(analysis_id, ScenarioSpec.parse([_lever(RRSP, "9000")]))
    b = await service.simulate(analysis_id, ScenarioSpec.parse([_lever(RRSP, "3000")]))

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        left, right = await _comparable(s, a.scenario_id), await _comparable(s, b.scenario_id)

    result = compare(left, right)
    assert result.better == "left"           # the larger deduction wins
    assert result.difference > 0
    assert result.objective_code == left.objective_code


@pytest.mark.asyncio
async def test_comparison_across_different_baselines_is_refused():
    uid, analysis_a = await _user_with_analysis()
    _uid_b, analysis_b = await _user_with_analysis()
    # a second analysis for the SAME user, so this is a baseline mismatch and
    # not merely an authorization failure
    async with unit_of_work(user_id=uid, actor_type="user") as s:
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
        analysis_second = run.id

    service = ScenarioService(uid)
    a = await service.simulate(analysis_a, ScenarioSpec.parse([_lever(RRSP, "5000")]))
    b = await service.simulate(
        analysis_second, ScenarioSpec.parse([_lever(RRSP, "6000")])
    )

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        left, right = await _comparable(s, a.scenario_id), await _comparable(s, b.scenario_id)

    with pytest.raises(ScenariosNotComparable, match="different baselines"):
        compare(left, right)


@pytest.mark.asyncio
async def test_comparison_across_tax_years_is_refused():
    uid_2025, analysis_2025 = await _user_with_analysis(tax_year=2025)
    service = ScenarioService(uid_2025)
    a = await service.simulate(analysis_2025, ScenarioSpec.parse([_lever(RRSP, "5000")]))

    async with unit_of_work(user_id=uid_2025, actor_type="user") as s:
        left = await _comparable(s, a.scenario_id)
    right = ComparableScenario(**{**left.__dict__, "scenario_id": uuid.uuid4(),
                                  "tax_year": 2024})

    with pytest.raises(ScenariosNotComparable, match="different tax years"):
        compare(left, right)


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_delete_archives_and_keeps_every_piece_of_evidence():
    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)
    outcome = await service.simulate(
        analysis_id, ScenarioSpec.parse([_lever(RRSP, "6000")])
    )

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        before = {
            "results": await s.scalar(
                select(func.count(ScenarioResult.id))
                .where(ScenarioResult.scenario_id == outcome.scenario_id)),
            "changes": await s.scalar(
                select(func.count(ScenarioInputChange.id))
                .where(ScenarioInputChange.scenario_id == outcome.scenario_id)),
            "levers": await s.scalar(
                select(func.count(ScenarioLever.id))
                .where(ScenarioLever.scenario_id == outcome.scenario_id)),
            "components": await s.scalar(
                select(func.count(ScenarioConfidenceComponent.id))
                .where(ScenarioConfidenceComponent.scenario_id == outcome.scenario_id)),
        }
    assert before["results"] == 1 and before["changes"] > 0 and before["levers"] == 1

    await service.archive(outcome.scenario_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        assert scenario.visibility_status == "archived"
        assert scenario.archived_at is not None
        # the row itself still exists, and so does every child
        after = {
            "results": await s.scalar(
                select(func.count(ScenarioResult.id))
                .where(ScenarioResult.scenario_id == outcome.scenario_id)),
            "changes": await s.scalar(
                select(func.count(ScenarioInputChange.id))
                .where(ScenarioInputChange.scenario_id == outcome.scenario_id)),
            "levers": await s.scalar(
                select(func.count(ScenarioLever.id))
                .where(ScenarioLever.scenario_id == outcome.scenario_id)),
            "components": await s.scalar(
                select(func.count(ScenarioConfidenceComponent.id))
                .where(ScenarioConfidenceComponent.scenario_id == outcome.scenario_id)),
        }
        assert after == before, "archiving must not delete evidence"

        # and it drops out of the active listing
        active = await s.scalar(
            select(func.count(Scenario.id)).where(
                Scenario.user_id == uid, Scenario.visibility_status == "active"
            )
        )
        assert active == 0


@pytest.mark.asyncio
async def test_an_archived_scenario_frees_its_spec_hash_for_a_new_run():
    """The uniqueness index covers ACTIVE scenarios, so archiving one lets the
    same specification be run again rather than being permanently blocked."""
    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)
    spec = ScenarioSpec.parse([_lever(RRSP, "4000")])

    first = await service.simulate(analysis_id, spec)
    await service.archive(first.scenario_id)
    second = await service.simulate(analysis_id, spec)

    assert second.scenario_id != first.scenario_id
    assert second.replayed is False
    assert second.spec_hash == first.spec_hash


@pytest.mark.asyncio
async def test_unarchive_restores_visibility():
    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)
    outcome = await service.simulate(analysis_id, ScenarioSpec.parse([_lever()]))
    await service.archive(outcome.scenario_id)
    await service.unarchive(outcome.scenario_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        assert scenario.visibility_status == "active"
        assert scenario.archived_at is None


# ---------------------------------------------------------------------------
# Freshness, refresh, supersession
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_completed_scenario_starts_current_with_no_stale_reason():
    uid, analysis_id = await _user_with_analysis()
    outcome = await ScenarioService(uid).simulate(
        analysis_id, ScenarioSpec.parse([_lever()])
    )
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        assert scenario.freshness_status == "current"
        assert scenario.stale_reason_code is None
        assert scenario.freshness_evaluated_at is not None


@pytest.mark.asyncio
async def test_refresh_creates_a_new_scenario_and_supersedes_the_old_one():
    """The historical result keeps its own pins and its own numbers."""
    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)
    original = await service.simulate(
        analysis_id, ScenarioSpec.parse([_lever(RRSP, "5000")], label="original")
    )

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        before = await s.get(Scenario, original.scenario_id)
        original_result_hash = before.scenario_result_hash
        original_delta = (await s.scalar(
            select(ScenarioResult).where(
                ScenarioResult.scenario_id == original.scenario_id)
        )).objective_delta

    # archive so the refresh is not simply a canonical replay of an active row
    await service.archive(original.scenario_id)
    refreshed = await service.refresh(original.scenario_id)
    assert refreshed.scenario_id != original.scenario_id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        old = await s.get(Scenario, original.scenario_id)
        new = await s.get(Scenario, refreshed.scenario_id)

        # the old scenario is superseded, and UNCHANGED in substance
        assert old.freshness_status == "superseded"
        assert old.stale_reason_code == "SUPERSEDED_BY_REFRESH"
        assert old.superseded_by_scenario_id == refreshed.scenario_id
        assert old.scenario_result_hash == original_result_hash
        old_result = await s.scalar(
            select(ScenarioResult).where(
                ScenarioResult.scenario_id == original.scenario_id)
        )
        assert old_result.objective_delta == original_delta

        # the new one records where it came from
        assert new.refreshed_from_scenario_id == original.scenario_id
        assert new.freshness_status == "current"


@pytest.mark.asyncio
async def test_a_stale_scenario_is_labelled_rather_than_recomputed():
    uid, analysis_id = await _user_with_analysis()
    outcome = await ScenarioService(uid).simulate(
        analysis_id, ScenarioSpec.parse([_lever()])
    )

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        result_before = await s.scalar(
            select(ScenarioResult).where(
                ScenarioResult.scenario_id == outcome.scenario_id)
        )
        delta_before = result_before.objective_delta
        # the world moved on
        scenario.freshness_status = "stale"
        scenario.stale_reason_code = "RULE_SNAPSHOT_SUPERSEDED"
        scenario.freshness_evaluated_at = datetime.now(tz=UTC)
        await s.flush()

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        result_after = await s.scalar(
            select(ScenarioResult).where(
                ScenarioResult.scenario_id == outcome.scenario_id)
        )
        assert scenario.freshness_status == "stale"
        assert scenario.stale_reason_code == "RULE_SNAPSHOT_SUPERSEDED"
        # the number itself is untouched: it is still true of its own baseline
        assert result_after.objective_delta == delta_before


@pytest.mark.asyncio
async def test_a_stale_reason_is_required_exactly_when_not_current():
    from sqlalchemy.exc import IntegrityError

    uid, analysis_id = await _user_with_analysis()
    outcome = await ScenarioService(uid).simulate(
        analysis_id, ScenarioSpec.parse([_lever()])
    )
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        scenario.freshness_status = "stale"          # without a reason
        with pytest.raises(IntegrityError):
            await s.flush()


# ---------------------------------------------------------------------------
# Rule-publication race
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_rule_published_after_pinning_is_not_in_the_scenarios_snapshot():
    uid, analysis_id = await _user_with_analysis()
    original = await _publish_rule(f"P5RACE_A_{_suffix()}")

    service = ScenarioService(uid)
    spec = ScenarioSpec.parse([_lever()])
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        pinned = await service._pin_specification(s, analysis_id, spec, result_schema_version="1.0.0")
    assert original in pinned.pinned_rule_version_ids

    intruder = await _publish_rule(f"P5RACE_B_{_suffix()}")
    assert intruder not in pinned.pinned_rule_version_ids

    # The in-flight run keeps the set it pinned, and its identity: recomputing
    # the spec hash from the pinned state after the publication gives the same
    # value, so the newly published rule cannot alter what this run is.
    assert service.compute_spec_hash(pinned) == pinned.spec_hash
    assert str(intruder) not in {str(v) for v in pinned.pinned_rule_version_ids}

    # A run started AFTER the publication legitimately sees the new rule — that
    # is the difference the pinning exists to create.
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        later = await service._pin_specification(s, analysis_id, spec, result_schema_version="1.0.0")
    assert intruder in later.pinned_rule_version_ids
    assert later.spec_hash != pinned.spec_hash, (
        "a changed rule set must change the scenario's identity"
    )


# ---------------------------------------------------------------------------
# Cross-user isolation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_scenario_and_all_its_children_are_invisible_to_another_user():
    uid_a, analysis_a = await _user_with_analysis()
    uid_b, _ = await _user_with_analysis()
    outcome = await ScenarioService(uid_a).simulate(
        analysis_a,
        ScenarioSpec.parse(
            [_lever()],
            assumptions=[{"assumption_code": "CONTRIBUTION_ROOM_AVAILABLE",
                          "value_number": Decimal("10000")}],
        ),
    )

    child_tables = (
        "scenario_result", "scenario_input_change", "scenario_lever",
        "scenario_assumption", "scenario_confidence_component", "scenario_event",
    )
    async with unit_of_work(user_id=uid_a, actor_type="user") as s:
        for table in child_tables:
            count = await s.scalar(
                text(f"SELECT count(*) FROM ioe.{table} WHERE scenario_id = :sid"),  # noqa: S608
                {"sid": outcome.scenario_id},
            )
            assert count > 0, f"fixture produced no {table} rows to hide"

    async with unit_of_work(user_id=uid_b, actor_type="user") as s:
        assert await s.scalar(
            select(Scenario).where(Scenario.id == outcome.scenario_id)
        ) is None
        for table in child_tables:
            count = await s.scalar(
                text(f"SELECT count(*) FROM ioe.{table} WHERE scenario_id = :sid"),  # noqa: S608
                {"sid": outcome.scenario_id},
            )
            assert count == 0, f"user B can read ioe.{table}"


@pytest.mark.asyncio
async def test_simulating_on_another_users_analysis_is_refused():
    uid_a, analysis_a = await _user_with_analysis()
    uid_b, _ = await _user_with_analysis()
    with pytest.raises(NotFound):
        await ScenarioService(uid_b).simulate(
            analysis_a, ScenarioSpec.parse([_lever()])
        )


@pytest.mark.asyncio
async def test_archiving_another_users_scenario_is_refused():
    uid_a, analysis_a = await _user_with_analysis()
    uid_b, _ = await _user_with_analysis()
    outcome = await ScenarioService(uid_a).simulate(
        analysis_a, ScenarioSpec.parse([_lever()])
    )
    with pytest.raises(NotFound):
        await ScenarioService(uid_b).archive(outcome.scenario_id)


# ---------------------------------------------------------------------------
# No raw financial input duplication
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_scenario_trace_does_not_duplicate_raw_financial_inputs():
    """The trace records the FIELDS the levers touched. The user's other income,
    expenses and profile stay in the frozen analysis snapshot.

    THE NEEDLE MUST NOT BE VALID HEX. The whole-row scan below sweeps JSON that
    legitimately carries dozens of third-party identifiers — rule-version UUIDs
    in `affected_rule_versions`, candidate ids in the sealed baseline — and a
    purely numeric needle can occur INSIDE one of them by chance: a release
    gate run failed exactly this way when a shared rule's UUIDv7 happened to
    contain `…1287654c…`, and every scenario that evaluated that rule carried
    the "leak" in its own row. A decimal point cannot appear in a UUID or a
    hex digest, while a genuinely leaked money value still renders with its
    cents — so the needle keeps its detection power and loses the collisions.
    """
    distinctive = "87654.21"
    assert any(ch not in "0123456789abcdef-" for ch in distinctive), (
        "the needle degenerated back into valid hex; see the docstring")
    uid, analysis_id = await _user_with_analysis(employment=distinctive)
    outcome = await ScenarioService(uid).simulate(
        analysis_id, ScenarioSpec.parse([_lever(RRSP, "5000")])
    )

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        changes = list(await s.scalars(
            select(ScenarioInputChange).where(
                ScenarioInputChange.scenario_id == outcome.scenario_id)
        ))
        assert changes
        touched_fields = {ch.field for ch in changes}
        assert touched_fields == {"rrsp_deduction"}, (
            "only the field the lever writes may appear in the trace"
        )
        for change in changes:
            for value in (change.old_value, change.new_value):
                assert distinctive not in (value or ""), (
                    "the user's employment income leaked into the scenario trace"
                )

        # and no scenario table carries a copy of the untouched inputs anywhere
        # in its row — checked over the whole row, not a chosen column
        for table in ("scenario_result", "scenario_lever", "scenario_assumption",
                      "scenario_input_change"):
            hit = await s.scalar(
                text(f"SELECT count(*) FROM ioe.{table} t "        # noqa: S608
                     "WHERE t.scenario_id = :sid "
                     "AND to_jsonb(t)::text LIKE :needle"),
                {"sid": outcome.scenario_id, "needle": f"%{distinctive}%"},
            )
            assert hit == 0, f"raw input {distinctive} was copied into ioe.{table}"

        # the scenario header holds the baseline TOTAL, which is a result, not an
        # input, and the snapshot hash rather than the snapshot
        scenario = await s.get(Scenario, outcome.scenario_id)
        assert scenario.baseline_input_snapshot_hash
        assert scenario.version_manifest is not None
        assert "employment_income" not in str(scenario.version_manifest)


@pytest.mark.asyncio
async def test_the_scenario_spec_stores_codes_not_engine_fields():
    uid, analysis_id = await _user_with_analysis()
    outcome = await ScenarioService(uid).simulate(
        analysis_id, ScenarioSpec.parse([_lever(RRSP, "5000")])
    )
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        lever = await s.scalar(
            select(ScenarioLever).where(ScenarioLever.scenario_id == outcome.scenario_id)
        )
        assert lever.lever_code == RRSP
        # the stored parameters name declared PARAMETERS, never engine fields
        assert set(lever.parameters) == {"amount"}
        assert "rrsp_deduction" not in str(lever.parameters)


# ---------------------------------------------------------------------------
# Support-score persistence
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_all_five_support_fields_and_components_are_persisted():
    uid, analysis_id = await _user_with_analysis()
    outcome = await ScenarioService(uid).simulate(
        analysis_id,
        ScenarioSpec.parse(
            [_lever()],
            assumptions=[{"assumption_code": "CONTRIBUTION_ROOM_AVAILABLE",
                          "value_number": Decimal("10000")}],
        ),
    )

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        result = await s.scalar(
            select(ScenarioResult).where(
                ScenarioResult.scenario_id == outcome.scenario_id)
        )
        assert result.raw_support_score is not None
        assert result.assumption_adjusted_score is not None
        assert result.display_support_score is not None
        assert result.support_cap_applied in (True, False)
        # confidence_score is DERIVED, never divergent
        assert result.confidence_score == round(result.display_support_score)
        # the display value is a cap of the adjusted one, never independent
        assert result.display_support_score <= result.assumption_adjusted_score

        components = list(await s.scalars(
            select(ScenarioConfidenceComponent).where(
                ScenarioConfidenceComponent.scenario_id == outcome.scenario_id)
        ))
        assert components, "uncertainty must be auditable, not just a number"
        for component in components:
            assert component.contribution is not None

        assumption = await s.scalar(
            select(ScenarioAssumption).where(
                ScenarioAssumption.scenario_id == outcome.scenario_id)
        )
        assert assumption.assumption_code == "CONTRIBUTION_ROOM_AVAILABLE"


@pytest.mark.asyncio
async def test_an_analysis_that_is_not_completed_is_refused():
    async with unit_of_work(actor_type="system") as s:
        u = UserAccount(email=f"p5bad_{uuid.uuid4().hex[:8]}@test.ca", status="active")
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
        await ScenarioService(uid).simulate(
            analysis_id, ScenarioSpec.parse([_lever()])
        )

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        assert await s.scalar(
            select(func.count(Scenario.id)).where(Scenario.user_id == uid)
        ) == 0


@pytest.mark.asyncio
async def test_a_request_carrying_a_field_path_never_reaches_the_service():
    """The refusal happens at parse time, so the service is never called with
    anything but a validated spec."""
    with pytest.raises(ScenarioSpecError, match="not accepted"):
        ScenarioSpec.parse([{"lever_code": RRSP, "field": "employment_income"}])
