"""Entry 11B5J — the contract of `workers.runtime.run_task` itself.

`test_privacy_task_reentrancy.py` certifies the OUTCOME: every 11B5 worker can
be called repeatedly in one process. This file certifies the MECHANISM those
tasks now depend on, because a helper that six production entry points route
through is a single point of failure and deserves to be pinned directly.

Three things have to hold, and only the first is obvious:

  1. a body that succeeds returns its value to the caller unchanged;
  2. a body that RAISES still gets its engines disposed — otherwise a failing
     task poisons the pool for the next invocation, which is the original
     defect with an extra step;
  3. the exception the body raised is the exception the caller sees — same
     object, not wrapped, not replaced by whatever disposal might raise on the
     way out. A `finally` that swallowed the error would turn a purge failure
     into a silent success, and `acks_late` would then ack the message.

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
