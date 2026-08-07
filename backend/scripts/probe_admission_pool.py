"""Does an admission flood on ONE scope starve unrelated database work?

Entry 10 conformance check. The earlier load simulation answered a narrower
question — does the limit overshoot — and reported "peak backends = 30" next to
a claim that pool exhaustion was far away. Those are two different quantities
and the sentence conflated them: 30 IS the application pool ceiling
(`db_pool_size` 10 + `db_max_overflow` 20), so the pool was not near its limit,
it was AT it. Whether that starved anything was never measured.

It matters because of how the blocking lock interacts with the pool. A
contender waiting on `pg_advisory_xact_lock` is holding a pooled connection
while it waits. Enough contenders on one hot scope and every connection in the
pool is parked on that one lock — at which point an unrelated request, from a
different user doing something completely different, cannot get a connection at
all. The concurrency limit would be perfectly enforced while the application
stopped answering.

So this measures the pool, not the limiter:

  * how long a caller waits to CHECK OUT a connection, separately from how long
    it then waits for the lock;
  * how deep the checkout queue gets, and whether any checkout times out;
  * and, continuously through the flood, the latency of two unrelated probes —
    a trivial query, and a real admission on a DIFFERENT scope.

The unrelated probes are the actual conformance question. Zero overshoot with a
starved application is not a pass.

    PGHOST=... PGPORT=... ONYX_DATABASE_URL=... python scripts/probe_admission_pool.py
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime

sys.path.insert(0, os.getcwd())

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import TimeoutError as PoolTimeout  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.database.session import engine, unit_of_work  # noqa: E402
from app.services.admission.policy import POLICIES, OperationClass, ScopeType  # noqa: E402
from app.services.admission.service import (  # noqa: E402
    AdmissionRejected,
    AdmissionService,
    _lock_key,
)

SETTINGS = get_settings()
OPERATION = OperationClass.OPTIMIZATION_RUN
POLICY = POLICIES[OPERATION]

#: The API's own request deadline. A caller that waits longer than this has been
#: failed regardless of what the limiter eventually decided.
API_TIMEOUT_MS = 30_000


def _pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def _line(label: str, values: list[float], unit: str = "ms") -> str:
    if not values:
        return f"    {label:32s} (none)"
    return (
        f"    {label:32s} n={len(values):5d}  "
        f"p50={statistics.median(values):8.2f}  p95={_pct(values, 0.95):8.2f}  "
        f"p99={_pct(values, 0.99):8.2f}  max={max(values):8.2f} {unit}"
    )


class Flood:
    """One contention run, with everything instrumented.

    `production=True` goes through `AdmissionService.admit`, which is what an
    endpoint actually calls: the RATE check runs first, and only a caller still
    inside its per-minute allowance ever reaches the advisory lock. That
    distinction turns out to be the whole answer, so it is measured rather than
    argued.
    """

    def __init__(self, n: int, *, mode: str = "lock") -> None:
        self.n = n
        self.mode = mode
        self.scope = str(uuid.uuid4())
        self.checkout_ms: list[float] = []
        self.lock_ms: list[float] = []
        self.total_ms: list[float] = []
        self.outcomes: dict[str, int] = {}
        self.pool_timeouts = 0
        self.in_flight = 0
        self.peak_checkedout = 0
        self.peak_overflow = 0
        self.peak_waiters = 0
        self.peak_backends = 0

    # -- one contender, timed in three parts --------------------------------
    async def contender(self) -> None:
        if self.mode == "control":
            await self._control_contender()
            return
        if self.mode == "production":
            await self._production_contender()
            return
        await self._lock_contender()

    async def _control_contender(self) -> None:
        """No admission at all — one trivial query through the same pool.

        THE control for the whole question. If 1000 concurrent `SELECT 1`s
        degrade unrelated traffic just as much, then the pool is simply finite
        and admission is not monopolising anything; the degradation belongs to
        concurrency, not to the advisory lock. Without this line the measurement
        cannot tell "admission starves the pool" from "1000 requests need more
        than 30 connections".
        """
        self.in_flight += 1
        started = time.perf_counter()
        try:
            async with unit_of_work(actor_type="admin") as session:
                checked_out = time.perf_counter()
                self.checkout_ms.append((checked_out - started) * 1000)
                await session.execute(text("SELECT 1"))
                self.lock_ms.append((time.perf_counter() - checked_out) * 1000)
            outcome = "ok"
        except PoolTimeout:
            self.pool_timeouts += 1
            outcome = "POOL_TIMEOUT"
        except Exception as exc:  # noqa: BLE001
            outcome = f"error:{type(exc).__name__}"
        finally:
            self.in_flight -= 1
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1
        self.total_ms.append((time.perf_counter() - started) * 1000)

    async def _production_contender(self) -> None:
        """The real endpoint path: rate check, then (maybe) the lock."""
        self.in_flight += 1
        started = time.perf_counter()
        try:
            async with unit_of_work(actor_type="admin") as session:
                checked_out = time.perf_counter()
                self.checkout_ms.append((checked_out - started) * 1000)
                decision = await AdmissionService(session).evaluate(
                    OPERATION, scope_id=self.scope, scope_type=ScopeType.USER)
                self.lock_ms.append((time.perf_counter() - checked_out) * 1000)
            # Raised AFTER the commit, exactly as `admission_guard` does — which
            # is what makes the rate-counter increment durable for a REFUSED
            # attempt. Calling `admit()` here instead would measure the old
            # defect rather than the shipped path.
            ticket = decision.raise_if_rejected()
            outcome = "admitted" if ticket.holds_lease else "admitted-no-lease"
        except AdmissionRejected as exc:
            outcome = f"rejected:{exc.reason.value}"
        except PoolTimeout:
            self.pool_timeouts += 1
            outcome = "POOL_TIMEOUT"
        except Exception as exc:  # noqa: BLE001
            outcome = f"error:{type(exc).__name__}"
        finally:
            self.in_flight -= 1
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1
        self.total_ms.append((time.perf_counter() - started) * 1000)

    async def _lock_contender(self) -> None:
        self.in_flight += 1
        started = time.perf_counter()
        try:
            async with unit_of_work(actor_type="admin") as session:
                # Everything before this point is queueing for a connection.
                checked_out = time.perf_counter()
                self.checkout_ms.append((checked_out - started) * 1000)

                # Take the advisory lock explicitly so its wait is separable.
                # `_acquire_lease` takes the SAME key immediately afterwards;
                # advisory locks are re-entrant within a transaction, so the
                # second acquisition is free and the production path is
                # unchanged — the lock is held from exactly the same instant it
                # would have been.
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:k)"),
                    {"k": _lock_key(ScopeType.USER, self.scope, OPERATION)},
                )
                locked = time.perf_counter()
                self.lock_ms.append((locked - checked_out) * 1000)

                lease = await AdmissionService(session)._acquire_lease(
                    POLICY, ScopeType.USER, self.scope,
                    dedupe_key=None, now=datetime.now(tz=UTC),
                )
            outcome = "admitted" if lease is not None else "rejected"
        except PoolTimeout:
            self.pool_timeouts += 1
            outcome = "POOL_TIMEOUT"
        except Exception as exc:  # noqa: BLE001 — an error here is a RESULT
            outcome = f"error:{type(exc).__name__}"
        finally:
            self.in_flight -= 1
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1
        self.total_ms.append((time.perf_counter() - started) * 1000)

    # -- samplers -----------------------------------------------------------
    async def sample_pool(self, stop: asyncio.Event) -> None:
        """The application pool's own view. Authoritative for 'is it full'."""
        pool = engine.sync_engine.pool
        while not stop.is_set():
            checked_out = pool.checkedout()
            self.peak_checkedout = max(self.peak_checkedout, checked_out)
            self.peak_overflow = max(self.peak_overflow, pool.overflow())
            # QueuePool exposes no waiter count, so it is derived: tasks that
            # have entered and do not hold a connection are queueing for one.
            self.peak_waiters = max(self.peak_waiters, max(0, self.in_flight - checked_out))
            await asyncio.sleep(0.005)

    async def sample_backends(self, stop: asyncio.Event) -> None:
        """The SERVER's view, on a connection outside the pool being measured."""
        probe = create_async_engine(SETTINGS.database_url, poolclass=NullPool)
        try:
            while not stop.is_set():
                async with probe.connect() as conn:
                    n = await conn.scalar(text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "  AND pid <> pg_backend_pid()"))
                self.peak_backends = max(self.peak_backends, int(n or 0))
                await asyncio.sleep(0.02)
        finally:
            await probe.dispose()


