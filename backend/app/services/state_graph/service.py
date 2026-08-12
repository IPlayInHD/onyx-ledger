"""The one entry point: load, then assemble.

There is no persistence step and no cache. Assembly is deterministic — the same
underlying state produces the same `graph_hash` — so a caller that wants to know
whether anything changed compares hashes rather than trusting a stored copy.

ADMISSION IS NOT HANDLED HERE, DELIBERATELY. An account past its deletion cutoff
is refused by `db_authed` → `assert_may_act` → `AccountDeletionInProgress`,
which every protected route inherits. Re-checking here would suggest the graph
has its own policy; it does not, and the only exemption in the repository
belongs to the deletion endpoints themselves.
"""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from .assembler import assemble_graph
from .contracts import TaxStateGraph
from .loader import GraphLoader


class TaxStateGraphService:
    def __init__(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        self.s = session
        self.user_id = user_id

    async def build(self, *, tax_year: int) -> TaxStateGraph:
        sources = await GraphLoader(self.s, self.user_id).load(tax_year=tax_year)
        return assemble_graph(sources, user_id=self.user_id, tax_year=tax_year)
