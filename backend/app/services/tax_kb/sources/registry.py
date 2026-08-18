"""Tax source registry — registration and provenance resolution.

TWO OPERATIONS, AND ONLY ONE OF THEM WRITES.

  `register`            records a source edition. Idempotent on (source,
                        content fingerprint). Never updates an existing
                        edition — a changed document becomes a NEW version.

  `provenance_for_*`    resolves the pinned provenance of a governed knowledge
                        object. Reads only, and reads only what was pinned.

WHAT IT WILL NOT DO. It publishes no tax rule, interprets no source, ranks no
legal authority, and fetches nothing. There is no network call in this module
and no code path that could add one: an official locator is RECORDED, never
retrieved.
"""
from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    Jurisdiction,
    KnowledgeCitation,
    SourceCitation,
    TaxSource,
    TaxSourceVersion,
)
from app.services.tax_kb.sources.domain import (
    ManifestError,
    SourceLocator,
    SourceManifest,
)


class ProvenanceUnavailable(RuntimeError):
    """Pinned provenance could not be resolved.

    Raised rather than resolved. This is the §21 guarantee made operational: a
    knowledge object whose pinned source version is missing is a governed
    integrity failure, NOT an invitation to substitute today's edition. Pointing
    at a newer source would answer "what does the law say now", when the
    question asked was "what was this rule authored against".
    """


@dataclass(frozen=True)
class ResolvedCitation:
    """One pinned citation, resolved to the exact edition it names."""

    citation_id: uuid.UUID
    locator: dict[str, str]
    label: str | None
    source_version_id: uuid.UUID
    edition: str
    content_fingerprint: str
    fingerprint_method: str
    official_locator: str
    version_status: str
    #: True when a LATER edition exists. Informational only: it never changes
    #: which version is returned. A superseded source is not a broken one — §22.
    superseded: bool
    tax_year: int | None
    publication_date: date | None
    effective_from: date | None
    effective_to: date | None
    retrieved_at: datetime
    source_id: uuid.UUID
    source_type: str
    issuer_code: str
    jurisdiction_code: str
    official_identifier: str
    title: str


