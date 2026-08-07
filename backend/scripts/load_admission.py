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

sys.path.insert(0, os.getcwd())

from app.database.session import engine, unit_of_work  # noqa: E402
from app.services.admission.policy import POLICIES, OperationClass  # noqa: E402
from app.services.admission.service import (  # noqa: E402
    AdmissionRejected,
    AdmissionService,
)


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
