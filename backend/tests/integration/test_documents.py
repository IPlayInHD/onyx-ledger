"""Document pipeline: upload -> process (structured) -> confirm -> income row
appears and feeds an analysis. Runs against real PostgreSQL (RLS live)."""
import uuid

import pytest


async def _auth(client, email):
    await client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret1"})
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": "supersecret1"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_document_upload_process_confirm(client):
    auth = await _auth(client, f"doc_{uuid.uuid4().hex[:8]}@test.ca")

    # 1) create upload -> presigned url + document row
    r = await client.post("/api/v1/documents", headers=auth,
                         json={"document_type_code": "T4", "filename": "t4.pdf", "tax_year": 2025})
    assert r.status_code == 201, r.text
    doc_id = r.json()["document_id"]
    assert r.json()["upload_url"].startswith("local://")

    # 2) process (structured fields path) -> extraction with fields
    r = await client.post(f"/api/v1/documents/{doc_id}/process", headers=auth,
                         json={"fields": {"employmentIncome": "68000", "taxWithheld": "11800"}})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "processed"
    assert any(f["name"] == "employmentIncome" for f in r.json()["fields"])

    # 3) confirm -> creates an income row from the extraction
    r = await client.post(f"/api/v1/documents/{doc_id}/confirm", headers=auth,
                         json={"tax_year": 2025})
    assert r.status_code == 200, r.text
    assert r.json()["created"]["income"] == 1

    # 4) the income now shows up and drives an analysis
    r = await client.get("/api/v1/financials/income?tax_year=2025", headers=auth)
    assert len(r.json()) == 1
    r = await client.post("/api/v1/analysis", headers=auth, json={"tax_year": 2025})
    assert r.status_code == 201
    assert float(r.json()["taxable_income"]) == pytest.approx(68000, abs=1)


@pytest.mark.asyncio
async def test_text_ocr_path(client):
    auth = await _auth(client, f"ocr_{uuid.uuid4().hex[:8]}@test.ca")
    r = await client.post("/api/v1/documents", headers=auth,
                         json={"document_type_code": "MEDICAL", "filename": "rx.jpg", "tax_year": 2025})
    doc_id = r.json()["document_id"]
    r = await client.post(f"/api/v1/documents/{doc_id}/process", headers=auth,
                         json={"text": "Pharmacy\nTotal amount 3,000.00"})
    assert r.status_code == 200
    assert r.json()["fields"][0]["name"] == "medicalExpenses"
