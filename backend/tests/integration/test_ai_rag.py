"""AI RAG: a question retrieves the seeded rule, the answer is grounded and
persisted with a citation to the rule version, and the guardrail holds. Runs
against real PostgreSQL + pgvector."""
import uuid

import pytest


async def _auth(client, email):
    await client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret1"})
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": "supersecret1"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_ask_grounds_and_cites(client):
    auth = await _auth(client, f"ai_{uuid.uuid4().hex[:8]}@test.ca")

    # give the user a real analysis so verified figures exist
    await client.put("/api/v1/users/me/tax-profile", headers=auth,
                     json={"province_code": "ON", "marital_status": "single"})
    await client.post("/api/v1/financials/income", headers=auth,
                      json={"tax_year": 2025, "income_type_code": "employment", "amount": "75000"})
    await client.post("/api/v1/financials/expenses", headers=auth,
                      json={"tax_year": 2025, "expense_category_code": "medical", "amount": "3000"})
    await client.post("/api/v1/analysis", headers=auth, json={"tax_year": 2025})

    # ask about medical expenses -> should retrieve the seeded Medical Expense Credit
    r = await client.post("/api/v1/ai/ask", headers=auth,
                          json={"question": "Can I claim my medical expenses?", "tax_year": 2025})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["grounded"] is True
    assert len(body["citations"]) >= 1                 # cited a rule version
    assert "medical" in body["answer"].lower()          # grounded in the retrieved rule

    # the turn is persisted (user + assistant messages)
    conv_id = body["conversation_id"]
    r = await client.get(f"/api/v1/ai/conversations/{conv_id}/messages", headers=auth)
    roles = [m["role"] for m in r.json()]
    assert roles == ["user", "assistant"]


@pytest.mark.asyncio
async def test_conversation_is_user_isolated(client):
    a = await _auth(client, f"a_{uuid.uuid4().hex[:8]}@test.ca")
    b = await _auth(client, f"b_{uuid.uuid4().hex[:8]}@test.ca")
    r = await client.post("/api/v1/ai/ask", headers=a,
                          json={"question": "What deductions apply?", "tax_year": 2025})
    conv_id = r.json()["conversation_id"]
    # B cannot read A's conversation (RLS + ownership)
    r = await client.get(f"/api/v1/ai/conversations/{conv_id}/messages", headers=b)
    assert r.status_code == 404