class UnrelatedProbe:
    """A different user, doing something else, through the SAME pool.

    THE conformance question. If this cannot get a connection while one scope is
    under attack, the admission path has converted a per-scope limit into a
    platform-wide outage.
    """

    def __init__(self, label: str, admission: bool) -> None:
        self.label = label
        self.admission = admission
        self.latencies: list[float] = []
        self.failures: dict[str, int] = {}

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            started = time.perf_counter()
            try:
                if self.admission:
                    # A real admission for an UNRELATED principal: its own
                    # scope, so its own advisory-lock key. It shares only the
                    # connection pool with the flood.
                    async with unit_of_work(actor_type="admin") as session:
                        await AdmissionService(session).admit(
                            OperationClass.SCENARIO_RUN, scope_id=str(uuid.uuid4()))
                else:
                    async with unit_of_work(actor_type="admin") as session:
                        await session.execute(text("SELECT 1"))
                self.latencies.append((time.perf_counter() - started) * 1000)
            except AdmissionRejected:
                self.latencies.append((time.perf_counter() - started) * 1000)
            except Exception as exc:  # noqa: BLE001
                name = type(exc).__name__
                self.failures[name] = self.failures.get(name, 0) + 1
            await asyncio.sleep(0.01)


async def _reset() -> None:
    async with unit_of_work(actor_type="admin") as session:
        await session.execute(text("""
            UPDATE admission.lease
               SET released_at = now(), release_reason = 'CANCELLED'
             WHERE released_at IS NULL
        """))


