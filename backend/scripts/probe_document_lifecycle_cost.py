"""What did Entry 11B4 cost the document paths? (§48)

Three changes are priceable, and they are not all in the same direction:

  UPLOAD (PD-2)
      The key is derived from the document's own id, and the id comes from a
      server default — so the row has to reach the database before the key can
      be computed. Written the obvious way that is INSERT, then UPDATE the key.
      An extra round trip on every upload forever, to fix a defect that is
      about what the key CONTAINS, not about when it is generated. Measured
      both ways: server-assigned id (INSERT + UPDATE) against a client-assigned
      `uuid7()` (INSERT alone, same value shape, same column).

  DELETE (PD-8)
      A path that did not exist before, so there is no "before" to subtract.
      What matters instead is how it SCALES: extraction cleanup is two
      set-based statements, and the question is whether a document with 40
      extracted fields costs materially more than one with none. If it does,
      the deletion is doing per-row work and the synchronous route in §36 is
      wrong.

  PROCESS (§25 race fix)
      One `pg_advisory_xact_lock` before the read. Uncontended it is a single
      round trip that never waits; contended it is exactly the serialization
      that was the point. The uncontended case is the one paid by everybody.

    PGHOST=... PGPORT=... python scripts/probe_document_lifecycle_cost.py
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
from sqlalchemy import event, select  # noqa: E402

from app.database.models import DocumentType  # noqa: E402
from app.database.session import engine, unit_of_work  # noqa: E402
from app.main import app  # noqa: E402
from app.services.document_processing import service as docservice  # noqa: E402

PASSWORD = "supersecret1"
ROUNDS = 40


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


_next_address = iter(f"127.31.{a}.{b}" for a in range(1, 250) for b in range(1, 250))


async def _register() -> tuple[dict[str, str], uuid.UUID]:
    """Register from a fresh source address — registration is throttled per IP,
    and a pool built through one client starts returning 429 partway."""
    address = next(_next_address)
    transport = httpx.ASGITransport(app=app, client=(address, 40000))
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as client:
        email = f"doccost_{uuid.uuid4().hex[:10]}@example.com"
        await client.post("/api/v1/auth/register",
                          json={"email": email, "password": PASSWORD})
        login = await client.post("/api/v1/auth/login",
                                  json={"email": email, "password": PASSWORD})
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        me = await client.get("/api/v1/users/me", headers=headers)
        return headers, uuid.UUID(me.json()["id"])


def _percentiles(samples: list[float]) -> tuple[float, float]:
    ordered = sorted(samples)
    return (statistics.median(ordered) * 1000,
            ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] * 1000)


async def _doc_type_code() -> str:
    """T4 specifically: extraction is slip-aware, and a type outside `SLIP_MAP`
    yields zero fields no matter how many are supplied — which would make the
    scaling measurement below silently measure nothing."""
    async with unit_of_work(actor_type="system") as session:
        row = await session.scalar(select(DocumentType).where(DocumentType.code == "T4"))
        if row is None:
            raise SystemExit("document type T4 not seeded; load reference data first")
        return row.code


# ---------------------------------------------------------------- upload ----
async def _upload_direct(user_id: uuid.UUID, code: str, *, server_id: bool) -> float:
    """One create_upload, timed.

    `server_id` runs the shape 11B4 first wrote — let the column's default
    supply the id, flush to learn it, compute the key, flush again — against
    what the service does now, which assigns `uuid7()` up front and inserts
    once. Same column, same value shape (the helper matches
    `ref.uuid_generate_v7()` byte for byte), so this is two round trips against
    one and nothing else.
    """
    started = time.perf_counter()
    async with unit_of_work(actor_type="user", user_id=user_id) as session:
        svc = docservice.DocumentService(session)
        if server_id:
            await _create_upload_server_id(svc, user_id, code)
        else:
            await svc.create_upload(user_id, code, "probe.pdf", "application/pdf", 2025)
    return time.perf_counter() - started


async def _create_upload_server_id(svc, user_id: uuid.UUID, code: str) -> None:
    """The INSERT-then-UPDATE shape, kept here so the comparison stays runnable
    without the service having to carry a branch it does not want."""
    from app.database.models import Document

    dtype = await svc.s.scalar(select(DocumentType).where(DocumentType.code == code))
    doc = Document(
        user_id=user_id, document_type_id=dtype.id, tax_year=2025,
        bucket=svc.settings.s3_bucket_documents, object_key="",
        mime_type="application/pdf", status="uploaded",
    )
    svc.s.add(doc)
    await svc.s.flush()
    doc.object_key = docservice._opaque_object_key(user_id, doc.id)
    await svc.s.flush()


# ---------------------------------------------------------------- delete ----
# Seeding and deletion both go through the SERVICE rather than the route.
# Admission is per-user rate limited, and forty uploads from one account
# measures the cost of being refused rather than the cost of the work — a
# lesson Entry 11B1's probe already paid for. Admission's own overhead was
# priced in Entry 10; what is unpriced here is the database work.
async def _seed_document(user_id: uuid.UUID, code: str, field_count: int) -> uuid.UUID:
    async with unit_of_work(actor_type="user", user_id=user_id) as session:
        svc = docservice.DocumentService(session)
        doc, _ = await svc.create_upload(
            user_id, code, "probe.pdf", "application/pdf", 2025)
        doc_id = doc.id
    if field_count:
        fields = ({"employmentIncome": "1000.00"} if field_count == 1
                  else {f"field{i}": f"{i}.00" for i in range(field_count)})
        async with unit_of_work(actor_type="user", user_id=user_id) as session:
            await docservice.DocumentService(session).process(doc_id, fields=fields)
    return doc_id


async def _delete(user_id: uuid.UUID, doc_id: uuid.UUID) -> float:
    started = time.perf_counter()
    async with unit_of_work(actor_type="user", user_id=user_id) as session:
        await docservice.DocumentService(session).delete_document(user_id, doc_id)
    return time.perf_counter() - started


async def main() -> None:
    counter = StatementCounter()
    counter.install()
    code = await _doc_type_code()

    print(f"Entry 11B4 document lifecycle cost, n={ROUNDS} per row\n")

    # ---- upload: server-assigned id vs client-assigned ---------------------
    _, warm_user = await _register()
    for _ in range(3):
        await _upload_direct(warm_user, code, server_id=False)

    upload: dict[str, tuple[int, float, float]] = {}
    for label, server_id in (("INSERT + UPDATE (first cut)", True),
                             ("INSERT only (current)", False)):
        counter.start()
        await _upload_direct(warm_user, code, server_id=server_id)
        statements = counter.stop()
        samples = [await _upload_direct(warm_user, code, server_id=server_id)
                   for _ in range(ROUNDS)]
        p50, p95 = _percentiles(samples)
        upload[label] = (statements, p50, p95)

    print("create_upload (service, own unit of work)")
    print(f"{'':30} {'stmts':>6} {'p50':>9} {'p95':>9}")
    for label, (statements, p50, p95) in upload.items():
        print(f"  {label:28} {statements:>6} {p50:>8.2f}m {p95:>8.2f}m")
    old, new = upload["INSERT + UPDATE (first cut)"], upload["INSERT only (current)"]
    print(f"  the round trip removed: {old[0] - new[0]} statement(s), "
          f"{new[1] - old[1]:+.3f} ms p50, {new[2] - old[2]:+.3f} ms p95")

    # ---- delete: does it scale with extracted fields? ----------------------
    print("\ndelete_document (service, own unit of work) — scaling with fields")
    print(f"{'':30} {'stmts':>6} {'p50':>9} {'p95':>9}")
    for field_count in (0, 1, 40):
        _, owner = await _register()
        doc_ids = [await _seed_document(owner, code, field_count)
                   for _ in range(ROUNDS + 1)]

        counter.start()
        await _delete(owner, doc_ids[0])
        statements = counter.stop()

        samples = [await _delete(owner, d) for d in doc_ids[1:]]
        p50, p95 = _percentiles(samples)
        print(f"  {f'{field_count} extracted field(s)':28} {statements:>6} "
              f"{p50:>8.2f}m {p95:>8.2f}m")

    # ---- the advisory lock, uncontended ------------------------------------
    print("\nthe §25 advisory lock, uncontended")
    _, lock_user = await _register()
    samples = []
    for _ in range(ROUNDS):
        async with unit_of_work(actor_type="user", user_id=lock_user) as session:
            svc = docservice.DocumentService(session)
            started = time.perf_counter()
            await svc._lock_document(uuid.uuid4())
            samples.append(time.perf_counter() - started)
    p50, p95 = _percentiles(samples)
    print(f"  pg_advisory_xact_lock: 1 statement, p50 {p50:.3f} ms, p95 {p95:.3f} ms")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
