"""Admission behaviour through the real HTTP surface (Entry 10).

The service tests prove the mechanism; these prove a caller actually experiences
it — the right status code, the right headers, and no leak of anything a caller
should not know.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.database.session import unit_of_work
from app.services.admission.policy import POLICIES, OperationClass
from app.services.admission.service import AdmissionService


async def _register(client) -> tuple[str, uuid.UUID]:
    email = f"admission_{uuid.uuid4().hex[:12]}@test.ca"
    response = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": "supersecret1"}
    )
    assert response.status_code == 201, response.text
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]

    me = await client.get("/api/v1/users/me",
                          headers={"Authorization": f"Bearer {token}"})
    return token, uuid.UUID(me.json()["id"])


@pytest.mark.asyncio
async def test_a_rate_rejection_is_429_with_retry_after(client):
    """The caller asked too much: 429, and a header saying when to come back."""
    token, user_id = await _register(client)
    headers = {"Authorization": f"Bearer {token}"}

    # Exhaust the class's allowance directly, so the test does not depend on how
    # many analyses happen to be runnable.
    policy = POLICIES[OperationClass.ANALYSIS_RUN]
    allowance = policy.rate_allowance
    assert allowance is not None
    async with unit_of_work(actor_type="admin") as session:
        service = AdmissionService(session)
        for _ in range(allowance):
            ticket = await service.admit(
                OperationClass.ANALYSIS_RUN, scope_id=str(user_id))
            await service.release_ticket(ticket)

    response = await client.post(
        "/api/v1/analysis", json={"tax_year": 2025}, headers=headers)

    assert response.status_code == 429
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["Retry-After"] == str(policy.retry_after_seconds)

    body = response.json()
    assert body["status"] == 429
    assert body["operation_code"] == "ANALYSIS_RUN"
    assert body["error_code"] == "USER_RATE_LIMIT"
    assert body["retry_after_seconds"] == policy.retry_after_seconds
    assert body["correlation_id"]


@pytest.mark.asyncio
async def test_a_rejection_body_reveals_nothing_about_the_platform(client):
    """A rejection is a closed code, not a capacity report."""
    token, user_id = await _register(client)
    headers = {"Authorization": f"Bearer {token}"}

    allowance = POLICIES[OperationClass.ANALYSIS_RUN].rate_allowance
    assert allowance is not None
    async with unit_of_work(actor_type="admin") as session:
        service = AdmissionService(session)
        for _ in range(allowance):
            await service.release_ticket(await service.admit(
                OperationClass.ANALYSIS_RUN, scope_id=str(user_id)))

    body = (await client.post(
        "/api/v1/analysis", json={"tax_year": 2025}, headers=headers)).json()

    # Exactly the documented fields, and nothing that describes the platform.
    assert set(body) == {
        "type", "title", "status", "detail", "correlation_id",
        "operation_code", "error_code", "retry_after_seconds",
    }
    serialized = str(body).lower()
    for leak in ("worker", "queue", "redis", "postgres", "capacity", "global",
                 "active_count", "lease"):
        assert leak not in serialized, f"rejection body leaked {leak!r}"


@pytest.mark.asyncio
async def test_an_ordinary_request_is_unaffected(client):
    """Admission must be invisible when nothing is wrong."""
    token, _ = await _register(client)
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.post(
        "/api/v1/analysis", json={"tax_year": 2025}, headers=headers)
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_concurrent_identical_analyses_do_not_both_run(client):
    """A double-click, or a client retrying after a timeout. One runs; the other
    gets a deterministic conflict rather than silently running a second full
    engine pass over the same tax year."""
    token, _ = await _register(client)
    headers = {"Authorization": f"Bearer {token}"}

    responses = await asyncio.gather(*(
        client.post("/api/v1/analysis", json={"tax_year": 2025}, headers=headers)
        for _ in range(4)
    ))
    codes = sorted(r.status_code for r in responses)

    created = [r for r in responses if r.status_code == 201]
    refused = [r for r in responses if r.status_code in (409, 429)]
    assert len(created) >= 1, f"nothing ran: {codes}"
    assert len(created) + len(refused) == 4, f"unexpected statuses: {codes}"
    # Never two simultaneous full analyses for the same (user, tax year).
    assert len(created) == 1, f"more than one analysis ran concurrently: {codes}"


@pytest.mark.asyncio
async def test_document_upload_refuses_an_unsupported_content_type(client):
    token, _ = await _register(client)
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.post(
        "/api/v1/documents",
        json={
            "document_type_code": "T4",
            "filename": "payload.exe",
            "mime_type": "application/x-msdownload",
        },
        headers=headers,
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_document_upload_refuses_an_oversized_declaration(client):
    from app.services.admission.limits import MAX_DOCUMENT_BYTES

    token, _ = await _register(client)
    headers = {"Authorization": f"Bearer {token}"}

    at_limit = await client.post(
        "/api/v1/documents",
        json={"document_type_code": "T4", "filename": "return.pdf",
              "mime_type": "application/pdf", "byte_size": MAX_DOCUMENT_BYTES},
        headers=headers,
    )
    assert at_limit.status_code == 201, at_limit.text

    over = await client.post(
        "/api/v1/documents",
        json={"document_type_code": "T4", "filename": "return.pdf",
              "mime_type": "application/pdf", "byte_size": MAX_DOCUMENT_BYTES + 1},
        headers=headers,
    )
    assert over.status_code == 422, over.text


@pytest.mark.asyncio
async def test_an_overlong_filename_is_refused_by_the_schema(client):
    from app.services.admission.limits import MAX_FILENAME_LENGTH

    token, _ = await _register(client)
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.post(
        "/api/v1/documents",
        json={"document_type_code": "T4",
              "filename": "x" * (MAX_FILENAME_LENGTH + 1),
              "mime_type": "application/pdf"},
        headers=headers,
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_a_scenario_over_the_lever_limit_is_refused_before_any_work(client):
    """Small in bytes, explosive in work — the complexity bound, not the size
    bound, is what catches this."""
    from app.services.admission.limits import MAX_SCENARIO_LEVERS

    token, _ = await _register(client)
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.post(
        "/api/v1/ioe/scenarios",
        json={
            "analysis_id": str(uuid.uuid4()),
            "levers": [
                {"lever_code": "INCREASE_RRSP_DEDUCTION", "parameters": {"amount": "1"}}
                for _ in range(MAX_SCENARIO_LEVERS + 1)
            ],
        },
        headers=headers,
    )
    assert response.status_code == 422, response.text
