"""Entry 11B5J — the contract of `workers.runtime.run_task` itself.

`test_privacy_task_reentrancy.py` certifies the OUTCOME: every 11B5 worker can
be called repeatedly in one process. This file certifies the MECHANISM those
tasks now depend on, because a helper that six production entry points route
through is a single point of failure and deserves to be pinned directly.

Five things have to hold, and only the first is obvious:

  1. a body that succeeds returns its value to the caller unchanged;
  2. a body that RAISES still gets its engines disposed — otherwise a failing
     task poisons the pool for the next invocation, which is the original
     defect with an extra step;
  3. the exception the body raised is the exception the caller sees — same
     object, not wrapped, not replaced by whatever disposal might raise on the
     way out. A `finally` that swallowed the error would turn a purge failure
     into a silent success, and `acks_late` would then ack the message;
  4. when DISPOSAL ITSELF fails, the task does not report success, and if the
     body failed too the body's failure stays primary while the cleanup
     failure is still recorded;
  5. nothing recorded about a cleanup failure carries its message. A disposal
     error comes from the driver, and the driver knows the connection string.

Disposal is proved twice over: once by observing the real engine's pool object
being replaced (`Engine.dispose()` swaps the pool), and once by a spy, so the
test does not rest on mocking alone.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from workers.runtime import run_task


class _TaskBodyExploded(RuntimeError):
    """Distinctive type — a generic RuntimeError could come from the loop."""


@pytest.fixture(autouse=True)
def _force_release_engines():
    """Undo what the cleanup-failure tests deliberately break.

    Several tests below stop disposal from running, which is exactly the state
    the whole entry is about: the application engine keeps a connection bound
    to a loop that `asyncio.run` has since closed, and the NEXT test to touch
    it fails inside `asyncpg` rather than in its own assertions. That happened
    on the first run of this file and is the sharpest evidence there is that a
    failed disposal leaves a poisoned process — but it belongs in an assertion,
    not in an unrelated test's traceback.

    Reaches for `engine.dispose` and `dispose_worker_engines` directly rather
    than `dispose_all_engines`, because that name is the one the tests replace.
    """
    yield

    import asyncio
    import contextlib

    from app.database import privacy_session
    from app.database.session import engine

    async def _release() -> None:
        # `close=False`: the pool may hold connections belonging to a loop that
        # has already closed, and asking to close those gracefully is what
        # raises "attached to a different loop" — the very state being cleaned
        # up. Dropping the pool without touching them is the documented way out.
        with contextlib.suppress(Exception):
            await engine.dispose(close=False)
        for name in list(privacy_session._engines):
            worker_engine = privacy_session._engines.pop(name)
            privacy_session._factories.pop(name, None)
            with contextlib.suppress(Exception):
                await worker_engine.dispose(close=False)

    asyncio.run(_release())


def _app_pool_id() -> int:
    """`AsyncEngine.dispose()` REPLACES the pool object, so a changed id here is
    disposal that actually happened rather than a call that was recorded."""
    from app.database.session import engine

    return id(engine.pool)


def _worker_runtimes() -> set[str]:
    """Which privileged engines are live. `dispose_worker_engines()` clears the
    registry, so an empty set after a run is disposal of those engines."""
    from app.database import privacy_session

    return set(privacy_session._engines)


async def _touch_every_engine() -> str:
    """Do real work on the application AND both privileged pools, so disposal
    has something to dispose. A body that never opened a connection would let a
    broken helper pass."""
    from app.database.privacy_session import (
        freshness_unit_of_work,
        privacy_unit_of_work,
    )
    from app.database.session import SessionLocal

    async with SessionLocal() as session:
        await session.execute(text("SELECT 1"))
    async with privacy_unit_of_work() as session:
        await session.execute(text("SELECT 1"))
    async with freshness_unit_of_work() as session:
        await session.execute(text("SELECT 1"))
    return "body-result"


def test_a_successful_body_returns_its_value_and_still_releases_the_engines():
    before = _app_pool_id()
    result = run_task(_touch_every_engine)

    assert result == "body-result", (
        f"run_task must hand back what the body returned, got {result!r}")
    assert _app_pool_id() != before, (
        "the application engine kept its pool across run_task — a pooled "
        "connection bound to the now-closed loop survives into the next "
        "invocation")
    assert _worker_runtimes() == set(), (
        f"privileged engines survived the run: {sorted(_worker_runtimes())}")


def test_a_body_that_raises_still_releases_the_engines():
    """The failure path is the one that matters. A task that raises is exactly
    the task Celery will retry, i.e. call again in this same process."""

    async def _work_then_fail() -> str:
        await _touch_every_engine()
        raise _TaskBodyExploded("the task body failed after using the database")

    before = _app_pool_id()
    with pytest.raises(_TaskBodyExploded):
        run_task(_work_then_fail)

    assert _app_pool_id() != before, (
        "a FAILING body left the application engine undisposed, so the retry "
        "inherits a connection attached to a dead loop")
    assert _worker_runtimes() == set(), (
        "a FAILING body left privileged engines live: "
        f"{sorted(_worker_runtimes())}")


def test_the_body_s_own_exception_reaches_the_caller_unwrapped():
    """Identity, not just type: `raise ... from` or a re-raise of a new object
    would lose the traceback the operator needs, and swallowing it entirely
    would let Celery ack a purge that never happened."""
    original = _TaskBodyExploded("original failure")

    async def _fail() -> str:
        raise original

    with pytest.raises(_TaskBodyExploded) as caught:
        run_task(_fail)

    assert caught.value is original, (
        f"run_task replaced the body's exception: {caught.value!r} is not the "
        f"object the body raised ({original!r})")
    assert caught.value.__cause__ is None and caught.value.__context__ is None, (
        "the exception was re-raised from inside another handler, so disposal "
        "is now part of the traceback the operator reads first")


def test_disposal_runs_on_both_paths_and_before_the_loop_closes(monkeypatch):
    """The spy view of the same two facts, plus the ordering that makes them
    work: disposal must happen INSIDE the loop `asyncio.run` is about to close,
    because `AsyncEngine.dispose()` is itself a coroutine."""
    import asyncio

    from app.database import privacy_session

    calls: list[str] = []
    real = privacy_session.dispose_all_engines

    async def _spy() -> None:
        calls.append(asyncio.get_running_loop().__class__.__name__)
        await real()

    monkeypatch.setattr(privacy_session, "dispose_all_engines", _spy)

    assert run_task(_touch_every_engine) == "body-result"
    assert len(calls) == 1, f"success path disposed {len(calls)} times, expected 1"

    async def _fail() -> str:
        await _touch_every_engine()
        raise _TaskBodyExploded("boom")

    with pytest.raises(_TaskBodyExploded):
        run_task(_fail)
    assert len(calls) == 2, (
        f"failure path did not dispose — total disposals {len(calls)}, expected 2")


# ---------------------------------------------------------------------------
# When the cleanup itself fails
# ---------------------------------------------------------------------------
# Disposal closes sockets, so it can fail on its own — and then the helper has
# to choose which failure the caller is told about. Measured behaviour of the
# plain `try/finally` this started as:
#
#   body succeeded, disposal raised    disposal error propagates    (correct)
#   body raised, disposal raised       DISPOSAL error propagates,
#                                      the body's error demoted to
#                                      `__context__`               (wrong)
#
# The second row is the one these tests pin. A caller writing
# `except SourceDataPurge...` stops matching when an unrelated socket close
# loses a race, and Celery records the wrong reason for the retry.


class _DisposalExploded(RuntimeError):
    """Synthetic, and deliberately carrying no DSN or credential material —
    the point of the message is that it must NOT reach a log."""


def _break_disposal(monkeypatch) -> None:
    from app.database import privacy_session

    async def _fail() -> None:
        raise _DisposalExploded(
            "synthetic disposal failure with no connection string in it")

    monkeypatch.setattr(privacy_session, "dispose_all_engines", _fail)


def test_a_task_whose_cleanup_fails_does_not_report_success(monkeypatch):
    """A successful body plus a failed disposal is NOT a successful task.

    Returning normally here would ack the message and leave the next
    invocation in this process holding whatever the failed disposal left
    behind — the original defect, reached by a different route.
    """
    _break_disposal(monkeypatch)

    with pytest.raises(_DisposalExploded):
        run_task(_touch_every_engine)


def test_the_body_s_failure_stays_primary_when_disposal_also_fails(monkeypatch):
    """Which of the two failures is the task's outcome.

    The body's exception answers "did the work happen"; the disposal exception
    says the pool is untidy. The first is the one a caller catches on and the
    one Celery records, so it stays primary — and the second is still carried,
    because a disposal that fails every run is its own defect.
    """
    original = _TaskBodyExploded("ORIGINAL_ERROR: the purge did not finish")

    async def _fail() -> str:
        raise original

    _break_disposal(monkeypatch)

    with pytest.raises(_TaskBodyExploded) as caught:
        run_task(_fail)

    assert caught.value is original, (
        f"a failed disposal replaced the task's own failure: {caught.value!r}")
    notes = getattr(caught.value, "__notes__", [])
    assert any("_DisposalExploded" in note for note in notes), (
        f"the cleanup failure vanished without evidence; notes = {notes}")


def test_a_failed_disposal_is_not_reported_as_a_clean_runtime(monkeypatch):
    """`cleanup attempted` is not `cleanup completed`, and the difference is
    observable rather than rhetorical.

    `dispose_worker_engines` stops at the first engine that raises and never
    reaches `_engines.clear()`, so after a failed disposal engines really are
    still registered and their pooled connections really are still open. The
    first run of this file proved it the hard way: the test that followed one
    of these was handed the dead-loop connection and failed inside `asyncpg`.

    So both halves are checked — what the process looks like afterwards, and
    that the note says only what the process can back up.
    """
    original = _TaskBodyExploded("ORIGINAL_ERROR")
    live_before: set[str] = set()

    async def _use_the_engines_then_fail() -> str:
        await _touch_every_engine()
        live_before.update(_worker_runtimes())
        raise original

    _break_disposal(monkeypatch)
    app_pool = _app_pool_id()

    with pytest.raises(_TaskBodyExploded) as caught:
        run_task(_use_the_engines_then_fail)

    assert live_before, "the body did not open any privileged engine"
    assert _worker_runtimes() == live_before, (
        "the registry was emptied even though disposal failed — the note below "
        f"would then be understating: {sorted(_worker_runtimes())}")
    assert _app_pool_id() == app_pool, (
        "the application pool was replaced even though disposal failed")

    note = " ".join(getattr(caught.value, "__notes__", []))
    assert "ATTEMPTED, not completed" in note, (
        f"the note claims more than the runtime can guarantee: {note!r}")
    for claim in ("successfully", "cleanup completed", "engines released",
                  "runtime clean"):
        assert claim not in note.lower(), (
            f"the note asserts a clean runtime the process cannot back up "
            f"({claim!r}): {note!r}")


def test_the_cleanup_failure_is_recorded_without_its_message(monkeypatch):
    """Entry 11A, applied to the one exception most likely to hold a DSN.

    A disposal failure comes from the driver, and asyncpg's messages carry the
    host, database and role it was connecting as. Only the class name and a
    closed reason code may be recorded — no message, and no `exc_info`, which
    would render the whole traceback through structlog's formatter.
    """
    import workers.runtime as runtime

    events: list[tuple[str, dict[str, object]]] = []

    class _Recorder:
        def error(self, event: str, **fields: object) -> None:
            events.append((event, fields))

        def __getattr__(self, _name: str):        # info/warning, unused here
            return lambda *a, **k: None

    monkeypatch.setattr(runtime, "log", _Recorder())
    _break_disposal(monkeypatch)

    with pytest.raises(_DisposalExploded):
        run_task(_touch_every_engine)

    assert len(events) == 1, f"expected one record, got {events}"
    event, fields = events[0]
    assert fields["reason"] == "ENGINE_DISPOSE_FAILED", fields
    assert fields["error_type"] == "_DisposalExploded", fields
    assert "exc_info" not in fields, "the whole traceback would be rendered"

    rendered = " ".join([event, *(f"{k}={v}" for k, v in fields.items())])
    assert "synthetic disposal failure" not in rendered, (
        f"the exception MESSAGE was recorded: {rendered!r}")
    assert "postgresql" not in rendered.lower() and "@" not in rendered, (
        f"connection-string material reached the log: {rendered!r}")


# ---------------------------------------------------------------------------
# The regression the failure path exists to prevent
# ---------------------------------------------------------------------------
def test_a_failed_invocation_does_not_poison_the_ones_after_it():
    """Invocation 1 fails after touching every pool; invocations 2 and 3 are
    real production tasks — ALL IN ONE PROCESS, which is the only shape in
    which a leaked pool is visible.

    Before `run_task`, the disposal in the deletion worker sat on the success
    path only, so this sequence left invocation 2 holding a connection bound to
    the loop invocation 1 closed.
    """
    from workers.tasks.ioe import relay_freshness_outbox, sweep_scenario_freshness
    from workers.tasks.privacy import run_account_deletion_phases

    async def _fail_after_using_the_database() -> str:
        await _touch_every_engine()
        raise _TaskBodyExploded("forced failure on invocation 1")

    with pytest.raises(_TaskBodyExploded):
        run_task(_fail_after_using_the_database)

    failures: list[str] = []
    for attempt, call in (
        (2, run_account_deletion_phases.run),
        (3, relay_freshness_outbox.run),
        (4, sweep_scenario_freshness.run),
    ):
        try:
            call()
        except Exception as exc:                           # noqa: BLE001
            failures.append(f"invocation {attempt}: {type(exc).__name__}: {exc}")

    assert not failures, (
        "a failed invocation left the process unable to run the next task:\n  "
        + "\n  ".join(f[:200] for f in failures))


# ---------------------------------------------------------------------------
# The structural rule, added after the lean-launch entry found it violated
# ---------------------------------------------------------------------------
def test_the_worker_tree_contains_exactly_one_asyncio_run() -> None:
    """`run_task` owns the event loop. Nothing else in `workers/` may open one.

    WHY THIS EXISTS. Everything above certifies that `run_task` disposes the
    engines. Nothing certified that the tasks USE it — and four entry points
    did not. `workers/tasks/maintenance.py`, `analysis.py`, `ioe.py` and the
    shared TKMS bridge each called `asyncio.run` for themselves, so their
    pooled connections outlived the loop that opened them and every second
    invocation in one worker process failed inside `asyncpg`:

        bare asyncio.run          ok, RuntimeError, ok, RuntimeError, ok
        workers.runtime.run_task  ok, ok, ok, ok, ok

    (Measured five calls each, in separate processes. Separate processes
    matter: run both modes in one, and the bare mode poisons the pool that the
    `run_task` mode then inherits, which makes the helper look broken.)

    The behavioural guard in `test_privacy_task_reentrancy.py` is an
    enumeration, and an enumeration is what let this survive — that list said
    "every task Entry 11B5 depends on", and these four were not in that entry.
    So this one is structural instead: it does not care which tasks exist, it
    cares that the loop is opened in exactly one place. A task added next year
    is covered without anybody remembering to add it.

    AST rather than grep, so the prose in this very docstring — which says
    `asyncio.run` five times — is not mistaken for a call.
    """
    import ast
    from pathlib import Path

    workers_root = Path(__file__).resolve().parents[2] / "workers"
    assert workers_root.is_dir(), f"no worker tree at {workers_root}"

    offenders: list[str] = []
    for source_file in sorted(workers_root.rglob("*.py")):
        tree = ast.parse(source_file.read_text(), filename=str(source_file))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = (
                f"{target.value.id}.{target.attr}"
                if isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                else getattr(target, "id", "")
            )
            if name == "asyncio.run":
                offenders.append(
                    f"{source_file.relative_to(workers_root.parent)}:{node.lineno}"
                )

    # EXACTLY ONE, and it is the helper's. Asserting "at most one" would pass
    # on a tree where `run_task` had stopped opening a loop at all, and this
    # guard would then be watching nothing.
    assert len(offenders) == 1 and offenders[0].startswith("workers/runtime.py:"), (
        "asyncio.run must appear exactly once in the worker tree — inside "
        "`workers.runtime.run_task`, which disposes every engine before the "
        "loop closes. Found: "
        + (", ".join(offenders) or "no call at all, so run_task no longer owns "
           "the loop and this guard is watching nothing")
    )


def test_every_celery_task_reaches_its_async_body_through_run_task() -> None:
    """The other half: a task module that never opens a loop must still use ours.

    The structural rule above would also be satisfied by a task module that
    simply stopped running its async body at all. This requires the positive:
    every module under `workers/tasks/` that defines an `async def` inside a
    task imports `run_task`, so the body has something to be handed to.
    """
    import ast
    from pathlib import Path

    tasks_root = Path(__file__).resolve().parents[2] / "workers" / "tasks"
    missing: list[str] = []

    for source_file in sorted(tasks_root.glob("*.py")):
        text = source_file.read_text()
        tree = ast.parse(text, filename=str(source_file))
        has_async_body = any(
            isinstance(node, ast.AsyncFunctionDef) for node in ast.walk(tree)
        )
        if not has_async_body:
            continue
        imports_run_task = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "workers.runtime"
            and any(alias.name == "run_task" for alias in node.names)
            for node in ast.walk(tree)
        )
        if not imports_run_task:
            missing.append(source_file.name)

    assert not missing, (
        "these task modules define async bodies but never import `run_task`, "
        "so nothing disposes their engines between invocations: "
        + ", ".join(missing)
    )
