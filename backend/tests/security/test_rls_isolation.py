
"""Security: Row-Level Security must prevent cross-user data access.

User A creates income; user B must not see it, even though both hit the same
tables through the same app role — because the DB RLS policy filters on the
per-transaction app.user_id GUC set by the Unit of Work.
"""
import uuid

import pytest

from tests.conftest import register_verified


async def _register_login(client, email):
    token = await register_verified(client, email, "supersecret1")
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_user_cannot_see_another_users_income(client):
    a = await _register_login(client, f"a_{uuid.uuid4().hex[:8]}@test.ca")
    b = await _register_login(client, f"b_{uuid.uuid4().hex[:8]}@test.ca")

    # A adds income
    r = await client.post("/api/v1/financials/income", headers=a,
                         json={"tax_year": 2025, "income_type_code": "employment", "amount": "50000.00"})
    assert r.status_code == 201

    # A sees exactly 1 income row
    ra = await client.get("/api/v1/financials/income?tax_year=2025", headers=a)
    assert ra.status_code == 200 and len(ra.json()) == 1

    # B sees ZERO — RLS isolates them
    rb = await client.get("/api/v1/financials/income?tax_year=2025", headers=b)
    assert rb.status_code == 200 and len(rb.json()) == 0
