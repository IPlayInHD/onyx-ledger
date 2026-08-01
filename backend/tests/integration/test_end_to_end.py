"""End-to-end: register → profile → income/expense → analysis → recommendations.

Exercises the full spine against real PostgreSQL (RLS + audit triggers live).
"""
import uuid

import pytest


@pytest.mark.asyncio
async def test_full_flow(client):
    email = f"user_{uuid.uuid4().hex[:8]}@test.ca"

    # register
    r = await client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret1"})
    assert r.status_code == 201, r.text

    # login
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": "supersecret1"})
    assert r.status_code == 200, r.text
    tokens = r.json()
    auth = {"Authorization": f"Bearer {tokens['access_token']}"}

    # set tax profile (Ontario)
    r = await client.put("/api/v1/users/me/tax-profile", headers=auth,
                         json={"province_code": "ON", "marital_status": "single"})
    assert r.status_code == 200, r.text
    assert r.json()["province_code"] == "ON"

    # add income + a medical expense (should trigger the seeded credit rule)
    r = await client.post("/api/v1/financials/income", headers=auth,
                         json={"tax_year": 2025, "income_type_code": "employment", "amount": "75000.00"})
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/financials/expenses", headers=auth,
                         json={"tax_year": 2025, "expense_category_code": "medical", "amount": "3000.00"})
    assert r.status_code == 201, r.text

    # run analysis
    r = await client.post("/api/v1/analysis", headers=auth, json={"tax_year": 2025})
    assert r.status_code == 201, r.text
    analysis = r.json()
    assert analysis["province_code"] == "ON"
    assert float(analysis["taxable_income"]) == pytest.approx(75000, abs=1)
    assert float(analysis["estimated_tax"]) > 0

    # recommendations should include the medical expense credit (from the DB rule)
    r = await client.get(f"/api/v1/recommendations?analysis_id={analysis['id']}", headers=auth)
    assert r.status_code == 200, r.text
    recs = r.json()
    assert any("medical" in rec["opportunity_code"].lower() for rec in recs), recs
    med = next(rec for rec in recs if "medical" in rec["opportunity_code"].lower())
    # impact = max(0, 3000 - min(75000*0.03, 2834)) * 0.145 = (3000-2250)*0.145 = 108.75
    assert float(med["estimated_impact"]) == pytest.approx(108.75, abs=0.5)


@pytest.mark.asyncio
async def test_refresh_rotation(client):
    email = f"user_{uuid.uuid4().hex[:8]}@test.ca"
    await client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret1"})
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": "supersecret1"})
    refresh = r.json()["refresh_token"]

    # first refresh works and rotates
    r1 = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert r1.status_code == 200
    # reusing the old (now revoked) refresh token fails
    r2 = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert r2.status_code == 401


@pytest.mark.asyncio
async def test_unauthenticated_rejected(client):
    r = await client.get("/api/v1/users/me")
    assert r.status_code == 401
