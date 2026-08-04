"""Integrity through the API, and replay determinism across processes.

Two things the in-process tests cannot show: what a client actually receives,
and that a replay's identity does not depend on this interpreter's hash seed.
"""
import json
import os
import subprocess
import sys
import textwrap
import uuid

import pytest

from app.core.security.jwt import create_access_token
from app.database.session import engine, unit_of_work
from app.services.ioe.domain.integrity import IntegrityStatus, integrity_warning
from app.services.ioe.replay import IntegrityVerificationService
from tests.integration.test_integrity_verification import (
    _portfolio_id,
    _sealed_optimization,
    _sealed_scenario,
)


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    await engine.dispose()


def _auth(user_id: uuid.UUID) -> dict:
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


# ---- API --------------------------------------------------------------------
@pytest.mark.asyncio
async def test_verified_metadata_round_trips_through_the_api(client):
    uid, _, run_id = await _sealed_optimization()
    await IntegrityVerificationService(uid).verify("optimization", run_id)

    response = await client.get(
        f"/api/v1/ioe/optimization/{run_id}/integrity", headers=_auth(uid))
    assert response.status_code == 200
    body = response.json()
    assert body["integrity_status"] == "verified"
    assert body["integrity_state"] == "verified"
    assert body["integrity_reason_code"] == "NONE"
    assert body["last_integrity_checked_at"] is not None
    assert body["integrity_warning"] == integrity_warning(IntegrityStatus.VERIFIED)


@pytest.mark.asyncio
async def test_the_api_never_exposes_a_hash_to_an_ordinary_user(client):
    uid, _, run_id = await _sealed_optimization()
    result = await IntegrityVerificationService(uid).verify("optimization", run_id)
    headers = _auth(uid)

    metadata = (await client.get(
        f"/api/v1/ioe/optimization/{run_id}/integrity", headers=headers)).json()
    verify = (await client.post(
        f"/api/v1/ioe/optimization/{run_id}/integrity/verify", headers=headers)).json()

    for body in (metadata, verify):
        serialized = json.dumps(body)
        assert "expected_result_hash" not in serialized
        assert "actual_result_hash" not in serialized
        assert "hash" not in serialized.replace("integrity_warning", "")
    assert verify["check_id"] != str(result.check_id)      # a new check appended


@pytest.mark.asyncio
async def test_an_explicit_verification_returns_a_typed_result(client):
    uid, analysis_id, _ = await _sealed_optimization()
    scenario_id = await _sealed_scenario(uid, analysis_id)

    response = await client.post(
        f"/api/v1/ioe/scenario/{scenario_id}/integrity/verify",
        headers=_auth(uid),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["entity_type"] == "scenario"
    assert body["integrity_status"] in {"verified", "unavailable", "mismatch"}
    assert "not a statement of tax correctness" in body["disclaimer"]


@pytest.mark.asyncio
async def test_the_unavailable_and_mismatch_wordings_are_distinct(client):
    verified = integrity_warning(IntegrityStatus.VERIFIED)
    unavailable = integrity_warning(IntegrityStatus.UNAVAILABLE)
    mismatch = integrity_warning(IntegrityStatus.MISMATCH)
    assert len({verified, unavailable, mismatch}) == 3
    assert "could not be reproduced" in mismatch
    assert "pinned dependency is unavailable" in unavailable


@pytest.mark.asyncio
async def test_an_integrity_read_cannot_reveal_another_users_entity(client):
    uid_a, _, run_id = await _sealed_optimization()
    uid_b, _, _ = await _sealed_optimization()

    seen = await client.get(
        f"/api/v1/ioe/optimization/{run_id}/integrity",
        headers=_auth(uid_b))
    missing = await client.get(
        f"/api/v1/ioe/optimization/{uuid.uuid4()}/integrity",
        headers=_auth(uid_b))
    # identical answers: existence is not disclosed
    assert seen.status_code == missing.status_code == 404


@pytest.mark.asyncio
async def test_a_portfolio_detail_carries_its_integrity_metadata(client):
    uid, _, run_id = await _sealed_optimization()
    portfolio_id = await _portfolio_id(uid, run_id)
    await IntegrityVerificationService(uid).verify("portfolio", portfolio_id)

    response = await client.get(
        f"/api/v1/ioe/runs/{run_id}/portfolio", headers=_auth(uid))
    assert response.status_code == 200
    integrity = response.json()["integrity"]
    assert integrity["integrity_status"] == "verified"
    assert integrity["integrity_state"] == "verified"


@pytest.mark.asyncio
async def test_an_unknown_entity_type_is_rejected_before_any_lookup(client):
    uid, _, run_id = await _sealed_optimization()
    response = await client.get(
        f"/api/v1/ioe/nonsense/{run_id}/integrity", headers=_auth(uid))
    assert response.status_code == 404


# ---- determinism ------------------------------------------------------------
_SUBPROCESS = textwrap.dedent(
    """
    import asyncio, os, sys, uuid
    os.environ.setdefault("ONYX_DATABASE_URL", sys.argv[2])
    os.environ.setdefault("ONYX_JWT_SECRET", "test-secret-at-least-32-bytes-long-000")
    from app.database.session import engine
    from app.services.ioe.replay.services import OptimizationReplayService

    async def main():
        run_id = uuid.UUID(sys.argv[1])
        uid = uuid.UUID(sys.argv[3])
        outcome = await OptimizationReplayService(uid).replay(run_id)
        print(outcome.actual_hash)
        await engine.dispose()

    asyncio.run(main())
    """
)


@pytest.mark.asyncio
async def test_replay_produces_the_same_hash_across_processes_and_hash_seeds(tmp_path):
    """A replay's identity must not depend on this interpreter's hash seed.

    Dict iteration order is seeded per process, so a canonicalizer that leaked
    insertion order would produce a different digest under a different
    PYTHONHASHSEED — and would do it intermittently, in production, months
    later.
    """
    uid, _, run_id = await _sealed_optimization()
    await engine.dispose()

    script = tmp_path / "replay_once.py"
    script.write_text(_SUBPROCESS)
    dsn = os.environ["ONYX_DATABASE_URL"]

    hashes = set()
    for seed in ("0", "1", "42"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": os.getcwd()}
        proc = subprocess.run(
            [sys.executable, str(script), str(run_id), dsn, str(uid)],
            capture_output=True, text=True, env=env, timeout=180, check=False,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        hashes.add(proc.stdout.strip().splitlines()[-1])

    assert len(hashes) == 1, f"replay hash varied by hash seed: {hashes}"

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        from app.database.models import OptimizationRun

        run = await s.get(OptimizationRun, run_id)
        assert hashes.pop() == run.optimization_result_hash
