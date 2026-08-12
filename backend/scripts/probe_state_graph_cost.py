"""What does assembling a Personal Tax State Graph actually cost? (Entry 12A)

The bounded-query test proves the STATEMENT COUNT does not grow with node count.
That is the shape guarantee, and it is the one that matters most, but it says
nothing about wall clock — a fixed number of queries can still be slow if one of
them scans. So this measures the real thing: load and assemble a graph for
tenants of increasing size, and report where the time goes.

METHOD
Load and assembly are timed SEPARATELY, because they fail differently. Loading
is I/O against a real database with RLS applied; assembly is pure CPU over
already-fetched rows. Reporting one number would hide which half moved.

Every measurement is a median over repeated rounds on the same data, so a single
slow round (autovacuum, a cold cache) does not become the headline.

    PGHOST=... PGPORT=... ONYX_DATABASE_URL=... python scripts/probe_state_graph_cost.py
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal

sys.path.insert(0, os.getcwd())

ROUNDS = 15
SIZES = (1, 25, 100, 400)


async def _seed(income_rows: int) -> uuid.UUID:
    from sqlalchemy import select

    from app.database.models import (
        AnalysisInputSnapshot,
        AnalysisRun,
        IncomeSource,
        IncomeType,
        TaxProfile,
        UserAccount,
    )
    from app.database.session import unit_of_work
    from tests.conftest import frozen_snapshot

    async with unit_of_work(actor_type="system") as s:
        account = UserAccount(
            email=f"probe_{uuid.uuid4().hex[:10]}@test.ca", status="active"
        )
        s.add(account)
        await s.flush()
        user_id = account.id
        income_type = await s.scalar(
            select(IncomeType).where(IncomeType.code == "employment")
        )
        income_type_id = income_type.id

    async with unit_of_work(user_id=user_id, actor_type="user") as s:
        s.add(TaxProfile(user_id=user_id, province_code="ON", marital_status="single"))
        for _ in range(income_rows):
            s.add(IncomeSource(
                user_id=user_id, tax_year=2025, income_type_id=income_type_id,
                amount=Decimal("95000") / income_rows, province_code="ON",
            ))
        run = AnalysisRun(
            user_id=user_id, tax_year=2025, province_code="ON",
            engine_version="py-1.0.0", status="completed",
            started_at=datetime.now(tz=UTC), completed_at=datetime.now(tz=UTC),
            data_verified=True,
        )
        s.add(run)
        await s.flush()
        payload, digest = frozen_snapshot(employment_income=Decimal("95000"))
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot=payload, snapshot_hash=digest,
        ))
    return user_id


async def _measure(user_id: uuid.UUID) -> tuple[float, float, int, int]:
    from app.database.session import unit_of_work
    from app.services.state_graph import GraphLoader, assemble_graph

    load_ms: list[float] = []
    assemble_ms: list[float] = []
    nodes = edges = 0

    for _ in range(ROUNDS):
        async with unit_of_work(user_id=user_id, actor_type="user") as s:
            started = time.perf_counter()
            sources = await GraphLoader(s, user_id).load(tax_year=2025)
            load_ms.append((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        graph = assemble_graph(sources, user_id=user_id, tax_year=2025)
        assemble_ms.append((time.perf_counter() - started) * 1000)
        nodes, edges = graph.summary.node_count, graph.summary.edge_count

    return statistics.median(load_ms), statistics.median(assemble_ms), nodes, edges


async def main() -> None:
    print(f"rounds per size: {ROUNDS}\n")
    print(f"{'facts':>7} {'nodes':>7} {'edges':>7} {'load ms':>10} "
          f"{'assemble ms':>13} {'total ms':>10} {'us/node':>9}")
    print("-" * 70)

    for size in SIZES:
        user_id = await _seed(size)
        load, assemble, nodes, edges = await _measure(user_id)
        per_node = ((load + assemble) * 1000 / nodes) if nodes else 0.0
        print(f"{size:>7} {nodes:>7} {edges:>7} {load:>10.2f} "
              f"{assemble:>13.2f} {load + assemble:>10.2f} {per_node:>9.1f}")

    from app.database.session import engine

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
