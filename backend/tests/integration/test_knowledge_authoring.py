"""The governed authoring pipeline against real persistence.

What is at stake: that nothing reaches the rules evaluator without a bound
validated specification, governed provenance and a second operator's approval;
that validating changes nothing; that a draft edited after validation cannot
publish on its predecessor's approval; and that a rule published yesterday still
resolves exactly as it did after a successor publishes today.

Every test builds its own source, citation, rule and admins. Nothing here relies
on residue left by another test, and every assertion is scoped to the fixtures
the test itself created — published rules accumulate in a shared database.
"""
import contextlib
import os
import uuid
from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.exceptions import Forbidden
from app.database.models import (
    CalcFormula,
    KnowledgeCitation,
    RuleOutcome,
    RulePublication,
    TaxRule,
    TaxRuleVersion,
    ValidationReport,
)
from app.database.session import unit_of_work
from app.services.admin.service import AdminService
from app.services.tax_kb.authoring.codes import Family, Readiness, ValidationCode
from app.services.tax_kb.authoring.manifest import parse_manifest, parse_members
from app.services.tax_kb.authoring.policy import PUBLICATION_POLICY_VERSION
from app.services.tax_kb.authoring.service import (
    GOVERNED_CHANNEL,
    KnowledgeAuthoringService,
    NotPublishable,
    SpecChangedSinceValidation,
)
from app.services.tax_kb.authoring.spec import (
    SpecError,
    TaxKnowledgeDraftSpec,
)
from app.services.tax_kb.sources.domain import (
    SourceLocator,
    fingerprint_bytes,
    manifest_from_payload,
)
from app.services.tax_kb.sources.registry import TaxSourceRegistry
from app.services.tkms.governance.service import GovernanceService

# 2026, deliberately. Published rules ACCUMULATE in the shared test database:
# a full integration run leaves several hundred published 2025 versions behind,
# and the optimizer suites evaluate 2025. Adding this suite's rules to that pile
# is how an unrelated candidate ends up excluded for search-budget reasons in a
# combined run — the accumulation defect a previous entry paid for. Nothing here
# needs a particular year, so it uses one the optimizer suites do not.
TAX_YEAR = 2026


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


# ---------------------------------------------------------------------------
# Authoring authority
#
# The pipeline is internal operator tooling, not a customer route. The customer
# application login CANNOT write the source registry — migration 64 revokes
# exactly that — so a suite that authored as the app role would be certifying a
# topology production does not have.
# ---------------------------------------------------------------------------
def _authoring_url() -> str:
    parsed = urlparse(os.environ.get("ONYX_DATABASE_URL", ""))
    query = parse_qs(parsed.query)
    database = (parsed.path or "/onyx_test").lstrip("/").split("?")[0]
    host = query.get("host", ["/var/run/postgresql"])[0]
    port = query.get("port", ["5432"])[0]
    return (f"postgresql+asyncpg://onyx_migrator@/{database}"
            f"?host={host}&port={port}")


