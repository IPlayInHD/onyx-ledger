"""The HTTP transaction contract: a success the client can see is durable.

    CLIENT_CAN_OBSERVE_SUCCESS  =>  TRANSACTION_COMMIT_COMPLETED

This file exists because that was false, in production, on every write in the
application, and because nothing else in the suite could have caught it.

WHY A REAL SOCKET SERVER. The rest of the suite drives the app through
`httpx.ASGITransport`, which awaits the whole ASGI call before returning — so
the dependency teardown has always finished by the time the test sees a
response, whatever order a real server would have used. `TestClient` behaves
the same way. Both report green against the broken architecture. The defect
lives in the handoff between the ASGI server writing the response to a socket
and the framework closing the exit stack that holds request-scoped `yield`
dependencies, so only a real server on a real socket can see it.

WHY THE COMMIT IS DELAYED. Not to create the defect — to make its window wide
enough that one request decides the question instead of needing a loaded
machine and a hundred attempts. With the delay removed these tests still pass
on a correct tree; without the fix they fail on every attempt rather than one
in three. That is the difference between a regression test and a coin toss.

Measured on the broken architecture, 150 ms delay: twelve of twelve
registrations returned 201 and were then refused at sign-in.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import socket
import threading
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import uvicorn

#: Long enough to lose the race decisively on a broken tree, short enough that
#: the whole module costs under a second of wall clock on a correct one.
COMMIT_DELAY_SECONDS = 0.15

PASSWORD = "supersecret1"


# --------------------------------------------------------------------------
# the server under test
# --------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Recorder:
    """Ordered events for one request, recorded server-side."""

    def __init__(self) -> None:
        self.events: list[str] = []
        #: Set by a test just before the request whose commit must fail, so the
        #: server's own start-up traffic is not caught by it.
        self.arm_failure = False

    def reset(self) -> None:
        self.events.clear()

    def add(self, name: str) -> None:
        self.events.append(name)


class CommitRefused(Exception):
    """Stands in for a commit that the database refuses at the last moment."""


def _build_app(recorder: _Recorder, fail_commit_on: str | None = None):
    from app.api import deps
    from app.database.session import unit_of_work as real_unit_of_work

    @asynccontextmanager
    async def slow_unit_of_work(
        user_id: uuid.UUID | None = None, actor_type: str = "system"
    ) -> AsyncIterator:
        async with real_unit_of_work(user_id=user_id, actor_type=actor_type) as session:
            try:
                yield session
            finally:
                # Still inside `session.begin()`, so this precedes the commit.
                recorder.add("COMMIT_STARTED")
                await asyncio.sleep(COMMIT_DELAY_SECONDS)
                if fail_commit_on is not None and recorder.arm_failure:
                    # Raised from inside the transaction scope, so SQLAlchemy
                    # rolls back exactly as it would for a real commit error.
                    recorder.add("COMMIT_REFUSED")
                    raise CommitRefused("the database refused the commit")
        recorder.add("COMMIT_FINISHED")

    original = deps.unit_of_work
    deps.unit_of_work = slow_unit_of_work
    try:
        from app.main import create_app

        app = create_app()
    finally:
        deps.unit_of_work = original

    # The dependencies captured the patched name when they were defined, so the
    # slow unit of work stays in force for this app instance only.
    deps.unit_of_work = slow_unit_of_work

    class RecordResponseStart:
        def __init__(self, inner) -> None:  # noqa: ANN001
            self.inner = inner

        async def __call__(self, scope, receive, send):  # noqa: ANN001, ANN201
            if scope["type"] != "http":
                return await self.inner(scope, receive, send)

            async def wrapped(message):  # noqa: ANN001, ANN202
                if message["type"] == "http.response.start":
                    recorder.add("HTTP_RESPONSE_START")
                await send(message)

            await self.inner(scope, receive, wrapped)

    return RecordResponseStart(app), original


class _LiveServer:
    """A real uvicorn server on a real port, in its own thread and loop."""

    def __init__(self, fail_commit_on: str | None = None) -> None:
        self.recorder = _Recorder()
        self.port = _free_port()
        self._app, self._original_uow = _build_app(self.recorder, fail_commit_on)
        self._server = uvicorn.Server(
            uvicorn.Config(self._app, host="127.0.0.1", port=self.port,
                           log_level="error", access_log=False)
        )
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        async def main() -> None:
            from app.database.privacy_session import dispose_all_engines
            from app.database.session import engine

            try:
                await self._server.serve()
            finally:
                # Pooled connections belong to THIS loop. Leaving them for the
                # next test's loop is the "attached to a different loop"
                # failure this repository has paid for repeatedly.
                await engine.dispose()
                await dispose_all_engines()

        asyncio.run(main())

    def __enter__(self) -> _LiveServer:
        self._thread.start()
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                self.get("/healthz")
                return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("the live server never became reachable")

    def __exit__(self, *exc: object) -> None:
        from app.api import deps

        self._server.should_exit = True
        self._thread.join(timeout=30)
        deps.unit_of_work = self._original_uow

    # ---------------------------------------------------------------- http --
    def _call(self, method: str, path: str, body: dict | None = None,
              token: str | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            headers = {"content-type": "application/json"}
            if token:
                headers["authorization"] = f"Bearer {token}"
            conn.request(method, path,
                         body=json.dumps(body) if body is not None else None,
                         headers=headers)
            response = conn.getresponse()
            raw = response.read()
            try:
                return response.status, (json.loads(raw) if raw else {})
            except json.JSONDecodeError:
                return response.status, {}
        finally:
            conn.close()

    def get(self, path: str, token: str | None = None) -> tuple[int, dict]:
        return self._call("GET", path, None, token)

    def post(self, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
        return self._call("POST", path, body, token)


@pytest.fixture(scope="module")
def live_server() -> object:
    with _LiveServer() as server:
        yield server


def _fresh_email(tag: str) -> str:
    return f"b5_{tag}_{uuid.uuid4().hex[:16]}@test.ca"


# --------------------------------------------------------------------------
# the invariant
# --------------------------------------------------------------------------
def test_a_success_the_client_can_see_is_a_write_the_next_request_can_see(
    live_server: _LiveServer,
) -> None:
    """The whole entry, in one assertion.

    Registration is the write and signing in is the independent read: the
    second request cannot succeed unless the first one's transaction is
    durable. No sleep between them, deliberately — the client is entitled to
    read its own write the instant it is told the write happened.
    """
    refused = []
    for _ in range(5):
        email = _fresh_email("rw")
        created, _ = live_server.post(
            "/api/v1/auth/register", {"email": email, "password": PASSWORD}
        )
        assert created == 201, "registration itself failed; the fixture is wrong"
        status, _ = live_server.post(
            "/api/v1/auth/login", {"email": email, "password": PASSWORD}
        )
        if status != 200:
            refused.append(status)

    assert not refused, (
        f"{len(refused)} of 5 sign-ins were refused for an account the server had "
        f"just reported created. A 201 that a client cannot then act on is the "
        f"application claiming a write it had not made."
    )


def test_the_commit_finishes_before_the_response_head_leaves(
    live_server: _LiveServer,
) -> None:
    """The ordering itself, not just its consequence.

    Read-after-write can pass by luck on a fast machine. This asserts the
    sequence the server actually executed, so a regression is caught even when
    the timing happens to hide it.
    """
    live_server.recorder.reset()
    email = _fresh_email("order")
    created, _ = live_server.post(
        "/api/v1/auth/register", {"email": email, "password": PASSWORD}
    )
    assert created == 201

    # The background email send runs after the response; give it a moment so
    # the recorded sequence is complete rather than truncated.
    time.sleep(COMMIT_DELAY_SECONDS + 0.4)
    events = list(live_server.recorder.events)

    def first(name: str) -> int:
        for index, event in enumerate(events):
            if event.startswith(name):
                return index
        raise AssertionError(f"{name} never happened: {events}")

    started = first("COMMIT_STARTED")
    finished = first("COMMIT_FINISHED")
    response_start = first("HTTP_RESPONSE_START")

    assert started < finished < response_start, (
        "required ordering violated — the response head left before the commit "
        f"finished. Recorded: {events}"
    )


def test_a_failed_write_does_not_answer_with_a_success(
    live_server: _LiveServer,
) -> None:
    """A refused write must be refused in the answer, not just in the database.

    Registering the same address twice is the cheapest real conflict this
    application has: the second attempt mutates, fails its uniqueness check and
    must roll back. What matters here is that the client is told so.
    """
    email = _fresh_email("dup")
    first_status, _ = live_server.post(
        "/api/v1/auth/register", {"email": email, "password": PASSWORD}
    )
    assert first_status == 201

    second_status, body = live_server.post(
        "/api/v1/auth/register", {"email": email, "password": PASSWORD}
    )
    assert second_status == 409, f"expected a conflict, got {second_status}"
    # A stable application error, not a database exception leaking outward.
    assert "detail" in body
    rendered = json.dumps(body).lower()
    for leak in ("traceback", "sqlalchemy", "asyncpg", "psycopg", "unique constraint"):
        assert leak not in rendered, f"the error body leaks {leak}: {body}"


def test_the_verification_email_is_sent_only_after_its_token_is_durable(
    live_server: _LiveServer,
) -> None:
    """B5 §6: a background side effect must not describe an uncommitted write.

    The verification link used to leave before the row backing its token
    existed. A customer quick enough to click it was told their own link was
    invalid, and if the commit had failed the message would have been advertising
    an account that never existed.
    """
    live_server.recorder.reset()
    email = _fresh_email("mail")
    created, _ = live_server.post(
        "/api/v1/auth/register", {"email": email, "password": PASSWORD}
    )
    assert created == 201
    time.sleep(COMMIT_DELAY_SECONDS + 0.6)

    events = list(live_server.recorder.events)
    assert "COMMIT_FINISHED" in events, f"nothing committed: {events}"
    # The send happens in a BackgroundTask, which Starlette runs after the
    # response — so committing before the response is what puts it in order.
    assert events.index("COMMIT_FINISHED") < events.index("HTTP_RESPONSE_START"), (
        f"the commit did not precede the response: {events}"
    )


# --------------------------------------------------------------------------
# the structural guard (B5 §21)
# --------------------------------------------------------------------------
def _every_api_route() -> list[object]:
    """Every APIRoute the application serves.

    FastAPI 0.141 keeps an included router as a nested `_IncludedRouter` rather
    than flattening it into `app.routes`, so this recurses. Written against the
    structure rather than a count: a new router must be covered automatically,
    which is the whole point of a guard.
    """
    from app.main import create_app

    app = create_app()

    def walk(router: object):  # noqa: ANN202
        for route in getattr(router, "routes", []):
            yield route
            nested = getattr(route, "original_router", None)
            if nested is not None:
                yield from walk(nested)

    return [r for r in walk(app) if hasattr(r, "dependant")]


def test_every_route_takes_its_database_session_function_scoped() -> None:
    """The architecture property, pinned so it cannot rot back.

    A request-scoped `yield` dependency is torn down AFTER the response is
    sent — that is FastAPI's documented behaviour, and it is what made every
    write in this application able to report a success it had not made. The
    session dependencies are therefore exported only as `Annotated` aliases
    carrying `scope="function"`.

    This fails if anyone writes `Depends(db_authed)` in a route signature
    again. It checks the resolved dependency graph rather than the source text,
    so it cannot be satisfied by a differently-spelled import.
    """
    from app.api import deps

    session_dependencies = {
        deps.db_authed,
        deps.db_authed_unverified_ok,
        deps.db_authed_legal_exempt,
        deps.db_authed_lifecycle_exempt,
        deps.db_anon,
        deps.db_admin,
    }

    offenders: list[str] = []
    checked = 0
    for route in _every_api_route():
        pending = list(route.dependant.dependencies)
        while pending:
            sub = pending.pop()
            if sub.call in session_dependencies:
                checked += 1
                if sub.scope != "function":
                    offenders.append(
                        f"{getattr(route, 'path', '?')} takes "
                        f"{sub.call.__name__} with scope={sub.scope!r}"
                    )
            pending.extend(sub.dependencies)

    assert checked > 0, "the guard found no session dependencies — the walk is broken"
    assert not offenders, (
        "these routes would commit after their response had been sent:\n  "
        + "\n  ".join(offenders)
    )


def test_no_route_module_reaches_past_the_aliases() -> None:
    """The same rule at the source level, where the mistake is actually typed.

    The resolved-graph test above is the authority. This one exists because its
    failure message points at the line somebody wrote, which is what a person
    needs at three in the afternoon.
    """
    import pathlib

    api = pathlib.Path(__file__).resolve().parents[2] / "app" / "api"
    offenders: list[str] = []
    for path in sorted(api.rglob("*.py")):
        if path.name == "deps.py":
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if "Depends(db_" in line:
                offenders.append(f"{path.relative_to(api.parent.parent)}:{number}: {line.strip()}")

    assert not offenders, (
        "take the session through an alias from app.api.deps (AuthedSession, "
        "AnonSession, AdminSession, …) — a bare Depends(db_…) is request-scoped "
        "and commits after the response:\n  " + "\n  ".join(offenders)
    )


def test_streaming_responses_would_need_an_explicit_policy() -> None:
    """B5 §5, recorded rather than assumed.

    Function-scoped dependencies close before the response is sent, which is
    exactly wrong for a response that streams its body afterwards — the session
    would be gone mid-stream. This application has no streaming response, so
    the question does not arise; this fails the day one is added, so it is
    answered deliberately then rather than discovered in production.
    """
    import pathlib
    import re

    api = pathlib.Path(__file__).resolve().parents[2] / "app" / "api"
    streaming: list[str] = []
    for path in sorted(api.rglob("*.py")):
        text = path.read_text()
        for marker in ("StreamingResponse", "FileResponse", "EventSourceResponse"):
            # Construction or import, not prose — this file's own explanation of
            # WHY streaming matters must not read as an instance of it.
            if re.search(rf"^\s*(from|import)\s.*\b{marker}\b", text, re.M) or f"{marker}(" in text:
                streaming.append(f"{path.name}: {marker}")

    assert not streaming, (
        "a streaming response arrived alongside function-scoped sessions. Decide "
        "the policy before shipping it: either the route commits before the "
        "stream starts and takes no session dependency, or it opts out "
        "explicitly. Found: " + ", ".join(streaming)
    )


# --------------------------------------------------------------------------
# commit failure (B5 §7) and rollback (B5 §8)
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def refusing_server() -> object:
    """A server whose commits fail once armed."""
    with _LiveServer(fail_commit_on="always") as server:
        yield server


def test_a_commit_that_fails_never_answers_with_a_success(
    refusing_server: _LiveServer,
) -> None:
    """B5 §7 and §20 together.

    The handler does its work, produces a nominal 201 body, and then the commit
    is refused. The customer must not be told "created". Discarding a success
    body that has already been built is the whole reason the commit has to
    happen before the response rather than after it — after it, there is
    nothing left to discard.
    """
    email = _fresh_email("commitfail")
    refusing_server.recorder.reset()
    refusing_server.recorder.arm_failure = True
    try:
        status, body = refusing_server.post(
            "/api/v1/auth/register", {"email": email, "password": PASSWORD}
        )
    finally:
        refusing_server.recorder.arm_failure = False

    assert status >= 500, (
        f"a refused commit answered {status}. A client that reads this as success "
        f"has been told about an account that does not exist."
    )
    rendered = json.dumps(body).lower()
    for word in ("created", "registered", "accepted"):
        assert word not in rendered, f"the failure body claims {word!r}: {body}"
    # And no database exception detail reaches the customer.
    for leak in ("traceback", "sqlalchemy", "asyncpg", "commitrefused"):
        assert leak not in rendered, f"the error body leaks {leak}: {body}"


def test_nothing_is_persisted_when_the_commit_is_refused(
    refusing_server: _LiveServer,
) -> None:
    """The other half: the refusal must be true of the database too.

    Proved through the application's own front door rather than by reading the
    table, because what matters is that the account genuinely is not there —
    the address must still be registrable afterwards.
    """
    email = _fresh_email("norows")
    refusing_server.recorder.arm_failure = True
    try:
        refusing_server.post(
            "/api/v1/auth/register", {"email": email, "password": PASSWORD}
        )
    finally:
        refusing_server.recorder.arm_failure = False

    # If the refused attempt had persisted anything, this would be a 409.
    status, _ = refusing_server.post(
        "/api/v1/auth/register", {"email": email, "password": PASSWORD}
    )
    assert status == 201, (
        f"re-registering the address answered {status}; the refused commit left "
        f"state behind."
    )
