"""Entry: Tax Knowledge Source Registry — provenance against real persistence.

What is at stake: that a historical rule keeps resolving the exact edition it
was authored against no matter what is published later, that a changed document
becomes a new version rather than overwriting the old one, that re-importing the
same bytes is idempotent, and that no customer role can register tax law.

Every test builds its own source, version, citation and knowledge link. Nothing
here relies on residue, and nothing registers a rule.
"""
import contextlib
import uuid
from datetime import date

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.database.models import (
    CalcFormula,
    Jurisdiction,
    TaxBracketSet,
    TaxRule,
    TaxRuleVersion,
    TaxSource,
    TaxSourceVersion,
)
from app.services.tax_kb.sources.domain import (
    SOURCE_MANIFEST_SCHEMA_VERSION,
    ManifestError,
    SourceLocator,
    fingerprint_bytes,
    manifest_from_payload,
)
from app.services.tax_kb.sources.registry import (
    ProvenanceUnavailable,
    TaxSourceRegistry,
)

TAX_YEAR = 2025


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


def _manifest(**over):
    """A distinct governed source per call — no shared residue."""
    payload = {
        "source_type": "CRA_GUIDE",
        "issuer_code": "CANADA_REVENUE_AGENCY",
        "jurisdiction_code": "FED",
        "official_identifier": f"T{uuid.uuid4().hex[:8].upper()}",
        "title": "Employment Expenses",
        "edition": "2025 edition",
        "official_locator": "https://example.invalid/guide",
        "content_fingerprint": fingerprint_bytes(uuid.uuid4().bytes),
        "fingerprint_method": "RAW_BYTES_SHA256",
        "retrieved_at": "2026-03-01T00:00:00+00:00",
    }
    payload.update(over)
    return manifest_from_payload(payload)


def _authoring_url() -> str:
    """An asyncpg URL authenticated with TAX-KNOWLEDGE AUTHORING authority.

    The customer application login deliberately cannot write these tables — the
    migration revokes exactly that, and `test_the_customer_runtime_role_cannot_
    write_tax_law` asserts it. Registration therefore runs under the owner, the
    same authority every existing tax_kb seed is applied with, and this suite
    connects the same way the internal tooling does rather than pretending the
    app role could.

    Derived from the harness's own database URL, never hardcoded: a fixture
    naming a database the harness did not provision certifies nothing.
    """
    import os
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(os.environ.get("ONYX_DATABASE_URL", ""))
    query = parse_qs(parsed.query)
    database = (parsed.path or "/onyx_test").lstrip("/").split("?")[0]
    host = query.get("host", ["/var/run/postgresql"])[0]
    port = query.get("port", ["5432"])[0]
    return (f"postgresql+asyncpg://onyx_migrator@/{database}"
            f"?host={host}&port={port}")


