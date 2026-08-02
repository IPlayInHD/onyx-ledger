"""TKMS admin API over HTTP — import → govern → publish → trace.

Drives the real FastAPI app (admin JWT, permission checks, audit triggers, the
four-eyes DB CHECK) end to end through the portal endpoints.
"""
import json
import uuid

import pytest

from app.core.security.jwt import create_admin_token
from app.database.session import unit_of_work
from app.services.admin.service import AdminService


async def _admin_token() -> str:
    async with unit_of_work(actor_type="system") as s:
        admin = await AdminService(s).create_admin(
            f"api_{uuid.uuid4().hex[:8]}@onyx.io", "adminpass1", ["kb_admin"]
        )
        aid = admin.id
    return create_admin_token(aid)


def _payload(code: str, max_amount: str = "2000") -> str:
    return json.dumps([{
        "rule_code": code, "name": "API credit", "category": "credit",
        "jurisdiction": "FED", "tax_year": 2025, "max_amount": max_amount,
        "description": "Imported via the TKMS API.",
        "source_url": "https://canada.ca/x", "legislation_reference": "ITA s.99",
    }])


@pytest.mark.asyncio
async def test_import_govern_publish_and_trace(client):
    ta = {"Authorization": f"Bearer {await _admin_token()}"}
    tb = {"Authorization": f"Bearer {await _admin_token()}"}
    code = f"API_{uuid.uuid4().hex[:8].upper()}"

    # import (runs the governed pipeline) → validated draft
    r = await client.post("/api/v1/tkms/imports", headers=ta,
                          json={"source_org": "CRA", "format": "json",
                                "payload": _payload(code), "tax_year": 2025})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["validation_status"] == "passed"
    version_id = body["draft_version_ids"][0]

    # extracted rules visible
    r = await client.get(f"/api/v1/tkms/imports/{body['job_id']}/extracted", headers=ta)
    assert r.status_code == 200 and r.json()[0]["payload"]["rule_code"] == code

    # validation report visible, no errors
    r = await client.get(f"/api/v1/tkms/imports/{body['job_id']}/validation", headers=ta)
    assert r.status_code == 200 and r.json()["status"] == "passed"

    # four-eyes: A submits, A cannot approve own, B approves, B publishes
    r = await client.post(f"/api/v1/tkms/versions/{version_id}/submit", headers=ta)
    assert r.status_code == 200, r.text

    r = await client.post(f"/api/v1/tkms/versions/{version_id}/approve", headers=ta)
    assert r.status_code == 403, r.text                      # submitter != approver

    r = await client.post(f"/api/v1/tkms/versions/{version_id}/approve", headers=tb)
    assert r.status_code == 200, r.text

    r = await client.post(f"/api/v1/tkms/versions/{version_id}/publish", headers=tb)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"

    # traceability: published version resolves its full provenance chain
    r = await client.get(f"/api/v1/tkms/versions/{version_id}/trace", headers=ta)
    assert r.status_code == 200, r.text
    trace = r.json()
    assert trace["version"]["status"] == "published"
    assert trace["version"]["rule_code"] == code
    assert trace["import_job"] is not None
    assert trace["parser"]["name"] == "government_json"
    assert trace["extracted_rule"]["payload"]["rule_code"] == code
    assert trace["validation"]["status"] == "passed"
    assert trace["publication"] is not None


@pytest.mark.asyncio
async def test_permission_denied_without_role(client):
    # an admin with no TKMS role cannot import
    async with unit_of_work(actor_type="system") as s:
        admin = await AdminService(s).create_admin(
            f"norole_{uuid.uuid4().hex[:8]}@onyx.io", "adminpass1", []
        )
        aid = admin.id
    headers = {"Authorization": f"Bearer {create_admin_token(aid)}"}
    r = await client.post("/api/v1/tkms/imports", headers=headers,
                          json={"source_org": "CRA", "format": "json",
                                "payload": _payload("NOPE"), "tax_year": 2025})
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_stats_and_parsers(client):
    headers = {"Authorization": f"Bearer {await _admin_token()}"}
    r = await client.get("/api/v1/tkms/parsers", headers=headers)
    assert r.status_code == 200
    names = {p["name"] for p in r.json()}
    assert {"government_csv", "government_json", "manual"} <= names

    r = await client.get("/api/v1/tkms/stats", headers=headers)
    assert r.status_code == 200
    assert "jobs_by_status" in r.json()
