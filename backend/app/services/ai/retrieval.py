"""Knowledge indexing + retrieval over pgvector.

Indexes PUBLISHED tax-rule versions into ai.knowledge_embedding, and retrieves
the top-k chunks for a query filtered to the relevant tax year — so the AI only
ever grounds answers in the law that was in force for the user's year.
"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import KnowledgeEmbedding, TaxRule, TaxRuleVersion
from app.integrations.embeddings import get_embedder


class KnowledgeIndex:
    def __init__(self, session: AsyncSession):
        self.s = session
        self.embedder = get_embedder()

    async def reindex_published_rules(self, tax_year: int) -> int:
        """(Re)embed every published rule version for a year. Idempotent."""
        await self.s.execute(
            delete(KnowledgeEmbedding).where(
                KnowledgeEmbedding.source_type == "rule_version",
                KnowledgeEmbedding.tax_year == tax_year,
            )
        )
        rows = await self.s.execute(
            select(TaxRule.name, TaxRule.category, TaxRuleVersion.id,
                   TaxRuleVersion.description, TaxRuleVersion.ai_explanation,
                   TaxRuleVersion.source_url)
            .join(TaxRuleVersion, TaxRuleVersion.tax_rule_id == TaxRule.id)
            .where(TaxRuleVersion.tax_year == tax_year, TaxRuleVersion.status == "published")
        )
        count = 0
        for r in rows:
            content = f"{r.name} ({r.category}). {r.description or ''} {r.ai_explanation or ''}".strip()
            self.s.add(KnowledgeEmbedding(
                source_type="rule_version", source_id=r.id, tax_year=tax_year,
                content=content, embedding=self.embedder.embed(content),
            ))
            count += 1
        await self.s.flush()
        return count

    async def retrieve(self, query: str, tax_year: int, k: int = 4) -> list[dict]:
        """Top-k nearest rule chunks (cosine distance), year-filtered."""
        qvec = self.embedder.embed(query)
        stmt = (
            select(
                KnowledgeEmbedding.source_id,
                KnowledgeEmbedding.content,
                KnowledgeEmbedding.embedding.cosine_distance(qvec).label("distance"),
            )
            .where(KnowledgeEmbedding.tax_year == tax_year,
                   KnowledgeEmbedding.source_type == "rule_version")
            .order_by("distance")
            .limit(k)
        )
        return [
            {"rule_version_id": row.source_id, "content": row.content,
             "distance": float(row.distance)}
            for row in await self.s.execute(stmt)
        ]

    async def count(self, tax_year: int) -> int:
        n = await self.s.scalar(
            select(KnowledgeEmbedding.id).where(KnowledgeEmbedding.tax_year == tax_year).limit(1)
        )
        return 1 if n else 0