@contextlib.asynccontextmanager
async def _authoring():
    """One short-lived authoring session, disposed on exit.

    Disposed rather than pooled because Entry 11B5E5's lesson applies to any
    engine a test creates: pooled connections that outlive their event loop
    fail an unrelated test later in the suite.
    """
    engine = create_async_engine(_authoring_url(), poolclass=NullPool, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
        await engine.dispose()


async def _rule_version() -> uuid.UUID:
    """This suite's own rule version. No outcome, no lever — nothing here
    publishes or evaluates a rule."""
    async with _authoring() as s:
        jurisdiction = await s.scalar(
            select(Jurisdiction).where(Jurisdiction.code == "FED"))
        code = f"SRC_{uuid.uuid4().hex[:8].upper()}"
        rule = TaxRule(code=code, name=code, category="deduction",
                       jurisdiction_id=jurisdiction.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=TAX_YEAR,
            effective_date=date(TAX_YEAR, 1, 1), status="published",
            description="source registry fixture")
        s.add(version)
        await s.flush()
        return version.id


# ===========================================================================
# Registration and idempotency (§24, §44)
# ===========================================================================
@pytest.mark.asyncio
async def test_registering_a_source_creates_the_publication_and_its_edition():
    manifest = _manifest(tax_year=TAX_YEAR, publication_date="2026-01-05",
                         effective_from="2025-01-01", effective_to="2025-12-31")
    async with _authoring() as s:
        version = await TaxSourceRegistry(s).register(manifest)
        source = await s.get(TaxSource, version.source_id)

        assert source.official_identifier == manifest.official_identifier
        assert source.source_type == "CRA_GUIDE"
        assert source.issuer_code == "CANADA_REVENUE_AGENCY"
        assert version.content_fingerprint == manifest.content_fingerprint
        assert version.fingerprint_method == "RAW_BYTES_SHA256"
        assert version.status == "ACTIVE"
        assert version.supersedes_version_id is None
        assert version.manifest_schema_version == SOURCE_MANIFEST_SCHEMA_VERSION
        assert version.tax_year == TAX_YEAR
        assert version.publication_date == date(2026, 1, 5)


@pytest.mark.asyncio
async def test_re_importing_identical_content_is_idempotent():
    """§44. Identity is the source plus its content — not when it was fetched."""
    manifest = _manifest()
    async with _authoring() as s:
        registry = TaxSourceRegistry(s)
        first = await registry.register(manifest)
        second = await registry.register(manifest)

        assert first.id == second.id
        count = await s.scalar(text(
            "SELECT count(*) FROM tax_kb.tax_source_version WHERE source_id = :s"
        ), {"s": str(first.source_id)})
        assert count == 1


@pytest.mark.asyncio
async def test_retrieval_noise_does_not_create_a_second_version():
    """§24 explicitly: a different retrieval time, a different URL query string
    and a different local path describe HOW a document was fetched, not WHICH
    document it is."""
    base = _manifest()
    async with _authoring() as s:
        registry = TaxSourceRegistry(s)
        first = await registry.register(base)
        again = await registry.register(_manifest(
            official_identifier=base.official_identifier,
            content_fingerprint=base.content_fingerprint,
            official_locator="https://example.invalid/guide?utm_source=email",
            retrieved_at="2026-09-09T09:09:09+00:00",
            edition="2025 edition (reprint label)"))

        assert first.id == again.id, "retrieval noise forked the edition"


@pytest.mark.asyncio
async def test_a_manifest_naming_an_ungoverned_jurisdiction_is_refused():
    async with _authoring() as s:
        with pytest.raises(ManifestError, match="not a governed jurisdiction"):
            await TaxSourceRegistry(s).register(_manifest(jurisdiction_code="CA-XX"))


@pytest.mark.asyncio
async def test_the_same_identifier_cannot_change_source_type():
    """A guide cannot become a statute because a later manifest said so."""
    first = _manifest()
    async with _authoring() as s:
        registry = TaxSourceRegistry(s)
        await registry.register(first)
        with pytest.raises(ManifestError, match="already registered as"):
            await registry.register(_manifest(
                official_identifier=first.official_identifier,
                source_type="STATUTE"))


# ===========================================================================
# Content change and supersession (§10, §22, §23, §43)
# ===========================================================================
@pytest.mark.asyncio
async def test_changed_content_becomes_a_new_version_and_never_overwrites():
    """§43. The same logical source retrieved later with different bytes."""
    first = _manifest()
    async with _authoring() as s:
        registry = TaxSourceRegistry(s)
        s1 = await registry.register(first)
        s1_fingerprint = s1.content_fingerprint

        s2 = await registry.register(_manifest(
            official_identifier=first.official_identifier,
            edition="2026 edition",
            content_fingerprint=fingerprint_bytes(b"revised content"),
            supersedes_fingerprint=s1_fingerprint))

        assert s2.id != s1.id
        assert s2.supersedes_version_id == s1.id
        assert s2.source_id == s1.source_id, "supersession forked the publication"

        # S1 is untouched — the whole point.
        reloaded = await s.get(TaxSourceVersion, s1.id)
        assert reloaded.content_fingerprint == s1_fingerprint
        assert reloaded.edition == first.edition
        assert reloaded.status == "ACTIVE", (
            "registering a successor rewrote the predecessor's own status")


@pytest.mark.asyncio
async def test_supersession_is_never_inferred():
    """A caller names the predecessor explicitly or supersession does not
    happen. Guessing from dates or titles is how the wrong edition gets
    retired."""
    first = _manifest()
    async with _authoring() as s:
        registry = TaxSourceRegistry(s)
        await registry.register(first)
        later = await registry.register(_manifest(
            official_identifier=first.official_identifier,
            edition="2026 edition",
            content_fingerprint=fingerprint_bytes(b"unrelated later content")))
        assert later.supersedes_version_id is None

        with pytest.raises(ManifestError, match="names no registered edition"):
            await registry.register(_manifest(
                official_identifier=first.official_identifier,
                edition="2027",
                content_fingerprint=fingerprint_bytes(b"c"),
                supersedes_fingerprint=fingerprint_bytes(b"never registered")))


@pytest.mark.asyncio
async def test_one_predecessor_can_only_be_superseded_once():
    """A linear chain, enforced by the database: "what replaced this" has one
    answer."""
    from sqlalchemy.exc import IntegrityError

    first = _manifest()
    async with _authoring() as s:
        registry = TaxSourceRegistry(s)
        s1 = await registry.register(first)
        await registry.register(_manifest(
            official_identifier=first.official_identifier, edition="B",
            content_fingerprint=fingerprint_bytes(b"B"),
            supersedes_fingerprint=s1.content_fingerprint))

        identifier, predecessor = first.official_identifier, s1.content_fingerprint

    # A SEPARATE session: the expected violation aborts its transaction, and
    # sharing one would make every later assertion fail for the wrong reason.
    async with _authoring() as s:
        with pytest.raises(IntegrityError):
            await TaxSourceRegistry(s).register(_manifest(
                official_identifier=identifier, edition="C",
                content_fingerprint=fingerprint_bytes(b"C"),
                supersedes_fingerprint=predecessor))
        # The violation aborted the transaction; unwind it explicitly rather
        # than letting the session try to commit an aborted one on exit.
        await s.rollback()


# ===========================================================================
# Citation identity and reuse (§13, §14, §45)
# ===========================================================================
@pytest.mark.asyncio
async def test_one_source_version_carries_many_distinct_locators():
    async with _authoring() as s:
        registry = TaxSourceRegistry(s)
        version = await registry.register(_manifest())
        a = await registry.cite(version.id, SourceLocator({"section": "8", "subsection": "1"}))
        b = await registry.cite(version.id, SourceLocator({"section": "8", "subsection": "2"}))
        c = await registry.cite(version.id, SourceLocator({"table": "A", "page": "12"}))

        assert len({a.id, b.id, c.id}) == 3


@pytest.mark.asyncio
async def test_the_same_location_is_one_reusable_citation_row():
    """§14: the source metadata is not copied into every citing object."""
    async with _authoring() as s:
        registry = TaxSourceRegistry(s)
        version = await registry.register(_manifest())
        first = await registry.cite(
            version.id, SourceLocator({"section": "118.2", "paragraph": "a"}))
        again = await registry.cite(
            version.id, SourceLocator({"paragraph": "a", "section": "118.2"}))

        assert first.id == again.id, "field order forked the citation"


@pytest.mark.asyncio
async def test_the_same_section_under_two_editions_stays_distinguishable():
    """§45. Section 118.2 of the 2025 edition and of the 2026 edition are
    different provenance, however identical the section number looks."""
    first = _manifest()
    async with _authoring() as s:
        registry = TaxSourceRegistry(s)
        v1 = await registry.register(first)
        v2 = await registry.register(_manifest(
            official_identifier=first.official_identifier, edition="2026",
            content_fingerprint=fingerprint_bytes(b"2026 bytes"),
            supersedes_fingerprint=v1.content_fingerprint))

        locator = SourceLocator({"section": "118.2"})
        c1 = await registry.cite(v1.id, locator)
        c2 = await registry.cite(v2.id, locator)

        assert c1.id != c2.id
        assert c1.locator_hash == c2.locator_hash, (
            "the locator digest should be edition-independent; the EDITION is "
            "what distinguishes these citations")


@pytest.mark.asyncio
async def test_citing_an_unregistered_source_version_is_refused():
    async with _authoring() as s:
        with pytest.raises(ManifestError, match="unknown source version"):
            await TaxSourceRegistry(s).cite(
                uuid.uuid4(), SourceLocator({"section": "1"}))


@pytest.mark.asyncio
async def test_a_citation_attaches_to_exactly_one_subject():
    from sqlalchemy.exc import IntegrityError

    rule_version_id = await _rule_version()
    async with _authoring() as s:
        registry = TaxSourceRegistry(s)
        version = await registry.register(_manifest())
        citation = await registry.cite(version.id, SourceLocator({"section": "8"}))

        with pytest.raises(ManifestError, match="exactly one subject"):
            await registry.attach(citation.id)

    # THE DATABASE ENFORCES IT TOO, not just the service — a CHECK the service
    # happens to duplicate is the one that survives a future caller bypassing
    # the service. Zero subjects and two subjects are both refused.
    for values in ("(:c, NULL, NULL, NULL, NULL, NULL)",
                   "(:c, :r, :f, NULL, NULL, NULL)"):
        async with _authoring() as s:
            formula = CalcFormula(
                code=f"SRCX_{uuid.uuid4().hex[:8]}", expression="amount",
                expression_lang="rpn", output_unit="CAD", description="probe")
            s.add(formula)
            await s.flush()
            with pytest.raises(IntegrityError):
                await s.execute(text(
                    "INSERT INTO tax_kb.knowledge_citation "
                    "  (citation_id, rule_version_id, formula_id, "
                    "   tax_bracket_set_id, contribution_limit_id, "
                    "   benefit_parameter_id) VALUES " + values),
                    {"c": str(citation.id), "r": str(rule_version_id),
                     "f": str(formula.id)})
            await s.rollback()


# ===========================================================================
# Rule provenance closure (§15, §20)
# ===========================================================================
@pytest.mark.asyncio
async def test_a_rule_version_resolves_every_citation_it_pins():
    rule_version_id = await _rule_version()
    async with _authoring() as s:
        registry = TaxSourceRegistry(s)
        statute = await registry.register(_manifest(
            source_type="STATUTE", issuer_code="PARLIAMENT_OF_CANADA",
            title="Income Tax Act", edition="consolidated 2025-06-20"))
        guide = await registry.register(_manifest(title="Employment Expenses"))

        for version, locator in (
            (statute.id, SourceLocator({"section": "8", "subsection": "1"})),
            (statute.id, SourceLocator({"section": "8", "subsection": "10"})),
            (guide.id, SourceLocator({"page": "14", "heading": "Conditions"})),
        ):
            citation = await registry.cite(version, locator)
            await registry.attach(citation.id, rule_version_id=rule_version_id)

        resolved = await registry.provenance_for_rule_version(rule_version_id)

    assert len(resolved) == 3, "a rule version must carry MANY citations (§14)"
    assert {r.source_type for r in resolved} == {"STATUTE", "CRA_GUIDE"}
    assert all(r.jurisdiction_code == "FED" for r in resolved)
    assert all(len(r.content_fingerprint) == 64 for r in resolved)


@pytest.mark.asyncio
async def test_provenance_order_is_deterministic():
    rule_version_id = await _rule_version()
    async with _authoring() as s:
        registry = TaxSourceRegistry(s)
        version = await registry.register(_manifest())
        for section in ("9", "8", "10", "1"):
            citation = await registry.cite(
                version.id, SourceLocator({"section": section}))
            await registry.attach(citation.id, rule_version_id=rule_version_id)

        first = await registry.provenance_for_rule_version(rule_version_id)
        second = await registry.provenance_for_rule_version(rule_version_id)

    assert [r.citation_id for r in first] == [r.citation_id for r in second]
    assert [r.label for r in first] == sorted(r.label for r in first)


@pytest.mark.asyncio
async def test_a_rule_without_provenance_resolves_empty_rather_than_failing():
    """§15: existing synthetic rules carry no citations and must not break. The
    contract exists now; enforcement belongs to the publication entry."""
    rule_version_id = await _rule_version()
    async with _authoring() as s:
        assert await TaxSourceRegistry(s).provenance_for_rule_version(
            rule_version_id) == ()


# ===========================================================================
# §42 / §49 — historical provenance never drifts to a newer edition
# ===========================================================================
@pytest.mark.asyncio
async def test_a_pinned_rule_still_resolves_its_own_edition_after_supersession():
    """THE CENTRAL ACCEPTANCE. R1 cites S1; S2 is published; R1 still resolves
    S1, and S2 is never substituted."""
    rule_version_id = await _rule_version()
    first = _manifest()
    async with _authoring() as s:
        registry = TaxSourceRegistry(s)
        s1 = await registry.register(first)
        citation = await registry.cite(s1.id, SourceLocator({"section": "118.2"}))
        await registry.attach(citation.id, rule_version_id=rule_version_id)

        before = await registry.provenance_for_rule_version(rule_version_id)

        s2 = await registry.register(_manifest(
            official_identifier=first.official_identifier,
            edition="2026 edition",
            content_fingerprint=fingerprint_bytes(b"2026 revised text"),
            supersedes_fingerprint=s1.content_fingerprint))

        after = await registry.provenance_for_rule_version(rule_version_id)

    (pinned,) = after
    assert pinned.source_version_id == s1.id
    assert pinned.source_version_id != s2.id, "a newer edition was substituted"
    assert pinned.content_fingerprint == s1.content_fingerprint
    assert pinned.edition == first.edition

    # Superseded is reported, never acted on — §22: a newer edition existing is
    # not an integrity failure and does not change what R1 was authored against.
    assert pinned.superseded is True
    assert before[0].superseded is False
    assert before[0].source_version_id == pinned.source_version_id


@pytest.mark.asyncio
async def test_missing_pinned_provenance_fails_rather_than_falling_back():
    """§21. If the pinned edition is gone the registry raises. It must never
    answer "here is the current page instead" — that answers a different
    question from the one asked."""
    rule_version_id = await _rule_version()
    async with _authoring() as s:
        registry = TaxSourceRegistry(s)
        version = await registry.register(_manifest())
        citation = await registry.cite(version.id, SourceLocator({"section": "1"}))
        await registry.attach(citation.id, rule_version_id=rule_version_id)
        assert await registry.provenance_for_rule_version(rule_version_id)

    # Break the pin the way real corruption would: the link survives, the
    # citation does not.
    async with _authoring() as s:
        await s.execute(text(
            "ALTER TABLE tax_kb.knowledge_citation "
            "  DROP CONSTRAINT knowledge_citation_citation_id_fkey"))
        await s.execute(text(
            "DELETE FROM tax_kb.source_citation WHERE id = :c"),
            {"c": str(citation.id)})
    try:
        async with _authoring() as s:
            with pytest.raises(ProvenanceUnavailable, match="no longer resolvable"):
                await TaxSourceRegistry(s).provenance_for_rule_version(
                    rule_version_id)
    finally:
        async with _authoring() as s:
            await s.execute(text(
                "DELETE FROM tax_kb.knowledge_citation WHERE citation_id = :c"),
                {"c": str(citation.id)})
            await s.execute(text(
                "ALTER TABLE tax_kb.knowledge_citation "
                "  ADD CONSTRAINT knowledge_citation_citation_id_fkey "
                "  FOREIGN KEY (citation_id) REFERENCES tax_kb.source_citation(id)"))


@pytest.mark.asyncio
async def test_resolving_provenance_makes_no_network_call():
    """§21/§27: there is no retrieval path, so there is nothing to disable."""
    import app.services.tax_kb.sources.registry as registry_module

    source = registry_module.__file__
    with open(source) as handle:
        text_body = handle.read()
    for forbidden in ("requests", "httpx", "urlopen", "aiohttp", "openai",
                      "anthropic", "embedding", "vector"):
        assert forbidden not in text_body, (
            f"the registry references {forbidden}")


# ===========================================================================
# Jurisdiction (§6, §46)
# ===========================================================================
@pytest.mark.asyncio
async def test_federal_and_provincial_sources_stay_distinct():
    identifier = f"SHARED_{uuid.uuid4().hex[:8].upper()}"
    async with _authoring() as s:
        registry = TaxSourceRegistry(s)
        federal = await registry.register(_manifest(
            official_identifier=identifier, jurisdiction_code="FED"))
        ontario = await registry.register(_manifest(
            official_identifier=identifier, jurisdiction_code="ON",
            issuer_code="PROVINCIAL_TAX_AUTHORITY"))
        bc = await registry.register(_manifest(
            official_identifier=identifier, jurisdiction_code="BC",
            issuer_code="PROVINCIAL_TAX_AUTHORITY"))

        assert len({federal.source_id, ontario.source_id, bc.source_id}) == 3, (
            "an identical identifier collapsed three jurisdictions into one")


@pytest.mark.asyncio
async def test_an_ontario_source_does_not_satisfy_a_federal_citation():
    """§46: matching titles must not let a provincial source stand in for a
    federal one."""
    rule_version_id = await _rule_version()
    identifier = f"SHARED_{uuid.uuid4().hex[:8].upper()}"
    async with _authoring() as s:
        registry = TaxSourceRegistry(s)
        ontario = await registry.register(_manifest(
            official_identifier=identifier, jurisdiction_code="ON",
            issuer_code="PROVINCIAL_TAX_AUTHORITY"))
        citation = await registry.cite(ontario.id, SourceLocator({"section": "8"}))
        await registry.attach(citation.id, rule_version_id=rule_version_id)

        (resolved,) = await registry.provenance_for_rule_version(rule_version_id)

    assert resolved.jurisdiction_code == "ON"
    assert resolved.jurisdiction_code != "FED", (
        "a provincial source was reported as federal provenance")


# ===========================================================================
# Formula and reference-data provenance (§16, §17, §48)
# ===========================================================================
@pytest.mark.asyncio
async def test_a_formula_carries_its_own_provenance():
    """§16: a formula is shared across rules, so "the rule that uses it" stops
    identifying anything the moment two do."""
    async with _authoring() as s:
        formula = CalcFormula(
            code=f"SRCF_{uuid.uuid4().hex[:8]}", expression="amount",
            expression_lang="rpn", output_unit="CAD", description="probe")
        s.add(formula)
        await s.flush()

        registry = TaxSourceRegistry(s)
        version = await registry.register(_manifest(
            source_type="INDEXED_PARAMETER_PUBLICATION",
            issuer_code="DEPARTMENT_OF_FINANCE_CANADA"))
        citation = await registry.cite(
            version.id, SourceLocator({"table": "2", "page": "3"}))
        await registry.attach(citation.id, formula_id=formula.id)

        (resolved,) = await registry.provenance_for_formula(formula.id)

    assert resolved.source_type == "INDEXED_PARAMETER_PUBLICATION"
    assert resolved.locator == {"page": "3", "table": "2"}


@pytest.mark.asyncio
async def test_reference_data_resolves_provenance_it_could_never_inherit():
    """§17, the case that matters most: a bracket set is read by the engine
    directly and never passes through a rule version, so without an explicit
    link it would be exactly the unexplained constant §17 forbids."""
    async with _authoring() as s:
        jurisdiction = await s.scalar(
            select(Jurisdiction).where(Jurisdiction.code == "FED"))
        bracket_set = TaxBracketSet(
            jurisdiction_id=jurisdiction.id, tax_year=TAX_YEAR,
            kind="income_tax")
        s.add(bracket_set)
        await s.flush()

        registry = TaxSourceRegistry(s)
        version = await registry.register(_manifest(
            source_type="RATE_TABLE", tax_year=TAX_YEAR))
        citation = await registry.cite(
            version.id, SourceLocator({"table": "1"}))
        await registry.attach(citation.id, tax_bracket_set_id=bracket_set.id)

        (resolved,) = await registry.provenance_for_reference_data(
            tax_bracket_set_id=bracket_set.id)

    assert resolved.source_type == "RATE_TABLE"
    assert resolved.tax_year == TAX_YEAR
    assert resolved.content_fingerprint


@pytest.mark.asyncio
async def test_reference_data_provenance_requires_exactly_one_subject():
    async with _authoring() as s:
        with pytest.raises(ManifestError, match="exactly one"):
            await TaxSourceRegistry(s).provenance_for_reference_data()


# ===========================================================================
# Security (§31, §32)
# ===========================================================================
@pytest.mark.asyncio
async def test_the_customer_runtime_role_cannot_write_tax_law():
    """§32. `tax_kb` DEFAULT PRIVILEGES grant onyx_app_rw arwd, so this is the
    assertion that the migration actually took that back."""
    async with _authoring() as s:
        rows = (await s.execute(text("""
            SELECT table_name, privilege_type
              FROM information_schema.role_table_grants
             WHERE grantee = 'onyx_app_rw'
               AND table_schema = 'tax_kb'
               AND table_name IN ('tax_source','tax_source_version',
                                  'source_citation','knowledge_citation')
             ORDER BY table_name, privilege_type
        """))).all()

    granted = {(t, p) for t, p in rows}
    for table in ("tax_source", "tax_source_version", "source_citation",
                  "knowledge_citation"):
        for privilege in ("INSERT", "UPDATE", "DELETE"):
            assert (table, privilege) not in granted, (
                f"the customer runtime role can {privilege} {table}")


@pytest.mark.asyncio
async def test_the_authoring_role_may_insert_but_never_rewrite_history():
    """§35: corrections happen by registering a new version and superseding,
    never by editing what a historical rule was authored against."""
    async with _authoring() as s:
        rows = (await s.execute(text("""
            SELECT table_name, privilege_type
              FROM information_schema.role_table_grants
             WHERE grantee = 'onyx_kb_admin'
               AND table_schema = 'tax_kb'
               AND table_name IN ('tax_source','tax_source_version',
                                  'source_citation','knowledge_citation')
        """))).all()

    granted = {(t, p) for t, p in rows}
    for table in ("tax_source", "tax_source_version", "source_citation",
                  "knowledge_citation"):
        assert (table, "INSERT") in granted, f"kb admin cannot register {table}"
        assert (table, "SELECT") in granted
        for privilege in ("UPDATE", "DELETE"):
            assert (table, privilege) not in granted, (
                f"kb admin can {privilege} {table}; history is not immutable")


@pytest.mark.asyncio
async def test_the_registry_is_not_reachable_from_any_customer_route(client):
    """§31: internal tooling. No consumer API registers authoritative tax law."""
    document = (await client.get("/api/v1/openapi.json")).json()
    for path in document["paths"]:
        for banned in ("source", "citation", "provenance", "registry"):
            assert banned not in path.lower(), (
                f"{path} exposes source registration to a customer surface")


@pytest.mark.asyncio
async def test_the_registry_tables_carry_no_tenant_column():
    """§33/§34: tax law is global reference knowledge. Forcing tenancy onto
    public statutes would be modelling them as customer data, and it would put
    them in the account-deletion universe where they do not belong."""
    async with _authoring() as s:
        rows = (await s.execute(text("""
            SELECT table_name, column_name
              FROM information_schema.columns
             WHERE table_schema = 'tax_kb'
               AND table_name IN ('tax_source','tax_source_version',
                                  'source_citation','knowledge_citation')
               AND column_name = 'user_id'
        """))).all()
    assert rows == [], f"a registry table carries a user_id: {rows}"


# ===========================================================================
# Performance (§50)
# ===========================================================================
@pytest.mark.asyncio
async def test_resolving_provenance_does_not_scale_with_citation_count():
    """§50. The obvious per-citation walk — citation, then version, then source
    — is an N+1 that would make provenance for a rule set cost hundreds of round
    trips."""
    async def statements_for(rule_version_id: uuid.UUID) -> tuple[int, int]:
        """Count on the AUTHORING engine, which is the one the registry uses.

        Listening on the application engine would have recorded zero statements
        for both sizes and reported a passing 0 == 0 — a counter pointed at the
        wrong connection cannot detect the N+1 it exists to catch.
        """
        seen: list[str] = []

        def record(conn, cur, statement, parameters, context, executemany):
            seen.append(statement)

        engine = create_async_engine(
            _authoring_url(), poolclass=NullPool, future=True)
        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            factory = async_sessionmaker(
                engine, expire_on_commit=False, class_=AsyncSession)
            session = factory()
            try:
                resolved = await TaxSourceRegistry(
                    session).provenance_for_rule_version(rule_version_id)
            finally:
                await session.close()
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)
            await engine.dispose()
        return len(seen), len(resolved)

    async def build(citations: int) -> uuid.UUID:
        rule_version_id = await _rule_version()
        async with _authoring() as s:
            registry = TaxSourceRegistry(s)
            version = await registry.register(_manifest())
            for i in range(citations):
                citation = await registry.cite(
                    version.id, SourceLocator({"section": f"{i}"}))
                await registry.attach(
                    citation.id, rule_version_id=rule_version_id)
        return rule_version_id

    few, many = await build(1), await build(25)
    one_count, one_len = await statements_for(few)
    many_count, many_len = await statements_for(many)

    assert one_len == 1 and many_len == 25, "the comparison is vacuous"
    assert one_count > 0, (
        "the statement counter recorded nothing; it is watching the wrong "
        "engine and could not detect an N+1")
    print(f"\nprovenance SQL: 1_citation={one_count} 25_citations={many_count}")  # noqa: T201
    assert many_count == one_count, (
        f"{many_count - one_count} extra statements for 24 more citations (N+1)")


# ===========================================================================
# §53 — no tax result changes
# ===========================================================================
@pytest.mark.asyncio
async def test_registering_sources_changes_no_rule_evaluation_input():
    """This entry is provenance infrastructure. The rules layer must not read
    the registry at all, so registering a source cannot move a tax outcome."""
    import app.services.tax_engine.rules_service as rules_module

    source = open(rules_module.__file__).read()
    for table in ("tax_source", "source_citation", "knowledge_citation",
                  "TaxSourceRegistry"):
        assert table not in source, (
            f"the rules evaluator reads {table}; provenance has become an "
            "evaluation input")