class TaxSourceRegistry:
    """Internal tax-knowledge tooling. Not reachable from any customer route."""

    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def register(self, manifest: SourceManifest) -> TaxSourceVersion:
        """Register one source edition. Idempotent, fail-closed, append-only.

        IDENTITY IS CONTENT, NOT RETRIEVAL. Two imports of the same bytes for
        the same source are the same edition however the URL query string, the
        local file path or the retrieval timestamp differed — those describe how
        a document was fetched, not which document it is.

        A DIFFERENT FINGERPRINT IS A DIFFERENT EDITION. It never overwrites: the
        earlier row stays exactly as written, and the caller may name it as the
        predecessor to record the supersession explicitly.
        """
        jurisdiction = await self.s.scalar(
            select(Jurisdiction).where(
                Jurisdiction.code == manifest.jurisdiction_code))
        if jurisdiction is None:
            raise ManifestError(
                f"jurisdiction_code={manifest.jurisdiction_code!r} is not a "
                "governed jurisdiction; the registry reuses ref.jurisdiction "
                "rather than inventing a second one")

        source = await self.s.scalar(
            select(TaxSource).where(
                TaxSource.jurisdiction_id == jurisdiction.id,
                TaxSource.issuer_code == manifest.issuer_code.value,
                TaxSource.official_identifier == manifest.official_identifier,
            ))
        if source is None:
            source = TaxSource(
                source_type=manifest.source_type.value,
                issuer_code=manifest.issuer_code.value,
                jurisdiction_id=jurisdiction.id,
                official_identifier=manifest.official_identifier,
                title=manifest.title,
            )
            self.s.add(source)
            await self.s.flush()
        elif source.source_type != manifest.source_type.value:
            # The same identifier from the same issuer cannot be a statute in
            # one manifest and a guide in the next. Fail closed rather than
            # letting the later claim quietly win.
            raise ManifestError(
                f"{manifest.official_identifier!r} is already registered as "
                f"{source.source_type}, not {manifest.source_type.value}")

        existing = await self.s.scalar(
            select(TaxSourceVersion).where(
                TaxSourceVersion.source_id == source.id,
                TaxSourceVersion.content_fingerprint == manifest.content_fingerprint,
            ))
        if existing is not None:
            return existing

        predecessor_id = None
        if manifest.supersedes_fingerprint is not None:
            predecessor = await self.s.scalar(
                select(TaxSourceVersion).where(
                    TaxSourceVersion.source_id == source.id,
                    TaxSourceVersion.content_fingerprint
                    == manifest.supersedes_fingerprint,
                ))
            if predecessor is None:
                raise ManifestError(
                    "supersedes_fingerprint names no registered edition of this "
                    "source; supersession is never inferred")
            predecessor_id = predecessor.id

        version = TaxSourceVersion(
            source_id=source.id,
            edition=manifest.edition,
            tax_year=manifest.tax_year,
            publication_date=manifest.publication_date,
            effective_from=manifest.effective_from,
            effective_to=manifest.effective_to,
            retrieved_at=manifest.retrieved_at,
            official_locator=manifest.official_locator,
            content_fingerprint=manifest.content_fingerprint,
            fingerprint_method=manifest.fingerprint_method.value,
            status=manifest.status.value,
            supersedes_version_id=predecessor_id,
            manifest_schema_version=manifest.manifest_schema_version,
        )
        self.s.add(version)
        await self.s.flush()
        return version

    async def cite(
        self, source_version_id: uuid.UUID, locator: SourceLocator
    ) -> SourceCitation:
        """Get or create the citation for one location in one edition.

        Reused rather than duplicated: the same location digests identically
        however the manifest ordered its keys, so two rules citing one section
        share a row instead of copying the source metadata into each.
        """
        version = await self.s.get(TaxSourceVersion, source_version_id)
        if version is None:
            raise ManifestError(
                "unknown source version; a citation cannot name an edition "
                "that was never registered")

        digest = locator.digest()
        existing = await self.s.scalar(
            select(SourceCitation).where(
                SourceCitation.source_version_id == source_version_id,
                SourceCitation.locator_hash == digest,
            ))
        if existing is not None:
            return existing

        citation = SourceCitation(
            source_version_id=source_version_id,
            locator=locator.canonical(),
            locator_hash=digest,
            label=locator.label(),
        )
        self.s.add(citation)
        await self.s.flush()
        return citation

    async def attach(
        self, citation_id: uuid.UUID, **subject: uuid.UUID
    ) -> KnowledgeCitation:
        """Attach a citation to exactly one governed knowledge object.

        The keyword names the subject kind — `rule_version_id`, `formula_id`,
        `tax_bracket_set_id`, `contribution_limit_id`, `benefit_parameter_id` —
        and the database CHECK enforces that exactly one arrives.

        `calc_constant_id` is here because a constant is read by the engine
        directly: nothing owns it, so nothing could pass provenance down to it.
        """
        allowed = {
            "rule_version_id", "formula_id", "tax_bracket_set_id",
            "contribution_limit_id", "benefit_parameter_id", "calc_constant_id",
        }
        unknown = sorted(set(subject) - allowed)
        if unknown:
            raise ManifestError(f"unknown citation subject(s) {unknown}")
        if len(subject) != 1:
            raise ManifestError(
                f"a citation attaches to exactly one subject, got {len(subject)}")

        link = KnowledgeCitation(citation_id=citation_id, **subject)
        self.s.add(link)
        await self.s.flush()
        return link

    # ---------------------------------------------------------------- reads --
    async def _resolve(self, links: list[KnowledgeCitation]
                       ) -> tuple[ResolvedCitation, ...]:
        """Resolve links to pinned provenance in a FIXED number of queries.

        Three statements regardless of how many citations there are — the
        obvious per-citation walk (citation, then version, then source) is an
        N+1 that would make provenance for a rule set cost hundreds of round
        trips.
        """
        if not links:
            return ()

        citations = {
            c.id: c for c in await self.s.scalars(
                select(SourceCitation).where(
                    SourceCitation.id.in_({link.citation_id for link in links})))
        }
        missing = sorted(
            str(link.citation_id) for link in links
            if link.citation_id not in citations)
        if missing:
            raise ProvenanceUnavailable(
                f"citation(s) {missing} are pinned but no longer resolvable")

        versions = {
            v.id: v for v in await self.s.scalars(
                select(TaxSourceVersion).where(
                    TaxSourceVersion.id.in_(
                        {c.source_version_id for c in citations.values()})))
        }
        # Which of those editions have a successor. Read here rather than from a
        # status column so the answer cannot go stale.
        superseded = set(await self.s.scalars(
            select(TaxSourceVersion.supersedes_version_id).where(
                TaxSourceVersion.supersedes_version_id.in_(set(versions)))))

        sources = {
            s.id: s for s in await self.s.scalars(
                select(TaxSource).where(
                    TaxSource.id.in_({v.source_id for v in versions.values()})))
        }
        jurisdictions = {
            j.id: j for j in await self.s.scalars(
                select(Jurisdiction).where(
                    Jurisdiction.id.in_({s.jurisdiction_id for s in sources.values()})))
        }

        resolved: list[ResolvedCitation] = []
        for link in links:
            citation = citations[link.citation_id]
            version = versions.get(citation.source_version_id)
            if version is None:
                raise ProvenanceUnavailable(
                    f"source version {citation.source_version_id} is pinned but "
                    "missing; the registry does not substitute a newer edition")
            source = sources.get(version.source_id)
            if source is None:
                raise ProvenanceUnavailable(
                    f"source {version.source_id} is pinned but missing")
            resolved.append(ResolvedCitation(
                citation_id=citation.id,
                locator=dict(citation.locator),
                label=citation.label,
                source_version_id=version.id,
                edition=version.edition,
                content_fingerprint=version.content_fingerprint,
                fingerprint_method=version.fingerprint_method,
                official_locator=version.official_locator,
                version_status=version.status,
                superseded=version.id in superseded,
                tax_year=version.tax_year,
                publication_date=version.publication_date,
                effective_from=version.effective_from,
                effective_to=version.effective_to,
                retrieved_at=version.retrieved_at,
                source_id=source.id,
                source_type=source.source_type,
                issuer_code=source.issuer_code,
                jurisdiction_code=jurisdictions[source.jurisdiction_id].code,
                official_identifier=source.official_identifier,
                title=source.title,
            ))
        # Deterministic order: identity, never insertion or query order.
        resolved.sort(key=lambda r: (r.official_identifier, r.edition,
                                     r.label or "", str(r.citation_id)))
        return tuple(resolved)

    async def resolve_citations(
        self, citation_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, ResolvedCitation]:
        """Resolve citations named DIRECTLY, before anything links to them.

        The `provenance_for_*` readers answer "what supports this published
        object?". Authoring asks the question a step earlier — "may these
        citations support an object that does not exist yet?" — and needs the
        same pinned resolution with the same fixed statement count. Reusing
        `_resolve` is what keeps the two answers identical.
        """
        if not citation_ids:
            return {}
        links = [
            KnowledgeCitation(citation_id=cid) for cid in dict.fromkeys(citation_ids)
        ]
        return {r.citation_id: r for r in await self._resolve(links)}

    async def provenance_for_rule_version(
        self, rule_version_id: uuid.UUID
    ) -> tuple[ResolvedCitation, ...]:
        """Every source citation supporting one rule version.

        Deadlines and required documents inherit through this: they hang off a
        rule version and have no independent existence, so a separate link would
        be a second answer to the same question.
        """
        links = list(await self.s.scalars(
            select(KnowledgeCitation).where(
                KnowledgeCitation.rule_version_id == rule_version_id)))
        return await self._resolve(links)

    async def provenance_for_formula(
        self, formula_id: uuid.UUID
    ) -> tuple[ResolvedCitation, ...]:
        """A formula's OWN provenance. It needs one: a formula is shared across
        rules, so "the rule that uses it" stops identifying anything the moment
        two do."""
        links = list(await self.s.scalars(
            select(KnowledgeCitation).where(
                KnowledgeCitation.formula_id == formula_id)))
        return await self._resolve(links)

    async def provenance_for_reference_data(
        self, **subject: uuid.UUID
    ) -> tuple[ResolvedCitation, ...]:
        """Reference data's own provenance — the case that cannot inherit.

        Brackets, contribution limits and indexed parameters are read by the
        engine directly and never pass through a rule version, so without this
        they would be exactly what §17 forbids: unexplained constants.
        """
        allowed = {
            "tax_bracket_set_id", "contribution_limit_id", "benefit_parameter_id",
            "calc_constant_id"}
        if len(subject) != 1 or set(subject) - allowed:
            raise ManifestError(
                f"resolve exactly one reference-data subject from {sorted(allowed)}")
        (column, value), = subject.items()
        links = list(await self.s.scalars(
            select(KnowledgeCitation).where(
                getattr(KnowledgeCitation, column) == value)))
        return await self._resolve(links)
