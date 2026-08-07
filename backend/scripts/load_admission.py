"""Controlled local load simulation for admission control (Entry 10 §49).

Measures the two numbers that decide whether the mechanism works — overshoot and
duplicate expensive jobs — plus what it costs to ask.

This is a LOCAL measurement on one machine against one PostgreSQL. It says
nothing about production capacity, and the report says so: what transfers is the
overshoot count (which must be zero) and the rough per-admission cost, not the
throughput.

    PGHOST=... PGPORT=... PGSUPER=... python scripts/load_admission.py
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

from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.database.session import engine, unit_of_work  # noqa: E402
from app.services.admission.policy import (  # noqa: E402
    POLICIES,
    OperationClass,
    ScopeType,
)
from app.services.admission.service import (  # noqa: E402
    AdmissionRejected,
    AdmissionService,
    _lock_key,
)

_settings = get_settings()


async def _timed_admit(operation: OperationClass, scope: str) -> tuple[bool, float]:
    start = time.perf_counter()
    try:
        async with unit_of_work(actor_type="admin") as session:
            await AdmissionService(session).admit(operation, scope_id=scope)
        return True, (time.perf_counter() - start) * 1000
    except AdmissionRejected:
        return False, (time.perf_counter() - start) * 1000


async def _reset(operation: OperationClass) -> None:
    """Release every live lease for one operation.

    Each scenario measures ONE property, so it starts from a clean pool. Without
    this the second scenario inherits the first's exhausted GLOBAL capacity and
    reports zero admissions — correct behaviour, but it measures the previous
    scenario rather than its own.
    """
    from sqlalchemy import text

    async with unit_of_work(actor_type="admin") as session:
        await session.execute(text("""
            UPDATE admission.lease
               SET released_at = now(), release_reason = 'CANCELLED'
             WHERE operation_code = :op AND released_at IS NULL
        """), {"op": operation.value})


def _report(label: str, latencies: list[float]) -> None:
    ordered = sorted(latencies)
    p50 = statistics.median(ordered)
    p95 = ordered[int(len(ordered) * 0.95) - 1] if len(ordered) > 1 else ordered[0]
    print(f"  {label:34s} n={len(ordered):5d}  p50={p50:7.2f}ms  p95={p95:7.2f}ms")


async def scenario_concurrent_optimizations(n: int = 100) -> int:
    """100 simultaneous optimization admissions from ONE principal.

    The number that matters is overshoot: anything above the configured limit
    means the race is open.
    """
    await _reset(OperationClass.OPTIMIZATION_RUN)
    scope = str(uuid.uuid4())
    limit = POLICIES[OperationClass.OPTIMIZATION_RUN].max_active_per_user
    results = await asyncio.gather(
        *(_timed_admit(OperationClass.OPTIMIZATION_RUN, scope) for _ in range(n))
    )
    admitted = sum(1 for ok, _ in results if ok)
    overshoot = max(0, admitted - (limit or 0))

    print(f"\n{n} concurrent OPTIMIZATION_RUN admissions, one principal")
    print(f"  limit={limit}  admitted={admitted}  rejected={n - admitted}")
    print(f"  OVERSHOOT = {overshoot}")
    _report("admission latency", [ms for _, ms in results])
    return overshoot


async def scenario_rapid_scenarios(n: int = 500) -> int:
    """500 rapid scenario admissions spread over many principals."""
    await _reset(OperationClass.SCENARIO_RUN)
    limit = POLICIES[OperationClass.SCENARIO_RUN].max_active_per_user or 0
    scopes = [str(uuid.uuid4()) for _ in range(20)]
    results = await asyncio.gather(
        *(_timed_admit(OperationClass.SCENARIO_RUN, scopes[i % len(scopes)])
          for i in range(n))
    )
    admitted = sum(1 for ok, _ in results if ok)
    ceiling = min(limit * len(scopes),
                  POLICIES[OperationClass.SCENARIO_RUN].max_active_global or n)
    overshoot = max(0, admitted - ceiling)

    print(f"\n{n} rapid SCENARIO_RUN admissions across {len(scopes)} principals")
    print(f"  per-principal limit={limit}  ceiling={ceiling}  admitted={admitted}")
    print(f"  OVERSHOOT = {overshoot}")
    _report("admission latency", [ms for _, ms in results])
    return overshoot


async def scenario_retry_storm(n: int = 50) -> int:
    """One logical request, retried 50 times. Duplicates must be zero."""
    await _reset(OperationClass.OPTIMIZATION_RUN)
    scope = str(uuid.uuid4())
    key = f"opt:{uuid.uuid4()}"

    async def attempt() -> bool:
        try:
            async with unit_of_work(actor_type="admin") as session:
                ticket = await AdmissionService(session).admit(
                    OperationClass.OPTIMIZATION_RUN, scope_id=scope, dedupe_key=key)
            return ticket.holds_lease
        except AdmissionRejected:
            return False

    results = await asyncio.gather(*(attempt() for _ in range(n)))
    holders = sum(1 for held in results if held)
    duplicates = max(0, holders - 1)

    print(f"\n{n} identical retries of ONE logical optimization")
    print(f"  expensive jobs started={holders}")
    print(f"  DUPLICATE EXPENSIVE JOBS = {duplicates}")
    return duplicates


async def scenario_mixed_principals(n: int = 200) -> int:
    """Mixed workload: one noisy principal against many quiet ones.

    Proves the anti-monopoly property — a noisy principal cannot take more than
    its own cap, so the quiet ones still get admitted.
    """
    await _reset(OperationClass.SCENARIO_RUN)
    noisy = str(uuid.uuid4())
    quiet = [str(uuid.uuid4()) for _ in range(10)]
    limit = POLICIES[OperationClass.SCENARIO_RUN].max_active_per_user or 0

    tasks = [_timed_admit(OperationClass.SCENARIO_RUN, noisy) for _ in range(n)]
    tasks += [_timed_admit(OperationClass.SCENARIO_RUN, q) for q in quiet]
    await asyncio.gather(*tasks)

    async with unit_of_work(actor_type="admin") as session:
        service = AdmissionService(session)
        noisy_active = await service.active_count(
            OperationClass.SCENARIO_RUN, scope_id=noisy)
        quiet_admitted = 0
        for q in quiet:
            quiet_admitted += await service.active_count(
                OperationClass.SCENARIO_RUN, scope_id=q)

    print(f"\nmixed workload: 1 noisy principal ({n} attempts) vs {len(quiet)} quiet")
    print(f"  noisy active={noisy_active} (cap {limit})")
    print(f"  quiet principals admitted={quiet_admitted}/{len(quiet)}")
    overshoot = max(0, noisy_active - limit)
    print(f"  OVERSHOOT = {overshoot}")
    return overshoot


async def scenario_overhead(n: int = 200) -> None:
    """What it costs to ask. CHEAP_READ tracks no concurrency, so this is the
    rate check alone: a single conditional UPSERT."""
    scope = str(uuid.uuid4())
    latencies = []
    for _ in range(n):
        _, ms = await _timed_admit(OperationClass.CHEAP_READ, scope)
        latencies.append(ms)
    print(f"\nadmission overhead, {n} sequential CHEAP_READ checks")
    print("  (1 SQL statement: the conditional rate UPSERT)")
    _report("rate-check only", latencies)


async def _max_backends_during(coro):
    """Run `coro` while sampling how many backends this application holds.

    The number the blocking-lock question actually turns on. Waiting on an
    advisory lock is not free: the waiter is holding a pooled connection while
    it waits, so a long queue on one lock converts into connection pressure —
    and if the pool runs out, callers fail with a pool timeout, which admission
    reports as a store failure and (for every expensive class) refuses.
    """
    from sqlalchemy import text as sql

    peak = 0
    stop = asyncio.Event()

    async def sample() -> None:
        nonlocal peak
        # A DEDICATED connection outside the application pool, so the probe
        # cannot itself be starved by the thing it is measuring.
        probe = create_async_engine(_settings.database_url, poolclass=NullPool)
        try:
            while not stop.is_set():
                async with probe.connect() as conn:
                    n = await conn.scalar(sql(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "  AND pid <> pg_backend_pid()"))
                peak = max(peak, int(n or 0))
                await asyncio.sleep(0.01)
        finally:
            await probe.dispose()

    sampler = asyncio.create_task(sample())
    try:
        result = await coro
    finally:
        stop.set()
        await sampler
    return result, peak


async def _blocking_contender(scope: str) -> tuple[str, float]:
    """One contender through the PRODUCTION path: pg_advisory_xact_lock."""
    start = time.perf_counter()
    try:
        async with unit_of_work(actor_type="admin") as session:
            service = AdmissionService(session)
            lease = await service._acquire_lease(
                POLICIES[OperationClass.OPTIMIZATION_RUN],
                ScopeType.USER, scope, dedupe_key=None,
                now=datetime.now(tz=UTC),
            )
        outcome = "admitted" if lease is not None else "rejected"
    except Exception as exc:  # noqa: BLE001 — a pool timeout is a RESULT here
        outcome = f"error:{type(exc).__name__}"
    return outcome, (time.perf_counter() - start) * 1000


async def _try_contender(scope: str) -> tuple[str, float]:
    """The alternative: pg_try_advisory_xact_lock, no waiting.

    Written out here rather than behind a flag in the service, because it is a
    candidate being measured, not a mode being shipped. Note what it costs: a
    contender that fails to TAKE the lock has learned nothing about whether it
    is under its limit, so the only safe answer is to refuse. Those refusals are
    SPURIOUS — the caller may have had quota to spare — and counting them is the
    entire point of the comparison.
    """
    from sqlalchemy import text as sql

    policy = POLICIES[OperationClass.OPTIMIZATION_RUN]
    start = time.perf_counter()
    try:
        async with unit_of_work(actor_type="admin") as session:
            got = await session.scalar(
                sql("SELECT pg_try_advisory_xact_lock(:k)"),
                {"k": _lock_key(ScopeType.USER, scope, policy.operation)},
            )
            if not got:
                return "lock-miss", (time.perf_counter() - start) * 1000
            active = await session.scalar(sql("""
                SELECT count(*) FROM admission.lease
                 WHERE scope_type = 'USER' AND scope_id = :s
                   AND operation_code = :op
                   AND released_at IS NULL AND expires_at > now()
            """), {"s": scope, "op": policy.operation.value})
            if (active or 0) >= (policy.max_active_per_user or 0):
                return "rejected", (time.perf_counter() - start) * 1000
            await session.execute(sql("""
                INSERT INTO admission.lease
                    (id, scope_type, scope_id, operation_code, acquired_at, expires_at)
                VALUES (:id, 'USER', :s, :op, now(), now() + interval '900 seconds')
            """), {"id": uuid.uuid4(), "s": scope, "op": policy.operation.value})
        return "admitted", (time.perf_counter() - start) * 1000
    except Exception as exc:  # noqa: BLE001
        return f"error:{type(exc).__name__}", (time.perf_counter() - start) * 1000


async def scenario_lock_contention(n: int) -> int:
    """§7 — blocking vs try-lock, at n contenders on ONE scope.

    Everything here contends on a single (scope, operation) advisory lock, which
    is the worst case the design can produce: real traffic spreads across
    principals and never queues like this.
    """
    print(f"\nlock contention: {n} contenders on ONE scope")
    limit = POLICIES[OperationClass.OPTIMIZATION_RUN].max_active_per_user or 0

    outcomes: dict[str, int] = {}
    for mode, run_one in (("blocking (production)", _blocking_contender),
                          ("try-lock (alternative)", _try_contender)):
        await _reset(OperationClass.OPTIMIZATION_RUN)
        scope = str(uuid.uuid4())

        async def run_all(one=run_one, target=scope):
            return await asyncio.gather(*(one(target) for _ in range(n)))

        wall = time.perf_counter()
        results, peak_backends = await _max_backends_during(run_all())
        wall = (time.perf_counter() - wall) * 1000

        counts: dict[str, int] = {}
        for outcome, _ in results:
            counts[outcome] = counts.get(outcome, 0) + 1
        latencies = sorted(ms for _, ms in results)
        admitted = counts.get("admitted", 0)
        overshoot = max(0, admitted - limit)

        print(f"  {mode}")
        print(f"    admitted={admitted} (cap {limit})  OVERSHOOT={overshoot}")
        print(f"    outcomes={counts}")
        print(f"    p50={statistics.median(latencies):7.2f}ms  "
              f"p95={latencies[int(len(latencies) * 0.95) - 1]:7.2f}ms  "
              f"max={latencies[-1]:7.2f}ms")
        print(f"    wall={wall:.0f}ms  peak backends={peak_backends} "
              f"(pool {_settings.db_pool_size}+{_settings.db_max_overflow})")
        outcomes[mode] = overshoot
        if mode.startswith("try") and counts.get("lock-miss"):
            print(f"    NOTE: {counts['lock-miss']} spurious refusals — callers "
                  f"that may have had quota, refused because another caller "
                  f"held the lock.")

    return sum(outcomes.values())


async def main() -> int:
    print("=" * 70)
    print("ADMISSION CONTROL LOAD SIMULATION — local measurement only")
    print("Throughput here is NOT production capacity. Overshoot and duplicate")
    print("counts are what transfer.")
    print("=" * 70)

    failures = 0
    failures += await scenario_concurrent_optimizations()
    failures += await scenario_rapid_scenarios()
    failures += await scenario_retry_storm()
    failures += await scenario_mixed_principals()
    failures += await scenario_lock_contention(100)
    failures += await scenario_lock_contention(500)
    await scenario_overhead()

    print("\n" + "=" * 70)
    if failures:
        print(f"FAIL: total overshoot + duplicates = {failures} (must be 0)")
    else:
        print("PASS: limit overshoot = 0, duplicate expensive jobs = 0")
    print("=" * 70)

    await engine.dispose()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
