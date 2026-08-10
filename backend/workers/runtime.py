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
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


def run_task(body: Callable[[], Awaitable[T]]) -> T:
    """Run one async task body and release every engine before the loop closes.

    `body` is a zero-argument callable returning a coroutine, so the coroutine
    is created INSIDE the new loop rather than before it exists.
    """

    async def _run_and_release() -> T:
        try:
            return await body()
        finally:
            # Imported here: this module is imported by task modules at import
            # time, and the database package pulls in settings and engines.
            from app.database.privacy_session import dispose_all_engines

            await dispose_all_engines()

    return asyncio.run(_run_and_release())


__all__ = ["run_task"]
