"""`/readyz` must answer the question and nothing else.

The endpoint is unauthenticated by necessity — a load balancer has to reach it,
which means anybody can. It used to return `str(e)` from a failed database
connection, and that string is not a tidy sentence: SQLAlchemy and asyncpg put
the host, the port, the database name and the runtime username into it, and a
DSN parse failure can carry the password. An unauthenticated prober could ask a
struggling service to describe its private network.

The prober needs the verdict. The operator needs the reason, and gets it from
the log, where it is already correlated with the request.

Both directions are driven by a substituted engine rather than a live database.
That is not convenience: the point is to control exactly what the failure says,
so the assertions are about disclosure rather than about whatever error this
particular machine happens to produce.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

#: Fragments a real connection error carries. The failure below is built to
#: contain every one of them, so "nothing leaked" is a claim with teeth.
SECRET_SHAPED = (
    "db.internal.onyx",
    "5432",
    "onyx_app_rw",
    "hunter2",
    "postgresql+asyncpg",
    "10.0.3.14",
)

BOOM = (
    'connection to server at "db.internal.onyx" (10.0.3.14), port 5432 failed: '
    'FATAL: password authentication failed for user "onyx_app_rw" '
    "(postgresql+asyncpg://onyx_app_rw:hunter2@db.internal.onyx:5432/onyx)"
)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def _engine_that(*, fails: bool) -> MagicMock:
    """A stand-in for the async engine.

    `AsyncEngine.connect` is read-only, so the engine OBJECT is replaced rather
    than one of its attributes.
    """

    @asynccontextmanager
    async def connect():
        if fails:
            raise RuntimeError(BOOM)
        conn = MagicMock()

        async def execute(_statement):
            return MagicMock()

        conn.execute = execute
        yield conn

    engine = MagicMock()
    engine.connect = connect
    return engine


def test_ready_when_the_database_answers(client: TestClient) -> None:
    """Non-vacuity: the endpoint must be capable of saying yes.

    Without this, an endpoint that returned 503 unconditionally would satisfy
    every disclosure assertion below while being completely broken.
    """
    with patch("app.database.session.engine", _engine_that(fails=False)):
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_failure_reports_not_ready_without_saying_why(client: TestClient) -> None:
    with patch("app.database.session.engine", _engine_that(fails=True)):
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}

    # Whole-body check rather than field-by-field: a contributor adding a
    # `reason` or `hint` key would slip past an assertion that only looked at
    # `detail`.
    body = json.dumps(response.json())
    for fragment in SECRET_SHAPED:
        assert fragment not in body, f"{fragment!r} leaked into the readiness response"


def test_the_operator_still_gets_the_reason(client: TestClient) -> None:
    """Silence is not the goal; disclosure to the wrong audience is the problem.

    A fix that dropped the exception entirely would pass the test above and
    leave an operator staring at a 503 with no way to find out why.
    """
    with patch("app.main.log") as log:
        with patch("app.database.session.engine", _engine_that(fails=True)):
            client.get("/readyz")

    assert log.warning.called, "the failure reason must still reach the log"
    assert log.warning.call_args.args[0] == "readiness_check_failed"
    assert log.warning.call_args.kwargs.get("exc_info") is True