async def run(n: int, *, mode: str = "lock") -> bool:
    await _reset()
    production = mode == "production"
    flood = Flood(n, mode=mode)
    stop = asyncio.Event()
    probes = [UnrelatedProbe("unrelated SELECT 1", admission=False),
              UnrelatedProbe("unrelated admission", admission=True)]

    # Baseline for the probes with no flood running, so "degraded" has a
    # reference point rather than being asserted against a guess.
    baseline = UnrelatedProbe("baseline", admission=False)
    baseline_stop = asyncio.Event()
    baseline_task = asyncio.create_task(baseline.run(baseline_stop))
    await asyncio.sleep(0.5)
    baseline_stop.set()
    await baseline_task

    watchers = [
        asyncio.create_task(flood.sample_pool(stop)),
        asyncio.create_task(flood.sample_backends(stop)),
        *(asyncio.create_task(p.run(stop)) for p in probes),
    ]

    wall = time.perf_counter()
    await asyncio.gather(*(flood.contender() for _ in range(n)))
    wall = (time.perf_counter() - wall) * 1000

    stop.set()
    await asyncio.gather(*watchers)

    limit = POLICY.max_active_per_user or 0
    admitted = flood.outcomes.get("admitted", 0)
    if production:
        # A rate-limited caller never reached the lock, so it holds no slot.
        admitted = sum(v for k, v in flood.outcomes.items() if k == "admitted")
    overshoot = max(0, admitted - limit)
    ceiling = SETTINGS.db_pool_size + SETTINGS.db_max_overflow

    path = {
        "production": "PRODUCTION path (evaluate: rate check, then lock)",
        "control": "CONTROL — no admission, one SELECT 1 (pool baseline)",
    }.get(mode, "LOCK path only (_acquire_lease, rate check bypassed)")
    print(f"\n{'=' * 78}\n{n} contenders on ONE admission scope — {path}\n{'=' * 78}")
    print(f"  admitted={admitted} (cap {limit})   OVERSHOOT={overshoot}")
    print(f"  outcomes={flood.outcomes}")
    print(f"  pool checkout timeouts = {flood.pool_timeouts}")
    print(f"  wall={wall:.0f}ms")
    print("  latency breakdown")
    print(_line("total per request", flood.total_ms))
    print(_line("  · pool checkout wait", flood.checkout_ms))
    print(_line("  · admission work + lock" if production else "  · advisory lock wait",
                flood.lock_ms))
    print("  pool occupancy")
    print(f"    peak checked out               {flood.peak_checkedout} / {ceiling}"
          f"  (pool_size {SETTINGS.db_pool_size} + overflow {SETTINGS.db_max_overflow})")
    print(f"    peak overflow in use           {flood.peak_overflow} / {SETTINGS.db_max_overflow}")
    print(f"    peak tasks queued for checkout {flood.peak_waiters}")
    print(f"    peak PostgreSQL backends       {flood.peak_backends} / 97 usable"
          f"  (max_connections 100 - 3 superuser-reserved)")
    print("  unrelated traffic DURING the flood")
    print(_line(f"  baseline ({baseline.label}, no flood)", baseline.latencies))
    for probe in probes:
        print(_line(f"  {probe.label}", probe.latencies))
        if probe.failures:
            print(f"      FAILURES: {probe.failures}")

    ok = True
    if mode == "control":
        overshoot = 0
    if overshoot:
        print("  FAIL  concurrency overshoot")
        ok = False
    if flood.pool_timeouts:
        print("  FAIL  a caller could not obtain a connection")
        ok = False
    for probe in probes:
        if probe.failures:
            print(f"  FAIL  unrelated traffic errored: {probe.label}")
            ok = False
        elif not probe.latencies:
            print(f"  FAIL  unrelated traffic never completed: {probe.label}")
            ok = False
        elif max(probe.latencies) > API_TIMEOUT_MS:
            print(f"  FAIL  unrelated traffic exceeded the API deadline: {probe.label}")
            ok = False
    if flood.total_ms and max(flood.total_ms) > API_TIMEOUT_MS:
        print("  FAIL  a contender exceeded the API deadline")
        ok = False
    return ok


