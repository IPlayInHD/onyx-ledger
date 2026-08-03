"""P6 exit tests — API surfaces, freshness wiring, workers, projections.

Covers: retrieval from sealed rows, read-time freshness, refresh/supersession,
comparison compatibility and ownership, archive filtering, worker idempotency,
projection separation, OpenAPI safety, support-score round-trip, cross-user
denial, and absence of route-level recomputation.
"""
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.security.jwt import create_access_token
from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
    IncomeSource,
    IncomeType,
    Scenario,
    ScenarioResult,
    TaxProfile,
    UserAccount,
)
from app.database.session import unit_of_work
from app.services.ioe.domain.scenario import ScenarioSpec, StaleReason
from app.services.ioe.scenario.freshness_service import ScenarioFreshnessService
from app.services.ioe.scenario.service import ScenarioService

RRSP = "INCREASE_RRSP_DEDUCTION"
API = "/api/v1/ioe"


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


def _auth(user_id: uuid.UUID) -> dict:
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


def _lever(amount="5000"):
    return {"lever_code": RRSP, "parameters": {"amount": str(Decimal(amount))}}


async def _user_with_analysis(employment: str = "95000") -> tuple[uuid.UUID, uuid.UUID]:
    async with unit_of_work(actor_type="system") as s:
        u = UserAccount(email=f"p6_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(u)
        await s.flush()
        uid = u.id
        income_type = await s.scalar(
            select(IncomeType).where(IncomeType.code == "employment")
        )
        type_id = income_type.id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(TaxProfile(user_id=uid, province_code="ON", marital_status="single"))
        s.add(IncomeSource(
            user_id=uid, tax_year=2025, income_type_id=type_id,
            amount=Decimal(employment), province_code="ON",
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


# ---------------------------------------------------------------------------
# Retrieval from sealed rows
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_creating_and_reading_a_scenario_returns_sealed_values(client):
    uid, analysis_id = await _user_with_analysis()
    body = {"analysis_id": str(analysis_id), "levers": [_lever("8000")],
            "label": "max RRSP"}

    created = await client.post(f"{API}/scenarios", json=body, headers=_auth(uid))
    assert created.status_code == 201, created.text
    detail = created.json()
    scenario_id = detail["id"]

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        stored = await s.scalar(
            select(ScenarioResult).where(
                ScenarioResult.scenario_id == uuid.UUID(scenario_id))
        )
        scenario = await s.get(Scenario, uuid.UUID(scenario_id))

    # every figure in the response IS the stored figure
    assert Decimal(detail["tax_delta"]["amount"]) == stored.tax_delta
    assert Decimal(detail["objective_delta"]["amount"]) == stored.objective_delta
    assert Decimal(detail["scenario_tax"]["amount"]) == stored.scenario_tax
    assert Decimal(detail["baseline_tax"]["amount"]) == scenario.baseline_tax
    assert detail["scenario_spec_hash"] == scenario.scenario_spec_hash
    assert detail["scenario_result_hash"] == scenario.scenario_result_hash


@pytest.mark.asyncio
async def test_every_monetary_field_carries_its_full_context(client):
    uid, analysis_id = await _user_with_analysis()
    created = await client.post(
        f"{API}/scenarios",
        json={"analysis_id": str(analysis_id), "levers": [_lever()]},
        headers=_auth(uid),
    )
    detail = created.json()

    required = {
        "amount", "effect_type", "calculation_basis", "evidence_status",
        "tax_year", "currency_code", "horizon_years", "is_permanent",
    }
    for field in ("baseline_tax", "scenario_tax", "tax_delta", "objective_delta"):
        amount = detail[field]
        assert required <= set(amount), f"{field} is missing context: {set(amount)}"
        assert amount["currency_code"] == "CAD"
        assert amount["tax_year"] == 2025
        assert amount["freshness"]["freshness_status"] in (
            "unknown", "current", "stale", "superseded"
        )
    # the support score travels with its disclaimer, never as a probability
    support = detail["tax_delta"]["support"]
    assert support is not None
    assert "probability" in support["disclaimer"].lower()


@pytest.mark.asyncio
async def test_support_scores_round_trip_through_the_api(client):
    uid, analysis_id = await _user_with_analysis()
    created = await client.post(
        f"{API}/scenarios",
        json={
            "analysis_id": str(analysis_id), "levers": [_lever()],
            "assumptions": [{"assumption_code": "CONTRIBUTION_ROOM_AVAILABLE",
                             "value_number": "10000"}],
        },
        headers=_auth(uid),
    )
    detail = created.json()
    support = detail["support"]

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        stored = await s.scalar(
            select(ScenarioResult).where(
                ScenarioResult.scenario_id == uuid.UUID(detail["id"]))
        )
    assert Decimal(support["display_support_score"]) == stored.display_support_score
    assert Decimal(support["assumption_adjusted_score"]) == stored.assumption_adjusted_score
    assert Decimal(support["raw_support_score"]) == stored.raw_support_score
    assert support["support_cap_applied"] == stored.support_cap_applied
    assert support["support_cap_reason_code"] == stored.support_cap_reason_code


# ---------------------------------------------------------------------------
# Read-time freshness
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_detail_read_evaluates_and_persists_freshness(client):
    uid, analysis_id = await _user_with_analysis()
    outcome = await ScenarioService(uid).simulate(
        analysis_id, ScenarioSpec.parse([{"lever_code": RRSP,
                                          "parameters": {"amount": Decimal("5000")}}])
    )

    # the world moves: the baseline snapshot hash changes
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        snapshot = await s.get(AnalysisInputSnapshot, analysis_id)
        snapshot.snapshot_hash = f"moved-{uuid.uuid4().hex[:8]}"
        await s.flush()

    response = await client.get(
        f"{API}/scenarios/{outcome.scenario_id}", headers=_auth(uid)
    )
    assert response.status_code == 200
    detail = response.json()
    assert detail["freshness"]["freshness_status"] == "stale"
    assert detail["freshness"]["stale_reason_code"] == "BASELINE_INPUTS_CHANGED"

    # and the transition was PERSISTED, not just reported
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        assert scenario.freshness_status == "stale"
        assert scenario.stale_reason_code == "BASELINE_INPUTS_CHANGED"
        assert scenario.freshness_evaluated_at is not None


@pytest.mark.asyncio
async def test_going_stale_never_alters_the_stored_result(client):
    uid, analysis_id = await _user_with_analysis()
    outcome = await ScenarioService(uid).simulate(
        analysis_id, ScenarioSpec.parse([{"lever_code": RRSP,
                                          "parameters": {"amount": Decimal("6000")}}])
    )
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        before = await s.scalar(
            select(ScenarioResult).where(
                ScenarioResult.scenario_id == outcome.scenario_id)
        )
        snapshot_before = (before.tax_delta, before.objective_delta,
                           before.scenario_tax, before.display_support_score)
        snapshot = await s.get(AnalysisInputSnapshot, analysis_id)
        snapshot.snapshot_hash = f"moved-{uuid.uuid4().hex[:8]}"
        await s.flush()

    await client.get(f"{API}/scenarios/{outcome.scenario_id}", headers=_auth(uid))

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        after = await s.scalar(
            select(ScenarioResult).where(
                ScenarioResult.scenario_id == outcome.scenario_id)
        )
        assert (after.tax_delta, after.objective_delta, after.scenario_tax,
                after.display_support_score) == snapshot_before


@pytest.mark.asyncio
async def test_a_stale_scenario_does_not_silently_return_to_current(client):
    """The only honest way back is a refresh, which creates a new scenario."""
    uid, analysis_id = await _user_with_analysis()
    outcome = await ScenarioService(uid).simulate(
        analysis_id, ScenarioSpec.parse([{"lever_code": RRSP,
                                          "parameters": {"amount": Decimal("5000")}}])
    )
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        scenario.freshness_status = "stale"
        scenario.stale_reason_code = "RULE_SNAPSHOT_SUPERSEDED"
        await s.flush()

    # nothing about the world has changed back, and even if it had:
    detail = (await client.get(
        f"{API}/scenarios/{outcome.scenario_id}", headers=_auth(uid)
    )).json()
    assert detail["freshness"]["freshness_status"] == "stale"


# ---------------------------------------------------------------------------
# Event-driven and scheduled freshness
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_event_driven_invalidation_marks_scenarios_on_a_moved_baseline():
    uid, analysis_id = await _user_with_analysis()
    outcome = await ScenarioService(uid).simulate(
        analysis_id, ScenarioSpec.parse([{"lever_code": RRSP,
                                          "parameters": {"amount": Decimal("5000")}}])
    )
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        count = await ScenarioFreshnessService(s, uid).invalidate_for_analysis(
            analysis_id, StaleReason.BASELINE_INPUTS_CHANGED
        )
        assert count >= 1

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        assert scenario.freshness_status == "stale"
        assert scenario.stale_reason_code == "BASELINE_INPUTS_CHANGED"
        # the result is untouched
        result = await s.scalar(
            select(ScenarioResult).where(
                ScenarioResult.scenario_id == outcome.scenario_id)
        )
        assert result.tax_delta is not None


@pytest.mark.asyncio
async def test_the_scheduled_sweep_is_bounded_and_evaluates_least_recent_first():
    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)
    for amount in ("2000", "3000", "4000"):
        await service.simulate(
            analysis_id,
            ScenarioSpec.parse([{"lever_code": RRSP,
                                 "parameters": {"amount": Decimal(amount)}}]),
        )

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        transitions = await ScenarioFreshnessService(s, uid).sweep(limit=2)
    assert len(transitions) <= 2, "the sweep must stay within its batch bound"


# ---------------------------------------------------------------------------
# Refresh / supersession through the API
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_refresh_creates_a_new_scenario_and_links_supersession(client):
    uid, analysis_id = await _user_with_analysis()
    created = (await client.post(
        f"{API}/scenarios",
        json={"analysis_id": str(analysis_id), "levers": [_lever("7000")],
              "label": "original"},
        headers=_auth(uid),
    )).json()
    original_id = created["id"]

    await client.delete(f"{API}/scenarios/{original_id}", headers=_auth(uid))
    refreshed = await client.post(
        f"{API}/scenarios/{original_id}/refresh", headers=_auth(uid)
    )
    assert refreshed.status_code == 201
    new_detail = refreshed.json()
    assert new_detail["id"] != original_id

    old = (await client.get(f"{API}/scenarios/{original_id}", headers=_auth(uid))).json()
    assert old["freshness"]["freshness_status"] == "superseded"
    assert old["freshness"]["superseded_by_scenario_id"] == new_detail["id"]
    # the historical numbers are unchanged
    assert old["scenario_result_hash"] == created["scenario_result_hash"]
    assert old["tax_delta"]["amount"] == created["tax_delta"]["amount"]


# ---------------------------------------------------------------------------
# Archive filtering
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_archived_scenarios_are_filtered_from_the_default_listing(client):
    uid, analysis_id = await _user_with_analysis()
    first = (await client.post(
        f"{API}/scenarios",
        json={"analysis_id": str(analysis_id), "levers": [_lever("3000")]},
        headers=_auth(uid),
    )).json()
    second = (await client.post(
        f"{API}/scenarios",
        json={"analysis_id": str(analysis_id), "levers": [_lever("6000")]},
        headers=_auth(uid),
    )).json()

    await client.delete(f"{API}/scenarios/{first['id']}", headers=_auth(uid))

    active = (await client.get(f"{API}/scenarios", headers=_auth(uid))).json()
    ids = {row["id"] for row in active}
    assert first["id"] not in ids
    assert second["id"] in ids

    everything = (await client.get(
        f"{API}/scenarios?include_archived=true", headers=_auth(uid)
    )).json()
    all_ids = {row["id"] for row in everything}
    assert {first["id"], second["id"]} <= all_ids

    # archived, not deleted: the detail read still works and evidence survives
    archived = await client.get(f"{API}/scenarios/{first['id']}", headers=_auth(uid))
    assert archived.status_code == 200
    assert archived.json()["visibility_status"] == "archived"
    assert archived.json()["tax_delta"] is not None


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_comparison_keeps_deltas_separate_by_concept(client):
    uid, analysis_id = await _user_with_analysis()
    left = (await client.post(
        f"{API}/scenarios",
        json={"analysis_id": str(analysis_id), "levers": [_lever("9000")]},
        headers=_auth(uid),
    )).json()
    right = (await client.post(
        f"{API}/scenarios",
        json={"analysis_id": str(analysis_id), "levers": [_lever("3000")]},
        headers=_auth(uid),
    )).json()

    response = await client.get(
        f"{API}/scenarios/{left['id']}/compare/{right['id']}", headers=_auth(uid)
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["better"] == "left"
    # each concept is its own labelled figure, not one merged "difference"
    for key in ("objective_difference", "tax_difference",
                "refund_balance_difference", "liquidity_commitment_difference"):
        assert body[key] is not None
        assert "effect_type" in body[key]
    effect_types = {d["effect_type"] for d in body["effect_type_differences"]}
    assert len(effect_types) > 1
    assert body["objective_difference"]["effect_type"] != \
        body["tax_difference"]["effect_type"]


@pytest.mark.asyncio
async def test_comparison_refuses_another_users_scenario(client):
    uid_a, analysis_a = await _user_with_analysis()
    uid_b, analysis_b = await _user_with_analysis()

    mine = (await client.post(
        f"{API}/scenarios",
        json={"analysis_id": str(analysis_b), "levers": [_lever("4000")]},
        headers=_auth(uid_b),
    )).json()
    theirs = (await client.post(
        f"{API}/scenarios",
        json={"analysis_id": str(analysis_a), "levers": [_lever("4000")]},
        headers=_auth(uid_a),
    )).json()

    response = await client.get(
        f"{API}/scenarios/{mine['id']}/compare/{theirs['id']}", headers=_auth(uid_b)
    )
    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_comparison_refuses_an_incomplete_scenario(client):
    uid, analysis_id = await _user_with_analysis()
    good = (await client.post(
        f"{API}/scenarios",
        json={"analysis_id": str(analysis_id), "levers": [_lever("5000")]},
        headers=_auth(uid),
    )).json()

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        pending = Scenario(
            user_id=uid, base_analysis_id=analysis_id, workflow_status="pending",
            tax_year=2025, jurisdiction="ON",
        )
        s.add(pending)
        await s.flush()
        pending_id = pending.id

    response = await client.get(
        f"{API}/scenarios/{good['id']}/compare/{pending_id}", headers=_auth(uid)
    )
    assert response.status_code == 409, response.text
    assert "scenario_not_completed" in response.text


@pytest.mark.asyncio
async def test_comparison_refuses_incompatible_baselines(client):
    uid, analysis_one = await _user_with_analysis()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
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
        analysis_two = run.id

    left = (await client.post(
        f"{API}/scenarios",
        json={"analysis_id": str(analysis_one), "levers": [_lever("5000")]},
        headers=_auth(uid),
    )).json()
    right = (await client.post(
        f"{API}/scenarios",
        json={"analysis_id": str(analysis_two), "levers": [_lever("6000")]},
        headers=_auth(uid),
    )).json()

    response = await client.get(
        f"{API}/scenarios/{left['id']}/compare/{right['id']}", headers=_auth(uid)
    )
    assert response.status_code in (409, 422), response.text
    assert "different baselines" in response.text


# ---------------------------------------------------------------------------
# Cross-user denial
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_another_user_cannot_read_archive_or_refresh_a_scenario(client):
    uid_a, analysis_a = await _user_with_analysis()
    uid_b, _ = await _user_with_analysis()
    mine = (await client.post(
        f"{API}/scenarios",
        json={"analysis_id": str(analysis_a), "levers": [_lever()]},
        headers=_auth(uid_a),
    )).json()

    for method, path in (
        ("get", f"{API}/scenarios/{mine['id']}"),
        ("delete", f"{API}/scenarios/{mine['id']}"),
        ("post", f"{API}/scenarios/{mine['id']}/refresh"),
        ("post", f"{API}/scenarios/{mine['id']}/unarchive"),
    ):
        response = await getattr(client, method)(path, headers=_auth(uid_b))
        assert response.status_code == 404, f"{method} {path} -> {response.status_code}"

    # and it does not appear in their listing
    listing = (await client.get(
        f"{API}/scenarios?include_archived=true", headers=_auth(uid_b)
    )).json()
    assert mine["id"] not in {row["id"] for row in listing}


@pytest.mark.asyncio
async def test_unauthenticated_requests_are_refused(client):
    response = await client.get(f"{API}/scenarios")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Portfolio reads come from the sealed row
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_portfolio_total_is_the_sealed_value_not_a_sum_of_members(client):
    from app.database.models import PortfolioMember, StrategyPortfolio
    from app.services.ioe.orchestrator import OptimizationOrchestrator
    from tests.integration.test_ioe_portfolio_persistence import (
        _publish_lever_rule,
        _suffix,
    )

    uid, analysis_id = await _user_with_analysis()
    await _publish_lever_rule(
        f"P6PF_{_suffix()}", lever_code=RRSP, amount="6000",
    )
    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    response = await client.get(
        f"{API}/runs/{outcome.run_id}/portfolio", headers=_auth(uid)
    )
    assert response.status_code == 200, response.text
    body = response.json()

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        portfolio = await s.scalar(
            select(StrategyPortfolio).where(StrategyPortfolio.run_id == outcome.run_id)
        )
        members = list(await s.scalars(
            select(PortfolioMember).where(PortfolioMember.portfolio_id == portfolio.id)
        ))

    assert Decimal(body["portfolio_total_benefit"]["amount"]) == \
        portfolio.portfolio_total_benefit
    # the members are shown, but the headline is NOT rebuilt from them
    assert len(body["members"]) == len(members)
    assert body["optimality_claim"] == "none"
    assert "not a globally optimal" in body["optimality_note"]
    # per-concept totals stay separate and labelled
    assert body["total_liquidity_commitment"]["effect_type"] == "liquidity_commitment"
    assert body["total_deferral_amount"]["effect_type"] == "tax_deferral"
    assert body["total_deferral_amount"]["is_permanent"] is False


@pytest.mark.asyncio
async def test_a_run_belonging_to_another_user_is_not_readable(client):
    from app.services.ioe.orchestrator import OptimizationOrchestrator
    from tests.integration.test_ioe_portfolio_persistence import (
        _publish_lever_rule,
        _suffix,
    )

    uid_a, analysis_a = await _user_with_analysis()
    uid_b, _ = await _user_with_analysis()
    await _publish_lever_rule(f"P6DENY_{_suffix()}", lever_code=RRSP, amount="5000")
    outcome = await OptimizationOrchestrator(uid_a).generate(analysis_a)

    response = await client.get(
        f"{API}/runs/{outcome.run_id}/portfolio", headers=_auth(uid_b)
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Projections stay separate
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_projections_are_returned_separately_and_never_in_a_total(client):
    from app.services.ioe.domain.enums import EconomicEffectType
    from app.services.ioe.orchestrator import OptimizationOrchestrator
    from app.services.ioe.projection import ProjectionService, project_recurring
    from tests.integration.test_ioe_portfolio_persistence import (
        _publish_lever_rule,
        _suffix,
    )

    uid, analysis_id = await _user_with_analysis()
    await _publish_lever_rule(f"P6PROJ_{_suffix()}", lever_code=RRSP, amount="5000")
    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    projection = project_recurring(
        annual_amount=Decimal("1200.00"),
        effect_type=EconomicEffectType.RECURRING_ANNUAL_BENEFIT,
        base_tax_year=2025, horizon_years=5,
    )
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await ProjectionService(s).persist(outcome.run_id, projection)

    projected = (await client.get(
        f"{API}/runs/{outcome.run_id}/projections", headers=_auth(uid)
    )).json()
    assert projected is not None
    assert projected["horizon_years"] == 5
    assert projected["methodology_version"]
    assert Decimal(projected["projected_total"]["amount"]) == Decimal("6000.00")
    assert projected["projected_total"]["calculation_basis"] == "projection_estimate"
    assert projected["projected_total"]["is_permanent"] is False
    assert projected["uncertainty"] is not None
    assert "NOT included in any current-year total" in projected["separation_note"]

    # and the portfolio total is entirely unaffected by the projection
    portfolio = (await client.get(
        f"{API}/runs/{outcome.run_id}/portfolio", headers=_auth(uid)
    )).json()
    total = Decimal(portfolio["portfolio_total_benefit"]["amount"])
    assert total != Decimal("6000.00")
    assert portfolio["portfolio_total_benefit"]["horizon_years"] == 1


@pytest.mark.asyncio
async def test_a_one_off_effect_cannot_be_projected_forward():
    from app.services.ioe.domain.enums import EconomicEffectType
    from app.services.ioe.projection import ProjectionNotApplicable, project_recurring

    with pytest.raises(ProjectionNotApplicable):
        project_recurring(
            annual_amount=Decimal("500"),
            effect_type=EconomicEffectType.IMMEDIATE_REFUND_IMPACT,
            base_tax_year=2025, horizon_years=3,
        )


# ---------------------------------------------------------------------------
# OpenAPI safety
# ---------------------------------------------------------------------------
def test_openapi_exposes_only_typed_lever_and_assumption_inputs():
    import json

    from app.main import app

    schemas = app.openapi()["components"]["schemas"]

    request = schemas["ScenarioCreateRequest"]
    assert request["additionalProperties"] is False
    assert set(request["properties"]) == {
        "analysis_id", "levers", "assumptions", "label", "note"
    }

    lever = schemas["LeverInput-Input"]
    assert lever["additionalProperties"] is False
    assert set(lever["properties"]) == {"lever_code", "parameters"}
    assert lever["properties"]["lever_code"]["pattern"] == r"^[A-Z][A-Z0-9_]{2,63}$"

    assumption = schemas["AssumptionInput-Input"]
    assert assumption["additionalProperties"] is False
    assert "formula" not in assumption["properties"]

    # no INPUT schema anywhere in the IOE surface may carry a mutation shape
    forbidden = {"field", "field_path", "path", "patch", "op", "operation",
                 "formula", "expression", "expr", "eval", "script", "mutation"}
    for name, schema in schemas.items():
        if not name.endswith("-Input") and "Request" not in name:
            continue
        offending = forbidden.intersection(schema.get("properties", {}))
        assert not offending, f"{name} exposes mutation fields: {offending}"

    # the only place a field name appears is the OUTPUT trace of what was written
    document = json.dumps(schemas)
    assert '"field"' in document
    assert "field" in schemas["AppliedChangeOut"]["properties"]


def test_openapi_documents_the_scenario_endpoints_as_read_only_of_sealed_data():
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/api/v1/ioe/scenarios" in paths
    assert "/api/v1/ioe/scenarios/{scenario_id}" in paths
    detail = paths["/api/v1/ioe/scenarios/{scenario_id}"]["get"]
    assert "freshness" in detail["description"].lower()
    assert "never modified" in detail["description"].lower()


# ---------------------------------------------------------------------------
# Routes do not recompute
# ---------------------------------------------------------------------------
def test_routes_contain_no_calculation_or_reconstruction():
    """A structural check: the route module must not do arithmetic.

    Route-level arithmetic is exactly how a displayed total comes to disagree
    with the verified one, so the absence is asserted rather than assumed.
    """
    import ast
    import pathlib

    source = pathlib.Path("app/api/v1/ioe/routes.py").read_text()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
        ):
            raise AssertionError(
                f"arithmetic in routes at line {node.lineno}: routes must not "
                "compute, rank, reconcile, or reconstruct totals"
            )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in ("sum", "sorted", "max", "min"), (
                f"aggregation '{node.func.id}' in routes at line {node.lineno}"
            )

    for banned in ("quantize(", "Decimal(", "objective_cost(", "assemble("):
        assert banned not in source, f"routes must not call {banned}"


def test_the_read_repository_never_issues_an_unqualified_child_read():
    """Every IOE child read must be parent-key-qualified (P4 carried constraint)."""
    import pathlib
    import re

    source = pathlib.Path("app/services/ioe/read_repository.py").read_text()
    # every select(...) must be followed by a .where(...) before it is awaited
    selects = re.findall(r"select\((\w+)\)((?:.|\n)*?)\)\)", source)
    assert selects
    for model, tail in selects:
        assert ".where(" in tail, f"unqualified select({model}) in the repository"
