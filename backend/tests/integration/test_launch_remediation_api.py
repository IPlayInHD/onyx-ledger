"""Launch-blocker remediation, product-path proofs.

H2: unsupported jurisdictions are refused at the analysis boundary — machine
readable, nothing persisted, no Ontario fallback.
H3A: optimization is reachable through the authenticated API, calling the
certified orchestrator and nothing else.
H3B: ACTUAL RRSP/FHSA contributions are recorded facts that enter the baseline
analysis, while scenario levers stay hypothetical.
H4: contribution room is an explicit sealed assumption; over-room scenarios are
refused, not sealed as attainable.
"""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.security.jwt import create_access_token
from app.database.models import (
    AnalysisRun,
    IncomeSource,
    IncomeType,
    TaxProfile,
    UserAccount,
)
from app.database.session import unit_of_work

API = "/api/v1"


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


def _auth(user_id: uuid.UUID) -> dict:
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


async def _user(province: str = "ON", employment: str = "80000") -> uuid.UUID:
    async with unit_of_work(actor_type="system") as s:
        u = UserAccount(email=f"rem_{uuid.uuid4().hex[:10]}@test.ca", status="active")
        s.add(u)
        await s.flush()
        uid = u.id
        itype = await s.scalar(select(IncomeType).where(IncomeType.code == "employment"))
        type_id = itype.id
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(TaxProfile(user_id=uid, province_code=province, marital_status="single"))
        s.add(IncomeSource(
            user_id=uid, tax_year=2025, income_type_id=type_id,
            amount=Decimal(employment), province_code=province,
        ))
    return uid


async def _analysis(client, uid) -> str:
    r = await client.post(f"{API}/analysis", json={"tax_year": 2025}, headers=_auth(uid))
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------- H2 --------
@pytest.mark.asyncio
async def test_unsupported_jurisdiction_is_refused_with_nothing_persisted(client):
    uid = await _user(province="YT")
    r = await client.post(f"{API}/analysis", json={"tax_year": 2025}, headers=_auth(uid))
    assert r.status_code == 422, r.text
    body = r.json()
    assert "unsupported_jurisdiction" in body["detail"]
    assert "YT" in body["detail"]
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        rows = list(await s.scalars(
            select(AnalysisRun).where(AnalysisRun.user_id == uid)))
    assert rows == []          # fail closed BEFORE any row exists


@pytest.mark.asyncio
async def test_ontario_still_analyses_normally(client):
    uid = await _user(province="ON")
    r = await client.post(f"{API}/analysis", json={"tax_year": 2025}, headers=_auth(uid))
    assert r.status_code == 201


# ---------------------------------------------------------------- H3A -------
@pytest.mark.asyncio
async def test_optimization_endpoint_runs_the_certified_orchestrator(client):
    uid = await _user()
    analysis_id = await _analysis(client, uid)
    r = await client.post(f"{API}/ioe/optimizations",
                          json={"analysis_id": analysis_id}, headers=_auth(uid))
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["workflow_status"] == "completed"
    assert out["spec_hash"]
    run_id = out["run_id"]

    pf = await client.get(f"{API}/ioe/runs/{run_id}/portfolio", headers=_auth(uid))
    assert pf.status_code == 200


@pytest.mark.asyncio
async def test_optimization_endpoint_denies_cross_tenant_without_enumeration(client):
    owner = await _user()
    analysis_id = await _analysis(client, owner)
    other = await _user()
    r = await client.post(f"{API}/ioe/optimizations",
                          json={"analysis_id": analysis_id}, headers=_auth(other))
    assert r.status_code == 404      # not-found, never forbidden-with-existence


@pytest.mark.asyncio
async def test_optimization_endpoint_seals_declared_capacities(client):
    uid = await _user()
    analysis_id = await _analysis(client, uid)
    r = await client.post(
        f"{API}/ioe/optimizations",
        json={"analysis_id": analysis_id,
              "resource_capacities": {"FHSA_ROOM": "12000"}},
        headers=_auth(uid))
    assert r.status_code == 201, r.text
    run_id = uuid.UUID(r.json()["run_id"])
    from app.database.models import ResourceLedgerEntry, StrategyPortfolio

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        portfolio = await s.scalar(select(StrategyPortfolio).where(
            StrategyPortfolio.run_id == run_id))
        entries = list(await s.scalars(select(ResourceLedgerEntry).where(
            ResourceLedgerEntry.portfolio_id == portfolio.id)))
    ledger = {e.resource_code: e.capacity for e in entries}
    assert ledger.get("FHSA_ROOM") == Decimal("12000.00")


# ---------------------------------------------------------------- H3B -------
@pytest.mark.asyncio
async def test_actual_contributions_enter_the_baseline_analysis(client):
    uid = await _user(employment="80000")
    for body in (
        {"tax_year": 2025, "registered_type": "RRSP", "contributions_ytd": "5000"},
        {"tax_year": 2025, "registered_type": "FHSA", "contributions_ytd": "3000"},
    ):
        r = await client.post(f"{API}/financials/registered-accounts",
                              json=body, headers=_auth(uid))
        assert r.status_code == 201, r.text

    r = await client.post(f"{API}/analysis", json={"tax_year": 2025}, headers=_auth(uid))
    assert r.status_code == 201
    assert Decimal(r.json()["taxable_income"]) == Decimal("72000.00")

    listing = await client.get(f"{API}/financials/registered-accounts",
                               params={"tax_year": 2025}, headers=_auth(uid))
    assert listing.status_code == 200
    assert {row["registered_type"] for row in listing.json()} == {"RRSP", "FHSA"}


