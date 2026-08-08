"""What does the deletion cutoff cost a request that is not being deleted?

Entry 11B1 §32–§33. Every authenticated request now consults the account
lifecycle, and almost every one of them is for an active account with no
lifecycle row at all. That is the case worth measuring: the cost is paid by the
99.9% who are not deleting, and it is paid on every request forever.

It is not one statement either. `assert_may_act` takes a shared advisory lock
and then reads, in two statements rather than one, because a combined statement
takes its snapshot before it waits for the lock and reads a stale world (see the
docstring there). That decision doubled the added round trips, so it should be
priced rather than assumed cheap.

WHAT IS MEASURED

  * statements per request, counted at the driver, with the cutoff on and off;
  * end-to-end latency of a read and a write, p50 and p95, on and off;
  * the worker preflight, in isolation, since it runs per task rather than
    per request.

"Off" is the real function replaced by a no-op — the same code path, minus the
check — so the difference is the check and not two different builds.

    PGHOST=... PGPORT=... ONYX_DATABASE_URL=... python scripts/probe_lifecycle_overhead.py
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
import uuid

sys.path.insert(0, os.getcwd())

import httpx  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.database.session import engine, unit_of_work  # noqa: E402
from app.main import app  # noqa: E402
from app.services.privacy.lifecycle import AccountLifecycleService  # noqa: E402
from app.services.privacy.preflight import refuse_if_deleting  # noqa: E402

PASSWORD = "supersecret1"
ROUNDS = 60


class StatementCounter:
    """Counts statements at the driver, which is where they actually cost."""

    def __init__(self) -> None:
        self.count = 0
        self._on = False

    def install(self) -> None:
        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def _count(conn, cursor, statement, parameters, context, executemany):
            if self._on:
                self.count += 1

    def start(self) -> None:
        self.count = 0
        self._on = True

    def stop(self) -> int:
        self._on = False
        return self.count


_next_address = iter(f"127.30.{a}.{b}" for a in range(1, 250) for b in range(1, 250))


async def _register(_unused: httpx.AsyncClient | None = None) -> tuple[str, uuid.UUID]:
    """Register and log in from a fresh source address.

    Registration and login are throttled per source IP, so a pool built through
    one client runs out of allowance partway and starts returning 429 — the
    first version of this probe crashed on the missing token rather than
    silently measuring something else, which is the better failure.
    """
    address = next(_next_address)
    transport = httpx.ASGITransport(app=app, client=(address, 40000))
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as client:
        email = f"overhead_{uuid.uuid4().hex[:10]}@example.com"
        await client.post("/api/v1/auth/register",
                          json={"email": email, "password": PASSWORD})
        login = await client.post("/api/v1/auth/login",
                                  json={"email": email, "password": PASSWORD})
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        me = await client.get("/api/v1/users/me", headers=headers)
        return token, uuid.UUID(me.json()["id"])


def _percentiles(samples: list[float]) -> tuple[float, float]:
    ordered = sorted(samples)
    p50 = statistics.median(ordered)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return p50 * 1000, p95 * 1000


async def _time(client, method: str, path: str, headers, body=None) -> float:
    started = time.perf_counter()
    if body is None:
        await getattr(client, method)(path, headers=headers)
    else:
        await getattr(client, method)(path, json=body, headers=headers)
    return time.perf_counter() - started


async def main() -> None:
    counter = StatementCounter()
    counter.install()

    transport = httpx.ASGITransport(app=app, client=("127.0.0.44", 40000))
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as client:
        token, user_id = await _register(client)
        headers = {"Authorization": f"Bearer {token}"}

        # A POOL OF ACCOUNTS FOR THE WRITES, one write each per condition.
        # Writes are rate limited per user, so hammering one account measures
        # the cost of being refused rather than the cost of writing — the first
        # version of this probe reported a 5 ms regression that was entirely
        # 429s. Reads are not limited, so one account is enough for those.
        pool = [await _register(client) for _ in range(ROUNDS)]
        pool_headers = [{"Authorization": f"Bearer {t}"} for t, _ in pool]

        read = ("get", "/api/v1/users/me", None)
        write = ("post", "/api/v1/financials/income",
                 {"tax_year": 2025, "income_type_code": "employment",
                  "amount": "1.00"})

        real = AccountLifecycleService.assert_may_act

        async def _noop(self, user_id):  # noqa: ANN001, ARG001
            return None

        results: dict[str, dict[str, object]] = {}
        for label, patched in (("with cutoff", None), ("without cutoff", _noop)):
            if patched is not None:
                AccountLifecycleService.assert_may_act = patched  # type: ignore[method-assign]
            else:
                AccountLifecycleService.assert_may_act = real  # type: ignore[method-assign]

            # Warm the pool and any first-call caching so the first sample is
            # not the one that pays for connection setup.
            for _ in range(5):
                await _time(client, *read[:2], headers)

            counter.start()
            await _time(client, *read[:2], headers)
            read_statements = counter.stop()

            counter.start()
            await _time(client, write[0], write[1], headers, write[2])
            write_statements = counter.stop()

            reads = [await _time(client, *read[:2], headers) for _ in range(ROUNDS)]
            writes = [await _time(client, write[0], write[1], h, write[2])
                      for h in pool_headers]

            results[label] = {
                "read_statements": read_statements,
                "write_statements": write_statements,
                "read": _percentiles(reads),
                "write": _percentiles(writes),
            }

        AccountLifecycleService.assert_may_act = real  # type: ignore[method-assign]

        print(f"authenticated request, n={ROUNDS} per row\n")
        print(f"{'':16} {'stmts/read':>11} {'stmts/write':>12} "
              f"{'read p50':>9} {'read p95':>9} {'write p50':>10} {'write p95':>10}")
        for label, row in results.items():
            rp50, rp95 = row["read"]      # type: ignore[misc]
            wp50, wp95 = row["write"]     # type: ignore[misc]
            print(f"{label:16} {row['read_statements']:>11} "
                  f"{row['write_statements']:>12} "
                  f"{rp50:>8.2f}m {rp95:>8.2f}m {wp50:>9.2f}m {wp95:>9.2f}m")

        on, off = results["with cutoff"], results["without cutoff"]
        print(f"\nadded statements: read "
              f"{int(on['read_statements']) - int(off['read_statements'])}, "
              f"write {int(on['write_statements']) - int(off['write_statements'])}")
        print(f"added read p50:  {on['read'][0] - off['read'][0]:+.3f} ms")   # type: ignore[index]
        print(f"added read p95:  {on['read'][1] - off['read'][1]:+.3f} ms")   # type: ignore[index]
        print(f"added write p50: {on['write'][0] - off['write'][0]:+.3f} ms")  # type: ignore[index]
        print(f"added write p95: {on['write'][1] - off['write'][1]:+.3f} ms")  # type: ignore[index]

        # ---- the worker preflight, on its own -----------------------------
        samples = []
        for _ in range(ROUNDS):
            started = time.perf_counter()
            async with unit_of_work(actor_type="system") as session:
                await refuse_if_deleting(session, user_id, task="probe")
            samples.append(time.perf_counter() - started)
        p50, p95 = _percentiles(samples)
        print(f"\nworker preflight (own unit of work), n={ROUNDS}: "
              f"p50 {p50:.2f} ms, p95 {p95:.2f} ms")

        counter.start()
        async with unit_of_work(actor_type="system") as session:
            await refuse_if_deleting(session, user_id, task="probe")
        print(f"worker preflight statements: {counter.stop()}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