async def leak_check() -> bool:
    """Every connection the flood used must have gone back.

    A pool that never returns to idle after the load stops is a leak, and it
    would show up as a slow strangulation rather than a failure.
    """
    await asyncio.sleep(0.3)
    pool = engine.sync_engine.pool
    checked_out = pool.checkedout()
    print(f"\nconnection leak check: {checked_out} still checked out after the runs")
    if checked_out:
        print("  FAIL  admission leaked connections")
        return False
    print("  ok    every connection returned to the pool")
    return True


async def main() -> int:
    print("=" * 78)
    print("ADMISSION POOL-CONTENTION CONFORMANCE CHECK")
    print("Local measurement. The question is not overshoot — it is whether one")
    print("hot admission scope can starve unrelated legitimate database work.")
    print("=" * 78)
    pool = engine.sync_engine.pool
    print(f"application pool ceiling : {SETTINGS.db_pool_size} + "
          f"{SETTINGS.db_max_overflow} = "
          f"{SETTINGS.db_pool_size + SETTINGS.db_max_overflow} connections")
    print(f"pool_timeout             : {getattr(pool, '_timeout', 'n/a')}s")
    print(f"pool_recycle             : {getattr(pool, '_recycle', 'n/a')}")
    print("PostgreSQL ceiling       : 97 usable (max_connections 100 - 3 reserved)")
    print("These are DIFFERENT ceilings. Reaching the first is not reaching the second.")

    ok = True
    for n in (100, 500, 1000):
        ok = await run(n) and ok

    # And the same flood through the path an endpoint actually takes. The rate
    # check is one conditional UPSERT and it runs FIRST, so almost nobody
    # reaches the advisory lock at all — which is the difference between "the
    # lock is a bottleneck" and "1000 concurrent requests need connections".
    ok = await run(1000, mode="production") and ok

    # And the control: the same 1000 concurrent tasks doing NO admission work
    # at all. Whatever degradation survives here belongs to the pool size, not
    # to the limiter.
    ok = await run(1000, mode="control") and ok

    ok = await leak_check() and ok

    await _reset()
    print("\n" + "=" * 78)
    print("PASS: bounded contention, unrelated work serviceable" if ok
          else "FAIL: see the marked lines above")
    print("=" * 78)
    await engine.dispose()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