@pytest.mark.asyncio
async def test_scenario_levers_remain_hypothetical_facts_stay_untouched(client):
    uid = await _user(employment="80000")
    analysis_id = await _analysis(client, uid)
    r = await client.post(
        f"{API}/ioe/scenarios",
        json={"analysis_id": analysis_id, "label": "hypothetical",
              "levers": [{"lever_code": "INCREASE_RRSP_DEDUCTION",
                          "parameters": {"amount": "5000"}}]},
        headers=_auth(uid))
    assert r.status_code == 201, r.text
    listing = await client.get(f"{API}/financials/registered-accounts",
                               params={"tax_year": 2025}, headers=_auth(uid))
    assert listing.json() == []      # the what-if recorded no taxpayer fact

    rerun = await client.post(f"{API}/analysis", json={"tax_year": 2025},
                              headers=_auth(uid))
    assert Decimal(rerun.json()["taxable_income"]) == Decimal("80000.00")


@pytest.mark.asyncio
async def test_negative_contribution_is_refused_by_the_contract(client):
    uid = await _user()
    r = await client.post(
        f"{API}/financials/registered-accounts",
        json={"tax_year": 2025, "registered_type": "RRSP",
              "contributions_ytd": "-1"},
        headers=_auth(uid))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_registered_accounts_are_tenant_isolated(client):
    owner = await _user()
    await client.post(f"{API}/financials/registered-accounts",
                      json={"tax_year": 2025, "registered_type": "FHSA",
                            "contributions_ytd": "4000"},
                      headers=_auth(owner))
    other = await _user()
    listing = await client.get(f"{API}/financials/registered-accounts",
                               params={"tax_year": 2025}, headers=_auth(other))
    assert listing.json() == []


# ---------------------------------------------------------------- H4 --------
async def _fhsa_scenario(client, uid, analysis_id, amount, assumptions=None):
    body = {"analysis_id": analysis_id, "label": f"fhsa {amount}",
            "levers": [{"lever_code": "INCREASE_FHSA_DEDUCTION",
                        "parameters": {"amount": str(amount)}}]}
    if assumptions is not None:
        body["assumptions"] = assumptions
    return await client.post(f"{API}/ioe/scenarios", json=body, headers=_auth(uid))


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", ["5000", "8000"])
async def test_within_room_fhsa_scenarios_seal_an_explicit_room_assumption(
    client, amount
):
    uid = await _user()
    analysis_id = await _analysis(client, uid)
    r = await _fhsa_scenario(client, uid, analysis_id, amount)
    assert r.status_code == 201, r.text
    sc = r.json()
    codes = {a["assumption_code"]: a for a in sc["assumptions"]}
    room = codes["CONTRIBUTION_ROOM_AVAILABLE"]
    assert Decimal(str(room["value_number"])) == Decimal("8000")
    assert room["source"] == "platform"

    # The scenario flow's assumption storage is ScenarioAssumption rows — the
    # rows the API read back above. Prove the persisted, sealed record exists
    # (the original defect was a contribution scenario with NO stored
    # assumption of any kind).
    from app.database.models import ScenarioAssumption

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        stored = list(await s.scalars(select(ScenarioAssumption).where(
            ScenarioAssumption.scenario_id == uuid.UUID(sc["id"]))))
    assert [a.assumption_code for a in stored] == ["CONTRIBUTION_ROOM_AVAILABLE"]
    assert stored[0].value_number == Decimal("8000.00")


@pytest.mark.asyncio
async def test_over_room_fhsa_scenario_is_refused_not_sealed(client):
    uid = await _user()
    analysis_id = await _analysis(client, uid)
    r = await _fhsa_scenario(client, uid, analysis_id, "9000")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "invalid_scenario_specification" in detail
    assert "FHSA_ROOM" in detail and "CONTRIBUTION_ROOM_AVAILABLE" in detail


@pytest.mark.asyncio
async def test_declared_carry_forward_room_permits_a_larger_contribution(client):
    uid = await _user()
    analysis_id = await _analysis(client, uid)
    r = await _fhsa_scenario(
        client, uid, analysis_id, "12000",
        assumptions=[{"assumption_code": "CONTRIBUTION_ROOM_AVAILABLE",
                      "value_number": "12000", "materiality": "high"}])
    assert r.status_code == 201, r.text
    codes = {a["assumption_code"]: a for a in r.json()["assumptions"]}
    assert Decimal(str(codes["CONTRIBUTION_ROOM_AVAILABLE"]["value_number"])) \
        == Decimal("12000")


@pytest.mark.asyncio
async def test_rrsp_lever_states_its_assumed_room_explicitly(client):
    uid = await _user()
    analysis_id = await _analysis(client, uid)
    r = await client.post(
        f"{API}/ioe/scenarios",
        json={"analysis_id": analysis_id, "label": "rrsp",
              "levers": [{"lever_code": "INCREASE_RRSP_DEDUCTION",
                          "parameters": {"amount": "5000"}}]},
        headers=_auth(uid))
    assert r.status_code == 201, r.text
    codes = {a["assumption_code"]: a for a in r.json()["assumptions"]}
    room = codes["CONTRIBUTION_ROOM_AVAILABLE"]
    assert Decimal(str(room["value_number"])) == Decimal("5000")
    assert room["certainty"] == "platform_default"
