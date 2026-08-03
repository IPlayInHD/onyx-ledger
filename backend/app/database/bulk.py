"""Batched inserts for immutable evidence rows.

Every IOE result child is written once, never updated, and never read back
inside the transaction that writes it. Adding those rows through the ORM's unit
of work costs one `INSERT` round trip per row: at 75 candidates a single
optimization run issued 1,620 insert statements, and the count grew with the
candidate population rather than with the number of tables.

`bulk_insert` sends one statement per table instead. It is a Core insert, so:

  * it participates in the caller's transaction exactly like `session.add()` —
    the atomicity of TX-2 is unchanged;
  * RLS `WITH CHECK`, the append-only audit triggers and the immutability
    triggers all still fire, because they are server-side and per row;
  * it does NOT populate the ORM identity map, which is why callers that need a
    parent id supply it with `uuid7()` rather than reading it back.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

# PostgreSQL's Bind message carries an int16 parameter count, so one statement
# can hold at most 32,767 bound values. Chunking below that keeps a large run
# correct rather than failing at the protocol level; the margin absorbs anything
# the compiler adds.
MAX_BIND_PARAMS = 30_000


async def bulk_insert(
    session: AsyncSession, model: Any, rows: Sequence[dict]
) -> int:
    """Insert `rows` into `model`'s table in one statement. Returns the count.

    Uses the multi-VALUES form rather than `executemany`. They are not
    equivalent here: with `executemany`, SQLAlchemy drops a `None` value from the
    column list so the column default can fire, and when rows disagree about
    which columns are `None` it has to recompile and re-execute per group. Real
    evidence rows disagree constantly — an optional reason code set on some rows
    and not others — so `executemany` degraded to roughly one statement per row,
    which is the fan-out this function exists to remove. `values()` compiles one
    column list for the whole batch, and an explicit `None` stays `NULL`.

    Rows are normalized to a common key set first, since `values()` requires it;
    a key missing from one row becomes an explicit `NULL` rather than silently
    shifting that row onto a different column list.

    Empty input is a no-op: SQLAlchemy rejects an empty parameter list, and
    "this run produced no exclusions" is an ordinary outcome, not an error.
    """
    if not rows:
        return 0

    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    normalized = [{key: row.get(key) for key in keys} for row in rows]

    chunk_size = max(1, MAX_BIND_PARAMS // max(len(keys), 1))
    for start in range(0, len(normalized), chunk_size):
        await session.execute(
            insert(model).values(normalized[start:start + chunk_size])
        )
    return len(rows)


__all__ = ["MAX_BIND_PARAMS", "bulk_insert"]
