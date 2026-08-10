"""Running an async task body from a long-lived Celery worker process.

THE PROBLEM THIS EXISTS FOR. A Celery worker calls a task many times in one
process. `asyncio.run` creates a fresh event loop per invocation and closes it
on the way out — but the engines are module-level and their connection pools
are not. A connection opened under one loop goes back into the pool, the loop
dies, and the next invocation is handed a connection bound to a dead loop:

    RuntimeError: Task ... got Future ... attached to a different loop
    RuntimeError: Event loop is closed

Measured on this repository before the fix, calling each task three times in
one process:

    privacy.run_account_deletion_phases   call 2 failed
    ioe.relay_freshness_outbox            call 2 failed
    ioe.sweep_scenario_freshness          call 2 failed
    ioe.verify_sealed_integrity           calls 1 and 3 failed

The pattern is not "every second call" in general — it depends on which pooled
connection is handed out — which is worse than a clean alternation, because it
looks like flakiness rather than a defect.

WHY A SHARED HELPER RATHER THAN A `finally` IN EACH TASK. The same defect has
now been found in four separate entry points, each written at a different time
by someone who had no reason to think about event loops. Putting the cleanup in
the one place that owns the `asyncio.run` call means a task added next year
inherits it by using the normal way of running a task body.

WHAT IT DOES NOT DO. It changes no identity and no privilege: the privileged
runtimes keep their own DSNs and their own PostgreSQL principals, and PD-16
stays closed. This is engine lifecycle, not authorization.

THE COST is reconnecting on the next invocation instead of reusing a pooled
connection. For periodic background tasks — the fastest is scheduled per
minute — that is a connection setup per run, against the alternative of a task
that fails outright.

WHEN CLEANUP ITSELF FAILS. Disposal closes sockets, so it can fail on its own.
Measured behaviour of the plain `try/finally` this started as:

    body succeeded, disposal raised    disposal error propagates    (correct)
    body raised, disposal raised       DISPOSAL error propagates,
                                       the body's error demoted to
                                       `__context__`               (wrong)

The second row is why this module now handles the failure explicitly. The
body's exception is the one that says whether the purge happened; the disposal
exception says the pool is untidy. A caller writing `except SourceDataPurge...`
stops matching when an unrelated socket close loses a race, and Celery records
the wrong reason for the retry. So the body's exception stays PRIMARY and the
cleanup failure is recorded beside it — never dropped, never promoted.

Recorded as a closed reason code and an exception CLASS NAME only. No
`str(exception)`, no `exc_info`: a disposal error comes from the driver and its
text can carry the host, database and role from the connection string. Entry
11A's rule about exception text applies with extra force here.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.core.logging import get_logger

log = get_logger("onyx.worker.runtime")

T = TypeVar("T")

#: Attached to the body's exception when disposal also failed. Says ATTEMPTED,
#: not completed: `dispose_worker_engines` stops at the first engine that
#: raises and does not clear the registry, so engines can still be live and
#: their pooled connections still open. Claiming a clean runtime here would be
#: the kind of status that lies in the direction that matters.
_CLEANUP_NOTE = (
    "engine disposal also failed ({error_type}, reason=ENGINE_DISPOSE_FAILED): "
    "cleanup was ATTEMPTED, not completed — engines may still be registered "
    "and pooled connections still open"
)


def run_task(body: Callable[[], Awaitable[T]]) -> T:
    """Run one async task body and release every engine before the loop closes.

    `body` is a zero-argument callable returning a coroutine, so the coroutine
    is created INSIDE the new loop rather than before it exists.
    """

    async def _run_and_release() -> T:
        failed: BaseException | None = None
        try:
            return await body()
        except BaseException as exc:
            # `BaseException` deliberately: a cancelled task must keep
            # `CancelledError` as its outcome too, rather than being reported
            # as a disposal problem.
            failed = exc
            raise
        finally:
            try:
                # Imported here: this module is imported by task modules at
                # import time, and the database package pulls in settings and
                # engines.
                from app.database.privacy_session import dispose_all_engines

                await dispose_all_engines()
            except BaseException as cleanup_error:
                # The class name and a closed code. Never the message, never
                # `exc_info` — a driver error carries the connection string.
                log.error("worker_engine_dispose_failed",
                          operation="run_task",
                          outcome="failed",
                          reason="ENGINE_DISPOSE_FAILED",
                          error_type=type(cleanup_error).__name__,
                          body_failed=failed is not None)
                if failed is None:
                    # Nothing else went wrong, so this IS the outcome. The task
                    # must not report success while the pool may be poisoned.
                    raise
                # The body's failure is the answer to "did the work happen".
                # Keep it primary and carry the cleanup failure alongside.
                failed.add_note(
                    _CLEANUP_NOTE.format(error_type=type(cleanup_error).__name__))

    return asyncio.run(_run_and_release())


__all__ = ["run_task"]