@contextlib.asynccontextmanager
async def _authoring():
    """One short-lived authoring session, disposed on exit."""
    engine = create_async_engine(_authoring_url(), poolclass=NullPool, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    session = factory()
    try:
        await session.execute(text("SET LOCAL app.actor_type = 'admin'"))
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _suffix() -> str:
    return uuid.uuid4().hex[:8].upper()


async def _citation(s, *, source_type: str = "CRA_GUIDE",
                    issuer: str = "CANADA_REVENUE_AGENCY") -> uuid.UUID:
    """A governed citation to a fresh registered source edition."""
    registry = TaxSourceRegistry(s)
    version = await registry.register(manifest_from_payload({
        "source_type": source_type,
        "issuer_code": issuer,
        "jurisdiction_code": "FED",
        "official_identifier": f"SRC{_suffix()}",
        "title": "A governed source",
        "edition": "2025 edition",
        "official_locator": "https://example.invalid/source",
        "content_fingerprint": fingerprint_bytes(uuid.uuid4().bytes),
        "fingerprint_method": "RAW_BYTES_SHA256",
        "retrieved_at": "2026-03-01T00:00:00+00:00",
    }))
    citation = await registry.cite(
        version.id, SourceLocator({"section": "146", "subsection": "1"}))
    return citation.id


async def _admins(s) -> tuple[uuid.UUID, uuid.UUID]:
    """Two distinct operators, because four-eyes needs two."""
    admin = AdminService(s)
    a = await admin.create_admin(f"kauth_{_suffix()}@onyx.io", "adminpass1",
                                 ["kb_admin"])
    b = await admin.create_admin(f"kauth_{_suffix()}@onyx.io", "adminpass1",
                                 ["kb_admin"])
    return a.id, b.id


def _draft_payload(code: str, citation_id: uuid.UUID, **over) -> dict:
    payload = {
        "rule_code": code,
        "name": f"{code} deduction",
        "category": "deduction",
        "jurisdiction_code": "FED",
        "tax_year": TAX_YEAR,
        "effective_from": f"{TAX_YEAR}-01-01",
        "description": "A governed fixture rule.",
        "eligibility_basis_codes": ["FIXTURE_BASIS"],
        "condition_group": {"logical_op": "AND", "conditions": [{
            "fact_key": "income.employment", "operator": "gt",
            "value_type": "money", "value_number": "1"}]},
        "outcomes": [{
            "outcome_type": "recommend", "priority": 1,
            "title_template": f"{code} opportunity",
            "economic_effect_type": "current_year_tax_reduction",
            "reversibility": "reversible",
            "portfolio_lever_code": "INCREASE_RRSP_DEDUCTION",
            "lever_parameters": {"amount": "action.cost_amount"},
            "formula_code": f"F_{code}"}],
        "actions": [{
            "action_code": "CONTRIBUTE", "description": "Contribute",
            "effort_rating": 2, "cost_type": "liquidity_commitment",
            "cost_amount": "8000"}],
        "formula": {
            "code": f"F_{code}",
            "expression": "contribution 0.3 *",
            "inputs": [{"param_name": "contribution",
                        "fact_key": "income.employment"}],
            "vectors": [{"name": "ten thousand",
                         "inputs": {"contribution": "10000"},
                         "expected": "3000.0"}]},
        "citation_ids": [str(citation_id)],
        "examples": [
            {"name": "earner", "facts": {"income.employment": "90000"},
             "expect_eligible": True, "expected_outcome_types": ["recommend"]},
            {"name": "no income", "facts": {"income.employment": "0"},
             "expect_eligible": False}],
    }
    payload.update(over)
    return payload


async def _staged(s, code: str, citation_id: uuid.UUID, **over):
    """Stage a draft and record its governed verdict. Returns (id, spec, report)."""
    service = KnowledgeAuthoringService(s)
    spec = TaxKnowledgeDraftSpec.read(_draft_payload(code, citation_id, **over))
    version_id = await service.stage_draft(spec)
    report = await service.validate(spec)
    await service.record_report(version_id, report)
    return version_id, spec, report


async def _approved(s, version_id: uuid.UUID, a: uuid.UUID, b: uuid.UUID) -> None:
    governance = GovernanceService(s)
    await governance.submit_for_review(a, version_id)
    await governance.approve(b, version_id)


# ===========================================================================
# Validation writes nothing
# ===========================================================================
@pytest.mark.asyncio
async def test_validation_is_side_effect_free():
    """§25. An operator must be able to run a dry run against production as
    often as they like."""
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        citation_id = await _citation(s)
        spec = TaxKnowledgeDraftSpec.read(_draft_payload(code, citation_id))
        service = KnowledgeAuthoringService(s)

        before = {
            "rules": await s.scalar(select(func.count()).select_from(TaxRule)),
            "versions": await s.scalar(
                select(func.count()).select_from(TaxRuleVersion)),
            "formulas": await s.scalar(
                select(func.count()).select_from(CalcFormula)),
            "reports": await s.scalar(
                select(func.count()).select_from(ValidationReport)),
            "links": await s.scalar(
                select(func.count()).select_from(KnowledgeCitation)),
        }
        report = await service.validate(spec)
        after = {
            "rules": await s.scalar(select(func.count()).select_from(TaxRule)),
            "versions": await s.scalar(
                select(func.count()).select_from(TaxRuleVersion)),
            "formulas": await s.scalar(
                select(func.count()).select_from(CalcFormula)),
            "reports": await s.scalar(
                select(func.count()).select_from(ValidationReport)),
            "links": await s.scalar(
                select(func.count()).select_from(KnowledgeCitation)),
        }
        assert before == after, "validation wrote something"
        assert report.publishable, report.error_codes()


@pytest.mark.asyncio
async def test_a_publishable_report_reports_every_family():
    """§74. A family with no readiness is an unanswered question, not a pass."""
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        report = await KnowledgeAuthoringService(s).validate(
            TaxKnowledgeDraftSpec.read(
                _draft_payload(code, await _citation(s))))

    for family in Family:
        assert report.readiness_of(family) in tuple(Readiness)
    assert report.readiness_of(Family.PROVENANCE) is Readiness.READY
    assert report.readiness_of(Family.FORMULA) is Readiness.READY
    # Declared by nothing, so authoritatively absent rather than empty-and-fine.
    assert report.readiness_of(Family.DEADLINES) is Readiness.NOT_APPLICABLE
    assert report.readiness_of(Family.REFERENCE_DATA) is Readiness.NOT_APPLICABLE
    # Authority is real and is checked at publication, so claiming it does not
    # apply would be as wrong as claiming it is ready.
    assert report.readiness_of(Family.AUTHORITY) is \
        Readiness.DEFERRED_TO_PUBLICATION
    assert report.publication_policy_version == PUBLICATION_POLICY_VERSION


# ===========================================================================
# Provenance
# ===========================================================================
@pytest.mark.asyncio
async def test_a_rule_with_no_citation_is_not_publishable():
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        report = await KnowledgeAuthoringService(s).validate(
            TaxKnowledgeDraftSpec.read(
                _draft_payload(code, await _citation(s), citation_ids=[])))
    assert not report.publishable
    assert str(ValidationCode.SOURCE_REQUIRED) in report.error_codes()


@pytest.mark.asyncio
async def test_an_unregistered_citation_is_refused():
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        report = await KnowledgeAuthoringService(s).validate(
            TaxKnowledgeDraftSpec.read(_draft_payload(
                code, await _citation(s),
                citation_ids=[str(uuid.uuid4())])))
    assert str(ValidationCode.SOURCE_CITATION_UNKNOWN) in report.error_codes()


@pytest.mark.asyncio
async def test_commentary_alone_cannot_carry_production_authority():
    """§12. A practitioner's article may be recorded and cited; it may not be
    the only thing standing behind a figure a user acts on."""
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        commentary = await _citation(s, source_type="SECONDARY_COMMENTARY",
                                     issuer="OTHER")
        report = await KnowledgeAuthoringService(s).validate(
            TaxKnowledgeDraftSpec.read(_draft_payload(
                code, commentary, citation_ids=[str(commentary)])))
    assert not report.publishable
    assert str(ValidationCode.SOURCE_NOT_PRODUCTION_QUALIFYING) in \
        report.error_codes()


@pytest.mark.asyncio
async def test_commentary_alongside_a_statute_is_permitted():
    """The policy is an allow list of KINDS, not a ban on recording commentary.
    Cited beside a qualifying source it is additional context."""
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        statute = await _citation(s, source_type="STATUTE",
                                  issuer="PARLIAMENT_OF_CANADA")
        commentary = await _citation(s, source_type="SECONDARY_COMMENTARY",
                                     issuer="OTHER")
        report = await KnowledgeAuthoringService(s).validate(
            TaxKnowledgeDraftSpec.read(_draft_payload(
                code, statute, citation_ids=[str(statute), str(commentary)])))
    assert report.publishable, report.error_codes()
    assert any(f.code is ValidationCode.SOURCE_NOT_PRODUCTION_QUALIFYING
               for f in report.warnings), (
        "the non-qualifying source must still be reported, just not as a block")


@pytest.mark.asyncio
async def test_a_withdrawn_source_edition_cannot_carry_authority():
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        registry = TaxSourceRegistry(s)
        version = await registry.register(manifest_from_payload({
            "source_type": "CRA_GUIDE",
            "issuer_code": "CANADA_REVENUE_AGENCY",
            "jurisdiction_code": "FED",
            "official_identifier": f"SRC{_suffix()}",
            "title": "Retracted guidance",
            "edition": "withdrawn edition",
            "official_locator": "https://example.invalid/withdrawn",
            "content_fingerprint": fingerprint_bytes(uuid.uuid4().bytes),
            "fingerprint_method": "RAW_BYTES_SHA256",
            "retrieved_at": "2026-03-01T00:00:00+00:00",
            "status": "WITHDRAWN",
        }))
        citation = await registry.cite(version.id, SourceLocator({"page": "3"}))
        report = await KnowledgeAuthoringService(s).validate(
            TaxKnowledgeDraftSpec.read(_draft_payload(
                code, citation.id, citation_ids=[str(citation.id)])))
    assert not report.publishable
    assert str(ValidationCode.SOURCE_WITHDRAWN) in report.error_codes()


@pytest.mark.asyncio
async def test_a_superseded_source_still_carries_authority_and_is_reported():
    """§22 and §72. A newer edition existing does not make older words wrong;
    it makes them older. Reported so a reviewer can decide, never substituted."""
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        registry = TaxSourceRegistry(s)
        identifier = f"SRC{_suffix()}"
        common = {
            "source_type": "CRA_GUIDE",
            "issuer_code": "CANADA_REVENUE_AGENCY",
            "jurisdiction_code": "FED",
            "official_identifier": identifier,
            "title": "Guidance",
            "official_locator": "https://example.invalid/guide",
            "fingerprint_method": "RAW_BYTES_SHA256",
            "retrieved_at": "2026-03-01T00:00:00+00:00",
        }
        first_bytes = uuid.uuid4().bytes
        first = await registry.register(manifest_from_payload({
            **common, "edition": "2024",
            "content_fingerprint": fingerprint_bytes(first_bytes)}))
        await registry.register(manifest_from_payload({
            **common, "edition": "2025",
            "content_fingerprint": fingerprint_bytes(uuid.uuid4().bytes),
            "supersedes_fingerprint": fingerprint_bytes(first_bytes)}))
        citation = await registry.cite(first.id, SourceLocator({"section": "1"}))
        report = await KnowledgeAuthoringService(s).validate(
            TaxKnowledgeDraftSpec.read(_draft_payload(
                code, citation.id, citation_ids=[str(citation.id)])))

    assert report.publishable, report.error_codes()
    assert any(f.code is ValidationCode.SOURCE_SUPERSEDED for f in report.warnings)


# ===========================================================================
# Staging and publication
# ===========================================================================
@pytest.mark.asyncio
async def test_a_governed_publication_carries_its_specification_identity():
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        a, b = await _admins(s)
        version_id, spec, report = await _staged(s, code, await _citation(s))
        await _approved(s, version_id, a, b)
        result = await KnowledgeAuthoringService(s).publish(
            b, [version_id], expected_spec_hashes=[report.spec_hash])

    assert result.published_version_ids == (version_id,)
    assert result.spec_hashes == (spec.spec_identity(),)
    assert result.policy_version == PUBLICATION_POLICY_VERSION

    async with _authoring() as s:
        version = await s.get(TaxRuleVersion, version_id)
        assert version.status == "published"
        publication = await s.scalar(
            select(RulePublication).where(
                RulePublication.tax_rule_version_id == version_id))
        assert publication.channel == GOVERNED_CHANNEL
        assert publication.spec_hash == spec.spec_identity()
        assert publication.pack_hash == result.pack_hash
        assert publication.policy_version == PUBLICATION_POLICY_VERSION


@pytest.mark.asyncio
async def test_a_draft_edited_after_validation_cannot_publish():
    """§71, the race that mattered most. Publication previously accepted any
    passed report targeting the version, so a validated draft could be edited
    and published carrying its predecessor's approval."""
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        a, b = await _admins(s)
        version_id, _, report = await _staged(s, code, await _citation(s))
        await _approved(s, version_id, a, b)

        # An edit that changes what the rule MEANS.
        outcome = await s.scalar(
            select(RuleOutcome).where(RuleOutcome.rule_version_id == version_id))
        outcome.title_template = "Something the reviewer never saw"
        await s.flush()

        with pytest.raises(SpecChangedSinceValidation, match="revalidate"):
            await KnowledgeAuthoringService(s).publish(
                b, [version_id], expected_spec_hashes=[report.spec_hash])

    async with _authoring() as s:
        assert (await s.get(TaxRuleVersion, version_id)).status != "published"


@pytest.mark.asyncio
async def test_the_rebuilt_specification_hashes_to_what_was_authored():
    """The binding only works if a stored draft reproduces the authored spec.
    Hashing the submitted document would prove only that the caller sent the
    same bytes twice."""
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        version_id, spec, _ = await _staged(s, code, await _citation(s))
        rebuilt = await KnowledgeAuthoringService(s).rebuild_spec(version_id)
    assert rebuilt.spec_identity() == spec.spec_identity()


@pytest.mark.asyncio
async def test_publication_refuses_a_draft_whose_report_says_no():
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        a, b = await _admins(s)
        service = KnowledgeAuthoringService(s)
        # No citation: structurally stageable, never publishable.
        spec = TaxKnowledgeDraftSpec.read(
            _draft_payload(code, await _citation(s), citation_ids=[]))
        version_id = await service.stage_draft(spec)
        report = await service.validate(spec)
        await service.record_report(version_id, report)
        await _approved(s, version_id, a, b)

        with pytest.raises(NotPublishable) as caught:
            await service.publish(b, [version_id],
                                  expected_spec_hashes=[report.spec_hash])
    assert str(ValidationCode.SOURCE_REQUIRED) in caught.value.error_codes


@pytest.mark.asyncio
async def test_there_is_no_force_flag_on_the_publication_path():
    """§50. An emergency override is a governance design with its own audit
    trail; a boolean parameter is how one gets built by accident."""
    import inspect

    signature = inspect.signature(KnowledgeAuthoringService.publish)
    for forbidden in ("force", "ignore_errors", "skip_provenance", "override",
                      "allow_warnings", "unsafe"):
        assert forbidden not in signature.parameters
    source = inspect.getsource(KnowledgeAuthoringService)
    assert "ignore_errors" not in source


@pytest.mark.asyncio
async def test_publishing_the_same_approved_draft_twice_is_not_two_versions():
    """§32. The lifecycle refuses published → published, so a retry cannot
    duplicate semantic authority."""
    from app.services.tkms.domain.lifecycle import IllegalTransition

    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        a, b = await _admins(s)
        version_id, _, report = await _staged(s, code, await _citation(s))
        await _approved(s, version_id, a, b)
        service = KnowledgeAuthoringService(s)
        await service.publish(b, [version_id],
                              expected_spec_hashes=[report.spec_hash])
        with pytest.raises(IllegalTransition):
            await service.publish(b, [version_id],
                                  expected_spec_hashes=[report.spec_hash])

    async with _authoring() as s:
        published = list(await s.scalars(
            select(TaxRuleVersion)
            .join(TaxRule, TaxRule.id == TaxRuleVersion.tax_rule_id)
            .where(TaxRule.code == code,
                   TaxRuleVersion.status == "published")))
    assert len(published) == 1


@pytest.mark.asyncio
async def test_revalidating_an_unchanged_draft_reuses_its_verdict():
    """Idempotent per (version, spec_hash), enforced by an index rather than by
    hoping two reports agree."""
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        service = KnowledgeAuthoringService(s)
        spec = TaxKnowledgeDraftSpec.read(
            _draft_payload(code, await _citation(s)))
        version_id = await service.stage_draft(spec)
        first = await service.record_report(version_id, await service.validate(spec))
        second = await service.record_report(version_id, await service.validate(spec))
        assert first.id == second.id

        count = await s.scalar(
            select(func.count()).select_from(ValidationReport)
            .where(ValidationReport.target_version_id == version_id))
    assert count == 1


@pytest.mark.asyncio
async def test_a_legacy_report_can_never_authorize_a_governed_publication():
    """The new columns are NULL on every pre-existing report. NULL is not false
    and not true — the question was never asked — so silence cannot approve."""
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        a, b = await _admins(s)
        version_id, _, report = await _staged(s, code, await _citation(s))
        await _approved(s, version_id, a, b)
        # Blank the governed verdict, leaving a report shaped like a legacy one.
        stored = await s.scalar(
            select(ValidationReport).where(
                ValidationReport.target_version_id == version_id))
        stored.spec_hash = None
        stored.publishable = None
        await s.flush()

        with pytest.raises(NotPublishable):
            await KnowledgeAuthoringService(s).publish(
                b, [version_id], expected_spec_hashes=[report.spec_hash])


# ===========================================================================
# Supersession, immutability, replay
# ===========================================================================
@pytest.mark.asyncio
async def test_a_successor_supersedes_without_rewriting_its_predecessor():
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        a, b = await _admins(s)
        citation_id = await _citation(s)
        first_id, first_spec, first_report = await _staged(s, code, citation_id)
        await _approved(s, first_id, a, b)
        service = KnowledgeAuthoringService(s)
        await service.publish(b, [first_id],
                              expected_spec_hashes=[first_report.spec_hash])
        first_description = (await s.get(TaxRuleVersion, first_id)).description

        second_id, _, second_report = await _staged(
            s, code, citation_id, description="A corrected reading.",
            supersedes_version_id=str(first_id))
        await _approved(s, second_id, a, b)
        result = await service.publish(
            b, [second_id], expected_spec_hashes=[second_report.spec_hash])

    assert result.superseded_version_ids == (first_id,)
    async with _authoring() as s:
        predecessor = await s.get(TaxRuleVersion, first_id)
        successor = await s.get(TaxRuleVersion, second_id)
        assert predecessor.status == "superseded"
        assert predecessor.description == first_description, (
            "a correction publishes a successor; it never rewrites history")
        assert predecessor.superseded_by_version_id == second_id
        assert successor.status == "published"
        assert first_spec.spec_identity() != \
            (await KnowledgeAuthoringService(s).rebuild_spec(
                second_id)).spec_identity()


@pytest.mark.asyncio
async def test_replacing_a_published_version_without_declaring_it_is_refused():
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        a, b = await _admins(s)
        citation_id = await _citation(s)
        first_id, _, first_report = await _staged(s, code, citation_id)
        await _approved(s, first_id, a, b)
        await KnowledgeAuthoringService(s).publish(
            b, [first_id], expected_spec_hashes=[first_report.spec_hash])

        _, _, second_report = await _staged(
            s, code, citation_id, description="Silent replacement.")
    assert not second_report.publishable
    assert str(ValidationCode.SUPERSESSION_REQUIRED) in second_report.error_codes()


@pytest.mark.asyncio
async def test_a_pinned_historical_evaluation_still_resolves_the_old_version():
    """§57. A scenario pinned to R1 keeps evaluating R1 after R2 publishes —
    the pin is the whole authority, and a live status filter would EMPTY it."""
    from app.services.tax_engine.rules_service import RulesEvaluatorService

    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        a, b = await _admins(s)
        citation_id = await _citation(s)
        first_id, _, first_report = await _staged(s, code, citation_id)
        await _approved(s, first_id, a, b)
        service = KnowledgeAuthoringService(s)
        await service.publish(b, [first_id],
                              expected_spec_hashes=[first_report.spec_hash])

        before = await RulesEvaluatorService(s).evaluate(
            TAX_YEAR, {"income.employment": Decimal("90000")},
            pinned_rule_version_ids=[first_id])
        assert [o.title for o in before] == [f"{code} opportunity"]

        second_id, _, second_report = await _staged(
            s, code, citation_id, description="A corrected reading.",
            supersedes_version_id=str(first_id),
            outcomes=[{
                "outcome_type": "recommend", "priority": 1,
                "title_template": "A DIFFERENT recommendation",
                "economic_effect_type": "current_year_tax_reduction",
                "reversibility": "reversible",
                "portfolio_lever_code": "INCREASE_RRSP_DEDUCTION",
                "lever_parameters": {"amount": "action.cost_amount"}}])
        await _approved(s, second_id, a, b)
        await service.publish(b, [second_id],
                              expected_spec_hashes=[second_report.spec_hash])

        after = await RulesEvaluatorService(s).evaluate(
            TAX_YEAR, {"income.employment": Decimal("90000")},
            pinned_rule_version_ids=[first_id])

    assert [o.title for o in after] == [f"{code} opportunity"], (
        "the sealed pin resolved the successor's words")


@pytest.mark.asyncio
async def test_a_draft_is_invisible_to_the_evaluator():
    """The evaluator selects status == 'published'. A staged draft is
    reviewable and unreachable at the same time."""
    from app.services.tax_engine.rules_service import RulesEvaluatorService

    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        version_id, _, _ = await _staged(s, code, await _citation(s))
        opportunities = await RulesEvaluatorService(s).evaluate(
            TAX_YEAR, {"income.employment": Decimal("90000")})
    assert not [o for o in opportunities if o.rule_version_id == version_id]


# ===========================================================================
# Packs — all or nothing
# ===========================================================================
@pytest.mark.asyncio
async def test_a_pack_publishes_as_one_release_or_not_at_all():
    """§75. Nine valid rules out of ten is not a partial success, it is a tax
    year that half exists."""
    good = f"KA_{_suffix()}"
    bad = f"KA_{_suffix()}"
    async with _authoring() as s:
        a, b = await _admins(s)
        citation_id = await _citation(s)
        good_id, _, good_report = await _staged(s, good, citation_id)
        # The second member cites nothing, so the pack cannot publish.
        service = KnowledgeAuthoringService(s)
        bad_spec = TaxKnowledgeDraftSpec.read(
            _draft_payload(bad, citation_id, citation_ids=[]))
        bad_id = await service.stage_draft(bad_spec)
        bad_report = await service.validate(bad_spec)
        await service.record_report(bad_id, bad_report)
        await _approved(s, good_id, a, b)
        await _approved(s, bad_id, a, b)

        with pytest.raises(NotPublishable):
            await service.publish(
                b, [good_id, bad_id],
                expected_spec_hashes=[good_report.spec_hash,
                                      bad_report.spec_hash])

    async with _authoring() as s:
        statuses = {
            row.id: row.status for row in await s.scalars(
                select(TaxRuleVersion).where(
                    TaxRuleVersion.id.in_([good_id, bad_id])))}
    assert statuses == {good_id: "approved", bad_id: "approved"}, (
        "the valid member published anyway")


@pytest.mark.asyncio
async def test_members_of_one_release_share_a_pack_identity():
    first = f"KA_{_suffix()}"
    second = f"KA_{_suffix()}"
    async with _authoring() as s:
        a, b = await _admins(s)
        citation_id = await _citation(s)
        first_id, _, first_report = await _staged(s, first, citation_id)
        second_id, _, second_report = await _staged(s, second, citation_id)
        await _approved(s, first_id, a, b)
        await _approved(s, second_id, a, b)
        result = await KnowledgeAuthoringService(s).publish(
            b, [first_id, second_id],
            expected_spec_hashes=[first_report.spec_hash,
                                  second_report.spec_hash])

    async with _authoring() as s:
        packs = set(await s.scalars(
            select(RulePublication.pack_hash).where(
                RulePublication.tax_rule_version_id.in_([first_id, second_id]))))
    assert packs == {result.pack_hash}


@pytest.mark.asyncio
async def test_a_pack_member_may_depend_on_another_unpublished_member():
    """§47. Forcing A to publish so B could validate would mean publishing
    knowledge in order to check knowledge."""
    first = f"KA_{_suffix()}"
    second = f"KA_{_suffix()}"
    async with _authoring() as s:
        citation_id = await _citation(s)
        service = KnowledgeAuthoringService(s)
        a_spec = TaxKnowledgeDraftSpec.read(_draft_payload(first, citation_id))
        b_spec = TaxKnowledgeDraftSpec.read(_draft_payload(
            second, citation_id,
            dependencies=[{"depends_on_rule_code": first,
                           "dependency_type": "requires"}]))
        alone = await service.validate(b_spec)
        together = await service.validate_pack([a_spec, b_spec])

    assert str(ValidationCode.UNKNOWN_DEPENDENCY_TARGET) in alone.error_codes()
    assert together.publishable, together.error_codes()


@pytest.mark.asyncio
async def test_a_pack_listing_one_rule_twice_is_refused():
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        citation_id = await _citation(s)
        spec = TaxKnowledgeDraftSpec.read(_draft_payload(code, citation_id))
        report = await KnowledgeAuthoringService(s).validate_pack([spec, spec])
    assert str(ValidationCode.PACK_DUPLICATE_OBJECT) in report.error_codes()


# ===========================================================================
# Security
# ===========================================================================
@pytest.mark.asyncio
async def test_the_customer_runtime_role_cannot_author_governed_knowledge():
    """§62. Enforced by DB grants, not by a route check.

    The authoring path writes tables the customer runtime has no INSERT on —
    the source registry (revoked by migration 64) and the example tables
    (revoked here, because `rules` DEFAULT PRIVILEGES would otherwise have
    handed them over). Which one denies first is not the point; that the path
    cannot complete under that identity is.
    """
    import asyncpg

    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        citation_id = await _citation(s)
    spec = TaxKnowledgeDraftSpec.read(_draft_payload(code, citation_id))

    with pytest.raises((asyncpg.InsufficientPrivilegeError, Exception)) as caught:
        async with unit_of_work(actor_type="admin") as s:
            await KnowledgeAuthoringService(s).stage_draft(spec)
    assert "permission denied" in str(caught.value).lower()


@pytest.mark.asyncio
async def test_an_operator_without_publish_permission_is_refused():
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        admin = AdminService(s)
        reviewer = await admin.create_admin(
            f"kauth_{_suffix()}@onyx.io", "adminpass1", ["kb_reviewer"])
        a, b = await _admins(s)
        version_id, _, report = await _staged(s, code, await _citation(s))
        await _approved(s, version_id, a, b)

        with pytest.raises(Forbidden):
            await KnowledgeAuthoringService(s).publish(
                reviewer.id, [version_id],
                expected_spec_hashes=[report.spec_hash])


@pytest.mark.asyncio
async def test_no_customer_route_reaches_the_authoring_pipeline():
    """§41. Internal tooling only."""
    from app.main import app

    paths = set(app.openapi()["paths"])
    for needle in ("authoring", "publishability", "knowledge-draft",
                   "draft-spec", "knowledge_pack"):
        assert not [p for p in paths if needle in p.lower()], needle


@pytest.mark.asyncio
async def test_the_evaluator_never_reads_the_authoring_tables():
    """§39. The pipeline governs what becomes authority; it never becomes a
    second evaluator."""
    import inspect

    from app.services.tax_engine import rules_service

    source = inspect.getsource(rules_service)
    for forbidden in ("tax_kb.authoring", "PublishabilityReport", "spec_hash",
                      "KnowledgeAuthoringService", "pack_hash", "rule_example",
                      "formula_vector", "validation_report"):
        assert forbidden not in source, forbidden


# ===========================================================================
# Manifest
# ===========================================================================
def test_a_manifest_round_trips_into_specifications():
    payload = {
        "manifest_schema_version": "1.0.0",
        "name": "fixture pack",
        "rules": [_draft_payload("MANIFEST_RULE", uuid.uuid4())],
    }
    pack = parse_manifest(payload)
    assert pack.name == "fixture pack"
    assert pack.rule_codes == {"MANIFEST_RULE"}


def test_a_manifest_with_an_unknown_field_is_refused():
    with pytest.raises(SpecError, match="unknown field"):
        parse_manifest({"name": "x", "published_by": "someone"})


def test_a_manifest_never_restates_source_metadata():
    """§43. The Source Registry stays canonical; a manifest carrying an issuer
    or a URL would be a second, quietly diverging record of the law."""
    payload = _draft_payload("MANIFEST_RULE", uuid.uuid4())
    for restated in ("issuer_code", "source_url", "source_title",
                     "official_locator", "section_text"):
        with pytest.raises(SpecError, match="unknown"):
            TaxKnowledgeDraftSpec.read({**payload, restated: "x"})


def test_one_malformed_member_does_not_hide_the_rest():
    """An operator running a dry run over a hundred rules needs every error at
    once, not the first one."""
    good = _draft_payload("GOOD_RULE", uuid.uuid4())
    pack, errors = parse_members({
        "name": "mixed",
        "rules": [good, {**good, "rule_code": "BAD_RULE", "nonsense": 1}]})
    assert pack.rule_codes == {"GOOD_RULE"}
    assert len(errors) == 1
    assert errors[0].code is ValidationCode.UNKNOWN_FIELD


def test_a_manifest_carries_no_executable_logic():
    """§7 and §44. There is no field a runtime could eval."""
    import inspect

    from app.services.tax_kb.authoring import manifest, service, spec, validation

    for module in (manifest, service, spec, validation):
        source = inspect.getsource(module)
        for forbidden in ("eval(", "exec(", "__import__", "compile(",
                          "subprocess", "os.system"):
            assert forbidden not in source, f"{module.__name__} contains {forbidden}"


# ===========================================================================
# Performance
# ===========================================================================
@pytest.mark.asyncio
async def test_batch_validation_does_not_grow_its_query_count_per_rule():
    """§69. The obvious shape — resolve each fact, operator and rule code as
    the checks meet them — is an N+1 that would make a content pack cost
    thousands of round trips."""
    async with _authoring() as s:
        citation_id = await _citation(s)
        service = KnowledgeAuthoringService(s)
        specs = [
            TaxKnowledgeDraftSpec.read(
                _draft_payload(f"KA_{_suffix()}", citation_id))
            for _ in range(20)]

        counts: dict[str, int] = {}
        # `get_bind()` on an AsyncSession hands back the SYNC engine the async
        # one wraps, which is exactly what carries the DBAPI events. Listening
        # on the wrong engine is how a counter reports a passing 0 == 0.
        engine = s.get_bind()
        for label, batch in (("one", specs[:1]), ("twenty", specs)):
            seen = [0]

            def _count(conn, cursor, statement, params, context, many,
                       _seen=seen):
                _seen[0] += 1

            event.listen(engine, "before_cursor_execute", _count)
            try:
                report = await service.validate_pack(batch)
            finally:
                event.remove(engine, "before_cursor_execute", _count)
            counts[label] = seen[0]
            assert report.publishable, report.error_codes()

    assert counts["one"] > 0, "the counter watched an engine nothing ran on"
    # EXACTLY equal, not merely close. The vocabularies load once and provenance
    # resolves once for the whole pack, so pack size does not appear in the
    # statement count at all — measured flat at 1, 10, 100 and 500 rules. A
    # tolerance here would let a per-member read creep back in unnoticed.
    assert counts["twenty"] == counts["one"], counts


# ===========================================================================
# Concurrency
# ===========================================================================
@pytest.mark.asyncio
async def test_two_competing_versions_cannot_both_become_current_authority():
    """§33, the case that matters: two DIFFERENT drafts for the same rule and
    tax year, approved independently and published at the same moment.

    Measured before it was believed. Without a lock BOTH succeeded: neither is a
    unique violation, because publication supersedes whatever is currently
    published, so the second silently retired the first operator's version
    moments after it went live — and every check that would have objected ran
    before the first had committed. `uq_rule_version_published` kept the end
    state coherent and could not make the second publisher notice.

    Publication now locks the parent `tax_rule` row, so the two serialize and
    the loser's revalidation sees the winner's version. It is refused with a
    code an operator can act on rather than replaced without being told.
    """
    import asyncio

    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        a, b = await _admins(s)
        citation_id = await _citation(s)
        first_id, _, first_report = await _staged(
            s, code, citation_id, description="Reading one.")
        second_id, _, second_report = await _staged(
            s, code, citation_id, description="Reading two.")
        await _approved(s, first_id, a, b)
        await _approved(s, second_id, a, b)

    async def _publish(version_id, spec_hash) -> str:
        try:
            async with _authoring() as s:
                await KnowledgeAuthoringService(s).publish(
                    b, [version_id], expected_spec_hashes=[spec_hash])
            return "published"
        except Exception as exc:  # noqa: BLE001 — any refusal is a refusal
            return type(exc).__name__

    outcomes = await asyncio.gather(
        _publish(first_id, first_report.spec_hash),
        _publish(second_id, second_report.spec_hash))
    assert outcomes.count("published") == 1, outcomes
    assert "NotPublishable" in outcomes, (
        f"the loser must be told why, not merely fail: {outcomes}")

    async with _authoring() as s:
        published = list(await s.scalars(
            select(TaxRuleVersion)
            .join(TaxRule, TaxRule.id == TaxRuleVersion.tax_rule_id)
            .where(TaxRule.code == code, TaxRuleVersion.status == "published")))
    assert len(published) == 1, "two versions became current authority at once"


    assert len(published) == 1


@pytest.mark.asyncio
async def test_the_published_uniqueness_is_enforced_by_the_database():
    """Read from pg_indexes rather than asserted from behaviour: a guarantee
    the schema does not actually carry would pass a behavioural test on a
    database that simply had not been raced."""
    async with _authoring() as s:
        definition = await s.scalar(text("""
            SELECT indexdef FROM pg_indexes
            WHERE schemaname = 'tax_kb' AND indexname = 'uq_rule_version_published'
        """))
    assert definition is not None
    assert "UNIQUE" in definition
    assert "tax_rule_id" in definition and "tax_year" in definition
    assert "published" in definition


# ===========================================================================
# Published immutability
# ===========================================================================
@pytest.mark.asyncio
async def test_a_published_versions_children_are_not_rewritten_by_a_correction():
    """§56. A correction publishes a successor. R1's conditions, outcomes and
    citations are exactly what they were."""
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        a, b = await _admins(s)
        citation_id = await _citation(s)
        first_id, _, first_report = await _staged(s, code, citation_id)
        await _approved(s, first_id, a, b)
        service = KnowledgeAuthoringService(s)
        await service.publish(b, [first_id],
                              expected_spec_hashes=[first_report.spec_hash])
        before = (await service.rebuild_spec(first_id)).spec_identity()

        second_id, _, second_report = await _staged(
            s, code, citation_id, description="Corrected.",
            supersedes_version_id=str(first_id))
        await _approved(s, second_id, a, b)
        await service.publish(b, [second_id],
                              expected_spec_hashes=[second_report.spec_hash])
        after = (await service.rebuild_spec(first_id)).spec_identity()

    assert before == after, "publishing a successor rewrote its predecessor"


@pytest.mark.asyncio
async def test_the_customer_runtime_role_cannot_rewrite_a_published_rules_examples():
    """The example tables live in `rules`, whose DEFAULT PRIVILEGES would have
    handed onyx_app_rw write access. Taken back explicitly, and asserted."""
    import asyncpg

    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        version_id, _, _ = await _staged(s, code, await _citation(s))

    with pytest.raises(Exception) as caught:
        async with unit_of_work(actor_type="admin") as s:
            await s.execute(text(
                "UPDATE rules.rule_example SET expect_eligible = false "
                "WHERE rule_version_id = :v"), {"v": str(version_id)})
    assert "permission denied" in str(caught.value).lower()
    assert asyncpg is not None


# ===========================================================================
# Reference data publication
# ===========================================================================
@pytest.mark.asyncio
async def test_reference_data_publishes_with_its_own_provenance():
    """§17. Brackets are read by the engine directly and never pass through a
    rule version, so nothing could be inherited — an uncited table is exactly
    the unexplained constant the entry forbids."""
    from app.database.models import TaxBracket, TaxBracketSet
    from app.services.tax_kb.authoring.spec import ReferenceDataSpec

    async with _authoring() as s:
        _, b = await _admins(s)
        citation_id = await _citation(s, source_type="RATE_TABLE")
        # A (jurisdiction, year) pair no bracket set occupies yet. Chosen by
        # QUERY rather than hardcoded: published reference data accumulates in a
        # shared database, and a fixed pair would pass alone and collide in
        # company.
        free = (await s.execute(text("""
            SELECT j.id, j.code, y.year
            FROM ref.jurisdiction j CROSS JOIN ref.tax_year y
            WHERE NOT EXISTS (
                SELECT 1 FROM tax_kb.tax_bracket_set b
                WHERE b.jurisdiction_id = j.id AND b.tax_year = y.year
                  AND b.kind = 'income_tax')
            ORDER BY y.year, j.code LIMIT 1
        """))).first()
        if free is None:
            pytest.skip("every jurisdiction/year pair already has a bracket set")
        jurisdiction_id, jurisdiction_code, year = free

        table = ReferenceDataSpec.read({
            "kind": "TAX_BRACKET_SET",
            "key": f"{jurisdiction_code}:income_tax",
            "tax_year": year,
            "jurisdiction_code": jurisdiction_code,
            "brackets": [
                {"ordinal": 1, "lower_bound": "0", "upper_bound": "50000",
                 "rate": "0.15"},
                {"ordinal": 2, "lower_bound": "50000", "rate": "0.26"}],
            "citation_ids": [str(citation_id)],
        })
        service = KnowledgeAuthoringService(s)
        report = await service.validate_reference_data(table)
        assert report.publishable, report.error_codes()

        result = await service.publish(
            b, [], expected_spec_hashes=[],
            reference_data=[table],
            expected_reference_hashes=[table.spec_identity()])
        assert result.reference_data_hashes == (table.spec_identity(),)

        published = await s.scalar(
            select(TaxBracketSet).where(
                TaxBracketSet.jurisdiction_id == jurisdiction_id,
                TaxBracketSet.tax_year == year))
        assert published is not None
        brackets = list(await s.scalars(
            select(TaxBracket).where(TaxBracket.bracket_set_id == published.id)
            .order_by(TaxBracket.ordinal)))
        assert [bk.rate for bk in brackets] == [Decimal("0.150000"),
                                                Decimal("0.260000")]
        assert brackets[-1].upper_bound is None

        provenance = await TaxSourceRegistry(s).provenance_for_reference_data(
            tax_bracket_set_id=published.id)
        assert len(provenance) == 1
        assert provenance[0].citation_id == citation_id


@pytest.mark.asyncio
async def test_reference_data_without_a_citation_is_not_publishable():
    from app.services.tax_kb.authoring.spec import ReferenceDataSpec

    async with _authoring() as s:
        table = ReferenceDataSpec.read({
            "kind": "CALC_CONSTANT", "key": f"K_{_suffix()}",
            "tax_year": 2026, "value": "0.03"})
        report = await KnowledgeAuthoringService(s).validate_reference_data(table)
    assert not report.publishable
    assert str(ValidationCode.SOURCE_REQUIRED) in report.error_codes()


# ===========================================================================
# Determinism
# ===========================================================================
@pytest.mark.asyncio
async def test_the_same_draft_validates_to_the_same_report_twice():
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        citation_id = await _citation(s)
        spec = TaxKnowledgeDraftSpec.read(_draft_payload(code, citation_id))
        service = KnowledgeAuthoringService(s)
        first = await service.validate(spec)
        second = await service.validate(spec)
    assert first.as_payload() == second.as_payload()


@pytest.mark.asyncio
async def test_findings_order_by_content_not_by_check_order():
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        citation_id = await _citation(s)
        broken = _draft_payload(code, citation_id, category="nope",
                                jurisdiction_code="NOWHERE", examples=[])
        report = await KnowledgeAuthoringService(s).validate(
            TaxKnowledgeDraftSpec.read(broken))
    assert len(report.errors) > 2
    assert list(report.findings) == sorted(report.findings)


# ===========================================================================
# The mirrored vocabularies must match the constraints they mirror
# ===========================================================================
@pytest.mark.asyncio
async def test_every_mirrored_vocabulary_matches_its_check_constraint():
    """The validators keep Python copies of vocabularies the DDL owns, so they
    can stay pure functions. A mirror nobody checks is how a constraint and its
    validator come to disagree — and the validator is the one users meet."""
    import re

    from app.services.tax_kb.authoring import validation as v

    mirrors = {
        ("rules.rule_outcome", "outcome_type"): v.OUTCOME_TYPES,
        ("rules.rule_condition_group", "logical_op"): v.LOGICAL_OPS,
        ("rules.rule_dependency", "dependency_type"): v.DEPENDENCY_TYPES,
        ("rules.rule_required_document", "necessity"): v.EVIDENCE_NECESSITY,
        ("rules.rule_shared_resource", "pool_scope"): v.POOL_SCOPES,
        ("rules.rule_action", "cost_type"): v.COST_TYPES,
        ("rules.rule_outcome", "economic_effect_type"): v.ECONOMIC_EFFECT_TYPES,
        ("rules.rule_outcome", "reversibility"): v.REVERSIBILITY,
        ("tax_kb.tax_bracket_set", "kind"): v.BRACKET_SET_KINDS,
        ("rules.rule_condition", "value_type"): v.VALUE_TYPES,
    }
    async with _authoring() as s:
        for (table, column), expected in mirrors.items():
            schema, name = table.split(".")
            definitions = list(await s.scalars(text("""
                SELECT pg_get_constraintdef(c.oid)
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                WHERE n.nspname = :schema AND t.relname = :name
                  AND c.contype = 'c'
                  AND pg_get_constraintdef(c.oid) LIKE '%' || :column || '%'
            """), {"schema": schema, "name": name, "column": column}))
            matching = [d for d in definitions if "ANY (ARRAY[" in d or " IN (" in d]
            assert matching, f"no CHECK enumerates {table}.{column}"
            declared = set()
            for definition in matching:
                declared.update(re.findall(r"'([a-zA-Z_]+)'::text", definition))
            assert declared == set(expected), (
                f"{table}.{column}: the database allows {sorted(declared)} but "
                f"the validator mirrors {sorted(expected)}")


# ===========================================================================
# §59 — the pipeline changes how knowledge becomes trusted, not what it means
# ===========================================================================
@pytest.mark.asyncio
async def test_governed_publication_produces_the_same_opportunity_as_direct_rows():
    """The same semantics, published two ways, must evaluate identically.

    One rule is built the way every existing fixture builds one — insert the
    version, insert its children, set status. The other goes through validation,
    four-eyes and the governed publication path. The pipeline governs whether
    knowledge is eligible to become authority; it must not alter tax
    mathematics.
    """
    from app.database.models import Jurisdiction, RuleAction
    from app.services.tax_engine.rules_service import RulesEvaluatorService

    direct_code = f"KA_{_suffix()}"
    governed_code = f"KA_{_suffix()}"
    facts = {"income.employment": Decimal("90000")}

    async with _authoring() as s:
        a, b = await _admins(s)
        citation_id = await _citation(s)

        # ---- the direct path, exactly as an existing fixture would ----
        jurisdiction = await s.scalar(
            select(Jurisdiction).where(Jurisdiction.code == "FED"))
        rule = TaxRule(code=direct_code, name=f"{direct_code} deduction",
                       category="deduction", jurisdiction_id=jurisdiction.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=TAX_YEAR,
            effective_date=date(TAX_YEAR, 1, 1), status="published",
            description="A governed fixture rule.",
            eligibility_basis_codes=["FIXTURE_BASIS"])
        s.add(version)
        await s.flush()
        s.add(RuleOutcome(
            rule_version_id=version.id, outcome_type="recommend", priority=1,
            title_template=f"{direct_code} opportunity",
            economic_effect_type="current_year_tax_reduction",
            reversibility="reversible",
            portfolio_lever_code="INCREASE_RRSP_DEDUCTION",
            lever_parameters={"amount": "action.cost_amount"}))
        s.add(RuleAction(
            rule_version_id=version.id, action_code="CONTRIBUTE",
            description="Contribute", effort_rating=2,
            cost_type="liquidity_commitment", cost_amount=Decimal("8000")))
        await s.flush()
        direct_version_id = version.id

        # ---- the governed path, same semantics ----
        governed_id, _, report = await _staged(s, governed_code, citation_id)
        await _approved(s, governed_id, a, b)
        await KnowledgeAuthoringService(s).publish(
            b, [governed_id], expected_spec_hashes=[report.spec_hash])

        opportunities = {
            o.rule_version_id: o
            for o in await RulesEvaluatorService(s).evaluate(TAX_YEAR, facts)}
        registry = TaxSourceRegistry(s)
        governed_provenance = await registry.provenance_for_rule_version(
            governed_id)
        direct_provenance = await registry.provenance_for_rule_version(
            direct_version_id)

    direct = opportunities[direct_version_id]
    governed = opportunities[governed_id]

    # Everything the IOE consumes, compared field by field. The titles and codes
    # differ because the two rules have different codes; nothing else may.
    assert governed.eligibility_status == direct.eligibility_status
    assert governed.eligibility_basis_codes == direct.eligibility_basis_codes
    assert governed.category == direct.category
    assert governed.jurisdiction == direct.jurisdiction
    assert governed.tax_year == direct.tax_year
    assert governed.priority == direct.priority
    assert governed.economic_effect_type == direct.economic_effect_type
    assert governed.reversibility == direct.reversibility
    assert governed.is_portfolio_evaluable == direct.is_portfolio_evaluable
    assert governed.has_contract_metadata == direct.has_contract_metadata
    assert (governed.portfolio_lever_ref.lever_code
            == direct.portfolio_lever_ref.lever_code)
    assert [(x.action_code, x.cost_type, x.cost_amount)
            for x in governed.required_actions] == \
           [(x.action_code, x.cost_type, x.cost_amount)
            for x in direct.required_actions]
    # The governed one additionally carries governed provenance the direct one
    # has none of. Read from the REGISTRY, not from the opportunity: the source
    # registry entry deliberately left the evaluator unwired from it, and a test
    # there asserts exactly that. `OpportunityContractV2.citations` still comes
    # from the legacy stub columns, which is why both are empty here.
    assert governed.citations == direct.citations == ()
    assert [c.citation_id for c in governed_provenance] == [citation_id]
    assert direct_provenance == ()


@pytest.mark.asyncio
async def test_a_governed_rule_with_two_outcomes_reaches_the_optimizer_intact():
    """The §10 fix, exercised through the pipeline that will author such rules.

    Two outcomes on one version used to collide into a single semantic
    candidate key and fail `UNIQUE (portfolio_id, candidate_id)` deep inside
    persistence. Here they are distinct opportunities with distinct keys.
    """
    from app.services.ioe.normalization.service import (
        OpportunityNormalizationService,
    )
    from app.services.tax_engine.rules_service import RulesEvaluatorService

    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        a, b = await _admins(s)
        citation_id = await _citation(s)
        version_id, _, report = await _staged(s, code, citation_id, outcomes=[
            {"outcome_type": "recommend", "priority": 1,
             "title_template": f"{code} contribute",
             "economic_effect_type": "current_year_tax_reduction",
             "reversibility": "reversible",
             "portfolio_lever_code": "INCREASE_RRSP_DEDUCTION",
             "lever_parameters": {"amount": "action.cost_amount"}},
            {"outcome_type": "apply_deduction", "priority": 2,
             "title_template": f"{code} claim",
             "economic_effect_type": "current_year_tax_reduction",
             "reversibility": "reversible",
             "portfolio_lever_code": "INCREASE_DONATIONS",
             "lever_parameters": {"amount": "action.cost_amount"}}])
        await _approved(s, version_id, a, b)
        await KnowledgeAuthoringService(s).publish(
            b, [version_id], expected_spec_hashes=[report.spec_hash])

        mine = [o for o in await RulesEvaluatorService(s).evaluate(
            TAX_YEAR, {"income.employment": Decimal("90000")})
            if o.rule_version_id == version_id]

    assert len(mine) == 2
    keys = {OpportunityNormalizationService.candidate_key(o) for o in mine}
    assert len(keys) == 2, f"two opportunities collapsed to one identity: {keys}"


@pytest.mark.asyncio
async def test_a_single_outcome_rule_keeps_the_candidate_key_it_always_had():
    """The discriminator is withheld for the single-outcome case ON PURPOSE.
    Every key sealed into a scenario, a counterfactual comparison or a
    historical graph before this entry must still be produced byte for byte."""
    from app.services.ioe.normalization.service import (
        OpportunityNormalizationService,
    )
    from app.services.tax_engine.rules_service import RulesEvaluatorService

    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        a, b = await _admins(s)
        version_id, _, report = await _staged(s, code, await _citation(s))
        await _approved(s, version_id, a, b)
        await KnowledgeAuthoringService(s).publish(
            b, [version_id], expected_spec_hashes=[report.spec_hash])
        mine = [o for o in await RulesEvaluatorService(s).evaluate(
            TAX_YEAR, {"income.employment": Decimal("90000")})
            if o.rule_version_id == version_id]

    assert len(mine) == 1
    assert mine[0].outcome_discriminator is None
    assert OpportunityNormalizationService.candidate_key(mine[0]) == \
        f"{code.lower()}:{version_id}"


# ===========================================================================
# Operator tooling
# ===========================================================================
def test_the_operator_tool_offers_validation_without_publication():
    """§70. An author must be able to validate, read deterministic errors,
    correct the manifest, and only then publish the exact validated spec."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[2] / "backend/scripts/knowledge_pack.py"
    if not path.exists():
        path = Path(__file__).parents[2] / "scripts/knowledge_pack.py"
    spec = importlib.util.spec_from_file_location("knowledge_pack", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    parser_source = path.read_text()
    assert '"validate"' in parser_source
    assert '"publish"' in parser_source
    for forbidden in ("--force", "ignore_errors", "skip_provenance"):
        assert forbidden not in parser_source, forbidden


# ===========================================================================
# The AI boundary and the publication audit record
# ===========================================================================
def test_no_ai_reaches_the_publication_decision():
    """§30. Not `LLM says valid → publish`, and not a confidence threshold
    either. Every publication passes deterministic validators and a governed
    authorization; there is no third way in."""
    import inspect

    from app.services.tax_kb.authoring import (
        codes,
        manifest,
        policy,
        report,
        service,
        spec,
        validation,
    )

    for module in (codes, manifest, policy, report, service, spec, validation):
        source = inspect.getsource(module)
        for forbidden in ("openai", "anthropic", "llm", "LLM", "embedding",
                          "confidence_threshold", "model.predict"):
            assert forbidden not in source, f"{module.__name__} references {forbidden}"


@pytest.mark.asyncio
async def test_the_publication_record_answers_what_when_and_on_what_authority():
    """§63. A high-value administrative action, so what it recorded has to be
    enough to reconstruct the decision — and it holds no customer data."""
    code = f"KA_{_suffix()}"
    async with _authoring() as s:
        a, b = await _admins(s)
        citation_id = await _citation(s)
        first_id, _, first_report = await _staged(s, code, citation_id)
        await _approved(s, first_id, a, b)
        service = KnowledgeAuthoringService(s)
        await service.publish(b, [first_id],
                              expected_spec_hashes=[first_report.spec_hash])

        second_id, second_spec, second_report = await _staged(
            s, code, citation_id, description="Corrected.",
            supersedes_version_id=str(first_id))
        await _approved(s, second_id, a, b)
        await service.publish(b, [second_id],
                              expected_spec_hashes=[second_report.spec_hash])

        record = await s.scalar(
            select(RulePublication).where(
                RulePublication.tax_rule_version_id == second_id))
        successor = await s.get(TaxRuleVersion, second_id)
        predecessor = await s.get(TaxRuleVersion, first_id)
        provenance = await TaxSourceRegistry(s).provenance_for_rule_version(
            second_id)

    # what published, and exactly which specification
    assert record.spec_hash == second_spec.spec_identity()
    # when, and by whom
    assert record.published_at is not None
    assert record.published_by == b
    # under which policy, through which path
    assert record.policy_version == PUBLICATION_POLICY_VERSION
    assert record.channel == GOVERNED_CHANNEL
    # what it superseded, from both directions
    assert successor.supersedes_version_id == first_id
    assert predecessor.superseded_by_version_id == second_id
    # and what authority it carried
    assert [c.citation_id for c in provenance] == [citation_id]
    # no customer data anywhere on the record
    assert not hasattr(record, "user_id")


@pytest.mark.asyncio
async def test_the_authoring_role_model_is_recorded_as_it_actually_is():
    """§29 and §62, measured rather than asserted.

    Read from `information_schema.role_table_grants`, because a grant this suite
    believed in but the database did not carry would certify nothing.

    What the grants say:

    * `onyx_app_rw` — the customer runtime — has NO write on the source registry
      or the example tables. The governed path is closed to it at the database.
    * `onyx_kb_admin` holds knowledge-table writes, and nothing in `tkms` or
      `admin`. So it can author a draft but cannot record a verdict or a
      publication, and NO single existing role can complete the governed path.

    That last line is a limitation, recorded rather than papered over. The
    pipeline is operator tooling that runs with owner authority today. Splitting
    it into an operator role that spans knowledge, reports and governance is a
    role-model change with its own security entry; fabricating one here would be
    the enterprise workflow §29 warns against.
    """
    async with _authoring() as s:
        rows = list(await s.execute(text("""
            SELECT grantee, table_schema, table_name, privilege_type
            FROM information_schema.role_table_grants
            WHERE grantee IN ('onyx_app_rw', 'onyx_kb_admin')
              AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE')
        """)))
    granted = {(g, f"{sch}.{tbl}", p) for g, sch, tbl, p in rows}

    # The customer runtime is shut out of exactly what makes the path governed.
    for table in ("tax_kb.source_citation", "tax_kb.knowledge_citation",
                  "tax_kb.tax_source", "tax_kb.tax_source_version",
                  "rules.rule_example", "rules.formula_vector"):
        for privilege in ("INSERT", "UPDATE", "DELETE"):
            assert ("onyx_app_rw", table, privilege) not in granted, \
                f"onyx_app_rw can {privilege} {table}"

    # The authoring role can write knowledge...
    assert ("onyx_kb_admin", "rules.rule_example", "INSERT") in granted
    assert ("onyx_kb_admin", "tax_kb.knowledge_citation", "INSERT") in granted
    # ...and never rewrite published provenance.
    assert ("onyx_kb_admin", "tax_kb.knowledge_citation", "UPDATE") not in granted
    assert ("onyx_kb_admin", "rules.rule_example", "UPDATE") not in granted

    # ...but holds nothing in the report or governance planes. This is the
    # measured limitation, asserted so it cannot change without being noticed.
    kb_admin_schemas = {t.split(".")[0] for g, t, _ in granted
                        if g == "onyx_kb_admin"}
    assert "tkms" not in kb_admin_schemas
    assert "admin" not in kb_admin_schemas
