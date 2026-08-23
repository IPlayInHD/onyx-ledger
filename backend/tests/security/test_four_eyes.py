"""Government ingestion + four-eyes publishing governance.

A rule ingested by admin A cannot be published without a DIFFERENT admin (B)
approving it; A cannot approve their own submission; publishing without an
approved change request is rejected.
"""
import json
import uuid

import pytest

from app.core.security.jwt import create_admin_token
from app.database.session import unit_of_work
from app.services.admin.service import AdminService
from tests.conftest import register_verified


async def _make_admin(email: str, roles: list[str]) -> str:
    async with unit_of_work(actor_type="system") as s:
        admin = await AdminService(s).create_admin(email, "adminpass1", roles)
        aid = admin.id
    return create_admin_token(aid)


def _dataset(code: str) -> dict:
    return {
        "format": "json",
        "payload": json.dumps([{
            "code": code, "name": "Test Credit", "category": "credit",
            "jurisdiction": "FED", "tax_year": "2025",
            "description": "An ingested test credit.", "source_url": "https://canada.ca",
        }]),
    }


@pytest.mark.asyncio
async def test_four_eyes_ingest_approve_publish(client):
    a = {"Authorization": f"Bearer {await _make_admin(f'a_{uuid.uuid4().hex[:6]}@onyx.io', ['superadmin'])}"}
    b = {"Authorization": f"Bearer {await _make_admin(f'b_{uuid.uuid4().hex[:6]}@onyx.io', ['superadmin'])}"}
    code = f"TESTCRED_{uuid.uuid4().hex[:6].upper()}"

    # A ingests -> draft version + pending change request
    r = await client.post("/api/v1/admin/ingestion/jobs", headers=a, json=_dataset(code))
    assert r.status_code == 201, r.text
    created = r.json()["created"][0]
    version_id, cr_id = created["version_id"], created["change_request_id"]

    # A cannot approve their OWN submission (four-eyes)
    r = await client.post(f"/api/v1/admin/change-requests/{cr_id}/approve", headers=a)
    assert r.status_code == 403, r.text

    # Cannot publish before approval
    r = await client.post(f"/api/v1/admin/rules/{version_id}/publish", headers=b)
    assert r.status_code == 403

    # B approves, then B publishes
    r = await client.post(f"/api/v1/admin/change-requests/{cr_id}/approve", headers=b)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    r = await client.post(f"/api/v1/admin/rules/{version_id}/publish", headers=b)
    assert r.status_code == 200 and r.json()["status"] == "published"

    # the newly published rule is now visible to end users via the KB
    email = f"u_{uuid.uuid4().hex[:6]}@t.ca"
    tok = await register_verified(client, email, "supersecret1")
    r = await client.get("/api/v1/tax/rules?tax_year=2025",
                         headers={"Authorization": f"Bearer {tok}"})
    assert any(rule["code"] == code for rule in r.json())


@pytest.mark.asyncio
async def test_non_admin_token_rejected(client):
    # a normal user access token must not reach admin endpoints
    email = f"nu_{uuid.uuid4().hex[:6]}@t.ca"
    tok = await register_verified(client, email, "supersecret1")
    r = await client.post("/api/v1/admin/ingestion/jobs",
                          headers={"Authorization": f"Bearer {tok}"},
                          json=_dataset("NOPE"))
    assert r.status_code == 401  # admin scope required
