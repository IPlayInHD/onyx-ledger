"""KnowledgeAuthoringService — validate without publishing, publish only what
was validated.

Two entry points, and the distinction between them is the whole design:

    validate(...)   reads. It writes nothing, publishes nothing, and never
                    touches customer data. An operator can run it against
                    production as often as they like.

    publish(...)    writes. It refuses unless the exact specification in front
                    of it hashes to the one a stored report found publishable,
                    a different operator approved it, and every citation still
                    resolves to a qualifying source.

There is no third entry point, no `force`, and no `ignore_errors`. An emergency
override is a governance design with its own audit trail; a boolean parameter
is how one gets built by accident.

The service is a GATE, not a second engine. Publication delegates the actual
state transition to the certified `PublicationService`, so supersession, the
freshness events and the four-eyes change-request check keep their single
implementation. What is added here is everything that must be true BEFORE that
transition is allowed to happen.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, DomainError, NotFound
from app.database.models import (
    AccountRegisteredType,
    CalcConstant,
    CalcFormula,
    CalcFormulaInput,
    ConditionOperator,
    ContributionLimit,
    DeadlineType,
    DocumentType,
    FactDefinition,
    FormulaVector,
    Jurisdiction,
    Province,
    RuleAction,
    RuleCategory,
    RuleCondition,
    RuleConditionGroup,
    RuleDeadline,
    RuleDependency,
    RuleExample,
    RuleOutcome,
    RulePublication,
    RuleRequiredDocument,
    RuleSharedResource,
    SourceCitation,
    TaxBracket,
    TaxBracketSet,
    TaxRule,
    TaxRuleVersion,
    TaxYear,
    ValidationReport,
)
from app.database.models import (
    ValidationFinding as ValidationFindingRow,
)
from app.services.admin.service import AdminService
from app.services.tax_kb.authoring import validation as v
from app.services.tax_kb.authoring.codes import (
    Family,
    Readiness,
    Severity,
    ValidationCode,
)
from app.services.tax_kb.authoring.policy import (
    PUBLICATION_POLICY_VERSION,
    assess_source,
)
from app.services.tax_kb.authoring.report import (
    Finding,
    PublishabilityReport,
    ReportBuilder,
    error,
    warning,
)
from app.services.tax_kb.authoring.spec import (
    KNOWLEDGE_SPEC_SCHEMA_VERSION,
    ConditionGroupSpec,
    ReferenceDataKind,
    ReferenceDataSpec,
    SpecError,
    TaxKnowledgeDraftSpec,
    pack_identity,
)
from app.services.tax_kb.sources.registry import ProvenanceUnavailable, TaxSourceRegistry
from app.services.tkms.domain.lifecycle import DRAFT, PUBLISHED, RuleLifecycle

#: The publication channel a version published through this pipeline carries.
GOVERNED_CHANNEL = "governed"


class NotPublishable(DomainError):
    """Publication was refused. Carries the codes, never a bare message."""

    status_code = 422
    error_type = "https://onyx.ledger/errors/knowledge-not-publishable"
    title = "Knowledge Not Publishable"

    def __init__(self, report: PublishabilityReport):
        self.report = report
        self.error_codes = report.error_codes()
        super().__init__(
            "publication refused: " + ", ".join(self.error_codes or ("UNKNOWN",)))


class SpecChangedSinceValidation(Conflict):
    """The draft in front of us is not the draft that was validated."""

    error_type = "https://onyx.ledger/errors/spec-changed-since-validation"
    title = "Specification Changed Since Validation"


@dataclass(frozen=True)
class _Provenance:
    """Citations resolved once for a whole pack.

    `known` is which ids exist at all; `resolutions` is the pinned edition each
    resolves to; `failure` is set when the registry refused to resolve rather
    than substituting a newer edition.
    """

    known: frozenset[uuid.UUID]
    resolutions: dict
    failure: str | None


@dataclass(frozen=True)
class PublicationResult:
    """What one governed publication produced. No customer data, ever."""

    pack_hash: str
    published_version_ids: tuple[uuid.UUID, ...]
    superseded_version_ids: tuple[uuid.UUID, ...]
    spec_hashes: tuple[str, ...]
    reference_data_hashes: tuple[str, ...] = ()
    policy_version: str = PUBLICATION_POLICY_VERSION


class KnowledgeAuthoringService:
    def __init__(self, session: AsyncSession, admin: AdminService | None = None):
        self.s = session
        self.admin = admin or AdminService(session)

    # =======================================================================
    # Context — one bounded set of reads, whatever the pack size
    # =======================================================================
    async def load_context(
        self, *, pack_rule_codes: frozenset[str] = frozenset()
    ) -> v.KnowledgeContext:
        """Load every vocabulary the validators resolve names against.

        A fixed number of statements regardless of how many drafts are being
        validated. The obvious shape — look up each fact, operator and rule code
        as the checks encounter them — is an N+1 that would make validating a
        content pack cost thousands of round trips.
        """
        facts = set(await self.s.scalars(select(FactDefinition.fact_key)))
        jurisdictions = set(await self.s.scalars(select(Jurisdiction.code)))
        provinces = set(await self.s.scalars(select(Province.code)))
        years = set(await self.s.scalars(select(TaxYear.year)))
        categories = set(await self.s.scalars(select(RuleCategory.code)))
        operators = set(await self.s.scalars(select(ConditionOperator.code)))
        documents = set(await self.s.scalars(select(DocumentType.code)))
        registered = set(await self.s.scalars(select(AccountRegisteredType.code)))
        deadlines = set(await self.s.scalars(select(DeadlineType.code)))

        rules = list(await self.s.execute(
            select(TaxRule.code, Jurisdiction.code, TaxRule.category,
                   TaxRule.subcategory, TaxRule.province_code, TaxRule.name)
            .join(Jurisdiction, Jurisdiction.id == TaxRule.jurisdiction_id)))
        rule_jurisdictions = {row[0]: row[1] for row in rules}
        rule_identities = {
            row[0]: (row[1], row[2], row[3], row[4], row[5]) for row in rules}

        published = {
            (code, year): version_id
            for code, year, version_id in await self.s.execute(
                select(TaxRule.code, TaxRuleVersion.tax_year, TaxRuleVersion.id)
                .join(TaxRuleVersion, TaxRuleVersion.tax_rule_id == TaxRule.id)
                .where(TaxRuleVersion.status == PUBLISHED))}

        formulas = {
            code: expression for code, expression in await self.s.execute(
                select(CalcFormula.code, CalcFormula.expression))}

        return v.KnowledgeContext(
            known_facts=frozenset(facts),
            known_jurisdictions=frozenset(jurisdictions),
            known_provinces=frozenset(provinces),
            known_years=frozenset(years),
            known_categories=frozenset(categories),
            known_operators=frozenset(operators),
            known_document_types=frozenset(documents),
            known_registered_types=frozenset(registered),
            known_deadline_codes=frozenset(deadlines),
            known_assumption_codes=await self._assumption_codes(),
            known_rule_codes=frozenset(rule_jurisdictions) | pack_rule_codes,
            rule_jurisdictions=rule_jurisdictions,
            rule_identities=rule_identities,
            published_reference_data=await self._published_reference_data(),
            published_formulas=formulas,
            published_rule_versions=published,
        )

    async def _assumption_codes(self) -> frozenset[str]:
        from app.database.models import Assumption

        return frozenset(await self.s.scalars(select(Assumption.code).distinct()))

    async def _published_reference_data(self) -> frozenset[v.PublishedReferenceData]:
        """Every reference-data object already published, by semantic key.

        Keyed the way the tables are unique — jurisdiction+kind, registered
        type, constant code — rather than by row id, so a manifest resolves
        identically in every environment.
        """
        out: set[v.PublishedReferenceData] = set()
        for jurisdiction, kind, year in await self.s.execute(
            select(Jurisdiction.code, TaxBracketSet.kind, TaxBracketSet.tax_year)
            .join(Jurisdiction, Jurisdiction.id == TaxBracketSet.jurisdiction_id)
        ):
            out.add(v.PublishedReferenceData(
                ReferenceDataKind.TAX_BRACKET_SET, f"{jurisdiction}:{kind}", year))
        for registered_type, year in await self.s.execute(
            select(ContributionLimit.registered_type, ContributionLimit.tax_year)
        ):
            out.add(v.PublishedReferenceData(
                ReferenceDataKind.CONTRIBUTION_LIMIT, registered_type, year))
        # Only the constant table carries periods; brackets and limits remain
        # whole-year objects, so they resolve to unbounded records above.
        for code, year, start, end in await self.s.execute(
            select(CalcConstant.code, CalcConstant.tax_year,
                   CalcConstant.effective_from, CalcConstant.effective_to)
        ):
            out.add(v.PublishedReferenceData(
                ReferenceDataKind.CALC_CONSTANT, code, year, start, end))
        return frozenset(out)

    # =======================================================================
    # Validation — reads only
    # =======================================================================
    async def validate(
        self,
        spec: TaxKnowledgeDraftSpec,
        *,
        ctx: v.KnowledgeContext | None = None,
        pack_codes: frozenset[str] = frozenset(),
        provenance: _Provenance | None = None,
    ) -> PublishabilityReport:
        """Assess one draft. Writes nothing — §25.

        Deliberately not a transaction: there is nothing to commit. A validation
        that had to be rolled back would be one that had already done something.
        """
        context = ctx or await self.load_context(pack_rule_codes=pack_codes)
        builder = ReportBuilder()

        builder.extend(v.validate_structure(spec, context))
        builder.extend(v.validate_actions(spec))
        builder.extend(v.validate_conditions(spec, context))
        builder.extend(v.validate_dependencies(spec, context))
        builder.extend(v.warn_unpublished_dependency(spec, pack_codes))
        builder.extend(v.validate_outcomes(spec, context))
        builder.extend(v.validate_formula(spec, context))
        builder.extend(v.validate_reference_data_refs(spec, context))
        builder.extend(v.validate_evidence(spec, context))
        builder.extend(v.validate_deadlines(spec, context))
        builder.extend(v.validate_assumptions(spec, context))
        builder.extend(v.validate_examples(spec, context))
        builder.extend(v.run_examples(spec))
        builder.extend(v.validate_versioning(spec, context))
        builder.extend(await self._validate_provenance(spec, provenance))

        ref = spec.rule_code
        builder.resolve(Family.STRUCTURE, ref, declared=True)
        builder.resolve(Family.PROVENANCE, ref, declared=bool(spec.citation_ids))
        builder.resolve(Family.CONDITIONS, ref,
                        declared=spec.condition_group is not None)
        builder.resolve(Family.DEPENDENCIES, ref, declared=bool(spec.dependencies))
        builder.resolve(Family.OUTCOMES, ref, declared=bool(spec.outcomes))
        builder.resolve(Family.FORMULA, ref, declared=True,
                        applicable=spec.formula is not None)
        builder.resolve(Family.REFERENCE_DATA, ref, declared=True,
                        applicable=bool(spec.reference_data_dependencies))
        builder.resolve(Family.EVIDENCE, ref, declared=True,
                        applicable=bool(spec.evidence_requirements))
        builder.resolve(Family.DEADLINES, ref, declared=True,
                        applicable=bool(spec.deadlines))
        builder.resolve(Family.ASSUMPTIONS, ref, declared=True,
                        applicable=bool(spec.assumption_codes))
        builder.resolve(Family.EXAMPLES, ref, declared=bool(spec.examples))
        builder.resolve(Family.VERSIONING, ref, declared=True)
        # Publication authority is real and is checked — by `publish()`, which
        # requires `tkms.publish` and an approved change request from someone
        # other than the submitter. It is simply not validation's to answer, and
        # NOT_APPLICABLE would assert the question does not arise.
        builder.assess(Family.AUTHORITY, Readiness.DEFERRED_TO_PUBLICATION)

        return builder.build(
            spec_hash=spec.spec_identity(),
            spec_schema_version=spec.schema_version,
            policy_version=PUBLICATION_POLICY_VERSION)

    async def validate_reference_data(
        self, spec: ReferenceDataSpec, *, ctx: v.KnowledgeContext | None = None,
        provenance: _Provenance | None = None
    ) -> PublishabilityReport:
        context = ctx or await self.load_context()
        builder = ReportBuilder()
        builder.extend(v.validate_reference_data(spec, context))
        builder.extend(await self._validate_provenance_ids(
            spec.key, spec.citation_ids, required=True, provenance=provenance))
        builder.resolve(Family.REFERENCE_DATA, spec.key, declared=True)
        builder.resolve(Family.PROVENANCE, spec.key,
                        declared=bool(spec.citation_ids))
        return builder.build(spec_hash=spec.spec_identity(),
                             spec_schema_version=spec.schema_version,
                             policy_version=PUBLICATION_POLICY_VERSION)

    async def validate_pack(
        self,
        specs: Sequence[TaxKnowledgeDraftSpec],
        reference_data: Sequence[ReferenceDataSpec] = (),
    ) -> PublishabilityReport:
        """Validate a whole candidate release, with cross-member resolution.

        A rule may depend on another member of the same unpublished pack — §47.
        Forcing dependency A to publish so draft B could validate would mean
        publishing knowledge specifically in order to check knowledge, which is
        the opposite of a dry run.
        """
        codes = frozenset(s.rule_code for s in specs)
        ctx = await self.load_context(pack_rule_codes=codes)
        builder = ReportBuilder()

        seen: set[tuple[str, int]] = set()
        for spec in specs:
            key = (spec.rule_code, spec.tax_year)
            if key in seen:
                builder.add(error(
                    spec.rule_code, Family.VERSIONING,
                    ValidationCode.PACK_DUPLICATE_OBJECT,
                    f"{spec.rule_code} for {spec.tax_year} appears twice in one "
                    "pack; which of the two would publish is undefined"))
            seen.add(key)
        # Overlap rather than key equality: one pack may legitimately carry
        # four quarters of one prescribed rate, and refusing them as duplicates
        # would refuse the case the period columns exist for. What is still
        # refused is two objects in one pack covering the same days.
        seen_ref: list[ReferenceDataSpec] = []
        for rd in reference_data:
            clash = next(
                (p for p in seen_ref
                 if (p.kind, p.key, p.tax_year) == (rd.kind, rd.key, rd.tax_year)
                 and v.periods_overlap(rd.effective_from, rd.effective_to,
                                       p.effective_from, p.effective_to)),
                None)
            if clash is not None:
                builder.add(error(
                    rd.key, Family.REFERENCE_DATA,
                    ValidationCode.PACK_DUPLICATE_OBJECT,
                    f"{rd.kind} {rd.key!r} for {rd.tax_year} appears twice over "
                    "overlapping effective periods; which of the two would "
                    "publish is undefined"))
            seen_ref.append(rd)

        builder.extend(v.detect_dependency_cycles(specs))
        for family in Family:
            builder.assess(family, Readiness.NOT_APPLICABLE)

        cited: list[uuid.UUID] = []
        for spec in specs:
            cited.extend(spec.citation_ids)
        for rd in reference_data:
            cited.extend(rd.citation_ids)
        provenance = await self.resolve_provenance(cited)

        members: list[PublishabilityReport] = []
        for rd in reference_data:
            members.append(await self.validate_reference_data(
                rd, ctx=ctx, provenance=provenance))
        # Reference data published in the same pack satisfies a rule's pins, so
        # the rules are validated against a context that already knows about it.
        rule_ctx = replace(ctx, published_reference_data=(
            ctx.published_reference_data
            | frozenset(v.PublishedReferenceData(
                rd.kind, rd.key, rd.tax_year, rd.effective_from, rd.effective_to)
                for rd in reference_data)))
        for spec in specs:
            members.append(await self.validate(
                spec, ctx=rule_ctx, pack_codes=codes, provenance=provenance))

        return builder.build(
            spec_hash=None, spec_schema_version=KNOWLEDGE_SPEC_SCHEMA_VERSION,
            policy_version=PUBLICATION_POLICY_VERSION,
            members=tuple(members))

    # =======================================================================
    # Provenance
    # =======================================================================
    async def _validate_provenance(
        self, spec: TaxKnowledgeDraftSpec, provenance: _Provenance | None = None
    ) -> list[Finding]:
        return await self._validate_provenance_ids(
            spec.rule_code, spec.citation_ids, required=True,
            provenance=provenance)

    async def resolve_provenance(
        self, citation_ids: Sequence[uuid.UUID]
    ) -> _Provenance:
        """Resolve every citation a PACK names, once.

        Per-member resolution costs six statements each, which is an N+1 across
        a content pack: twenty rules paid a hundred and twenty round trips for
        provenance that overlaps almost completely, because a pack cites the
        same handful of sources throughout.
        """
        unique = list(dict.fromkeys(citation_ids))
        if not unique:
            return _Provenance(known=frozenset(), resolutions={}, failure=None)
        rows = set(await self.s.scalars(
            select(SourceCitation.id).where(SourceCitation.id.in_(unique))))
        try:
            resolutions = await TaxSourceRegistry(self.s).resolve_citations(
                [cid for cid in unique if cid in rows])
        except ProvenanceUnavailable as exc:
            return _Provenance(known=frozenset(rows), resolutions={},
                               failure=str(exc))
        return _Provenance(known=frozenset(rows), resolutions=resolutions,
                           failure=None)

    async def _validate_provenance_ids(
        self, ref: str, citation_ids: Sequence[uuid.UUID], *, required: bool,
        provenance: _Provenance | None = None
    ) -> list[Finding]:
        """Every citation must resolve to a registered, qualifying edition.

        Resolution is against the PINNED edition. A newer edition existing is
        reported and changes nothing, because a rule was authored against
        particular words — §22 and §72 both turn on keeping *superseded* and
        *broken* apart.
        """
        out: list[Finding] = []
        if not citation_ids:
            if required:
                out.append(error(
                    ref, Family.PROVENANCE, ValidationCode.SOURCE_REQUIRED,
                    "production knowledge publishes with at least one governed "
                    "citation; a free-form URL is not provenance"))
            return out

        if len(set(citation_ids)) != len(citation_ids):
            out.append(error(ref, Family.PROVENANCE,
                             ValidationCode.DUPLICATE_CITATION,
                             "the same citation is listed twice"))

        resolved_set = provenance or await self.resolve_provenance(citation_ids)
        missing = sorted(
            str(cid) for cid in citation_ids if cid not in resolved_set.known)
        if missing:
            out.append(error(
                ref, Family.PROVENANCE, ValidationCode.SOURCE_CITATION_UNKNOWN,
                f"citation(s) {missing} are not registered in the source "
                "registry"))
        if resolved_set.failure is not None:
            # The pack-wide batch failed, which does not mean THIS object is the
            # one that broke. Re-resolving its own citations puts the finding on
            # the member that caused it — a dry run over a hundred rules that
            # blames all hundred for one bad citation is not usable.
            if provenance is not None:
                resolved_set = await self.resolve_provenance(citation_ids)
            if resolved_set.failure is not None:
                out.append(error(
                    ref, Family.PROVENANCE,
                    ValidationCode.SOURCE_INTEGRITY_UNAVAILABLE,
                    f"pinned provenance could not be resolved: "
                    f"{resolved_set.failure}. The registry does not substitute "
                    "a newer edition, so this is an integrity failure rather "
                    "than a stale citation"))
                return out

        qualifying = 0
        for cid in citation_ids:
            resolved = resolved_set.resolutions.get(cid)
            if resolved is None:
                continue
            verdict = assess_source(resolved.source_type, resolved.version_status,
                                    resolved.superseded)
            if verdict.withdrawn:
                out.append(error(
                    ref, Family.PROVENANCE, ValidationCode.SOURCE_WITHDRAWN,
                    f"citation {cid} names a WITHDRAWN edition of "
                    f"{resolved.official_identifier}; the publisher retracted "
                    "it, so it never carried the authority it appears to"))
                continue
            if verdict.superseded:
                out.append(warning(
                    ref, Family.PROVENANCE, ValidationCode.SOURCE_SUPERSEDED,
                    f"citation {cid} names an edition of "
                    f"{resolved.official_identifier} that a later edition has "
                    "replaced. The pinned edition is used unchanged; review "
                    "whether this knowledge should cite the newer words"))
            if verdict.qualifies:
                qualifying += 1
            else:
                out.append(warning(
                    ref, Family.PROVENANCE,
                    ValidationCode.SOURCE_NOT_PRODUCTION_QUALIFYING,
                    f"citation {cid} is {resolved.source_type}, which policy "
                    f"{PUBLICATION_POLICY_VERSION} does not accept as "
                    "production authority on its own"))

        if not qualifying and not missing:
            out.append(error(
                ref, Family.PROVENANCE,
                ValidationCode.SOURCE_NOT_PRODUCTION_QUALIFYING,
                "no citation carries production authority. Commentary about the "
                "law may be recorded and cited alongside it; it may not be the "
                "only thing standing behind a figure a user acts on"))
        return out

    # =======================================================================
    # Draft staging
    # =======================================================================
    async def stage_draft(self, spec: TaxKnowledgeDraftSpec) -> uuid.UUID:
        """Materialize a validated specification as a DRAFT rule version.

        Written whole, in the caller's transaction. Nothing here is reachable by
        the evaluator: it selects `status == 'published'`, and a draft is not
        that. The write is what makes the draft reviewable — a specification
        living only in a request body is one nobody else can look at.
        """
        rule = await self.s.scalar(
            select(TaxRule).where(TaxRule.code == spec.rule_code))
        if rule is None:
            jurisdiction = await self.s.scalar(
                select(Jurisdiction).where(
                    Jurisdiction.code == spec.jurisdiction_code))
            if jurisdiction is None:
                raise NotFound(f"jurisdiction {spec.jurisdiction_code!r} not found")
            rule = TaxRule(
                code=spec.rule_code, name=spec.name, category=spec.category,
                subcategory=spec.subcategory, jurisdiction_id=jurisdiction.id,
                province_code=spec.province_code)
            self.s.add(rule)
            await self.s.flush()

        formula_id = await self._upsert_formula(spec)
        version = TaxRuleVersion(
            tax_rule_id=rule.id,
            tax_year=spec.tax_year,
            effective_date=spec.effective_from,
            expiry_date=spec.effective_to,
            status=DRAFT,
            description=spec.description,
            formula_id=formula_id,
            eligibility_basis_codes=list(spec.eligibility_basis_codes),
            supersedes_version_id=spec.supersedes_version_id,
        )
        self.s.add(version)
        await self.s.flush()

        await self._write_children(spec, version.id, formula_id)
        await self._attach_citations(spec, version.id, formula_id)
        return version.id

    async def _upsert_formula(self, spec: TaxKnowledgeDraftSpec) -> uuid.UUID | None:
        """Reuse an identical published formula; never rewrite a different one.

        `rules.calc_formula` is unique on `code` and carries no version column,
        so a formula IS its code. Re-authoring the same expression is idempotent;
        authoring a different one under the same code is refused by validation
        before this runs.
        """
        if spec.formula is None:
            return None
        existing = await self.s.scalar(
            select(CalcFormula).where(CalcFormula.code == spec.formula.code))
        if existing is not None:
            return existing.id
        formula = CalcFormula(
            code=spec.formula.code, expression=spec.formula.expression,
            expression_lang=spec.formula.expression_lang,
            output_unit=spec.formula.output_unit,
            description=spec.formula.description)
        self.s.add(formula)
        await self.s.flush()
        for inp in spec.formula.inputs:
            self.s.add(CalcFormulaInput(
                formula_id=formula.id, param_name=inp.param_name,
                fact_key=inp.fact_key, literal_value=inp.literal_value))
        for vector in spec.formula.vectors:
            self.s.add(FormulaVector(
                formula_id=formula.id, name=vector.name,
                inputs=dict(vector.inputs), expected=vector.expected))
        await self.s.flush()
        return formula.id

    async def _write_children(self, spec: TaxKnowledgeDraftSpec,
                              version_id: uuid.UUID,
                              formula_id: uuid.UUID | None) -> None:
        if spec.condition_group is not None:
            await self._write_group(spec.condition_group, version_id, None)
        for outcome in spec.outcomes:
            self.s.add(RuleOutcome(
                rule_version_id=version_id,
                outcome_type=outcome.outcome_type,
                impact_formula_id=(
                    formula_id if outcome.formula_code is not None else None),
                priority=outcome.priority,
                title_template=outcome.title_template,
                mechanism=outcome.mechanism,
                where_template=outcome.where_template,
                how_template=outcome.how_template,
                why_template=outcome.why_template,
                economic_effect_type=outcome.economic_effect_type,
                reversibility=outcome.reversibility,
                portfolio_lever_code=outcome.portfolio_lever_code,
                lever_parameters=outcome.lever_parameters))
        for action in spec.actions:
            self.s.add(RuleAction(
                rule_version_id=version_id, action_code=action.action_code,
                description=action.description, effort_rating=action.effort_rating,
                cost_type=action.cost_type, cost_amount=action.cost_amount,
                deadline_code=action.deadline_code, sort_order=action.sort_order))
        for evidence in spec.evidence_requirements:
            self.s.add(RuleRequiredDocument(
                rule_version_id=version_id,
                document_type_code=evidence.document_type_code,
                necessity=evidence.necessity, note=evidence.note))
        for dep in spec.dependencies:
            self.s.add(RuleDependency(
                rule_version_id=version_id,
                depends_on_rule_code=dep.depends_on_rule_code,
                dependency_type=dep.dependency_type, note=dep.note))
        for deadline in spec.deadlines:
            self.s.add(RuleDeadline(
                rule_version_id=version_id, deadline_code=deadline.deadline_code,
                deadline_date=deadline.deadline_date,
                description=deadline.description, is_hard=deadline.is_hard,
                jurisdiction_code=deadline.jurisdiction_code))
        for resource in spec.shared_resources:
            self.s.add(RuleSharedResource(
                rule_version_id=version_id, resource_code=resource.resource_code,
                pool_scope=resource.pool_scope))
        for i, example in enumerate(spec.examples):
            self.s.add(RuleExample(
                rule_version_id=version_id, name=example.name,
                facts=dict(example.facts),
                expect_eligible=example.expect_eligible,
                expected_outcome_types=list(example.expected_outcome_types),
                sort_order=i))
        await self.s.flush()

    async def _write_group(self, group: ConditionGroupSpec, version_id: uuid.UUID,
                           parent_id: uuid.UUID | None, sort: int = 0) -> None:
        row = RuleConditionGroup(
            rule_version_id=version_id, parent_group_id=parent_id,
            logical_op=group.logical_op, sort_order=sort)
        self.s.add(row)
        await self.s.flush()
        for i, cond in enumerate(group.conditions):
            self.s.add(RuleCondition(
                group_id=row.id, fact_key=cond.fact_key, operator=cond.operator,
                value_type=cond.value_type, value_number=cond.value_number,
                value_number_high=cond.value_number_high,
                value_text=cond.value_text, value_boolean=cond.value_boolean,
                value_date=cond.value_date, sort_order=i))
        for i, child in enumerate(group.groups):
            await self._write_group(child, version_id, row.id, i)
        await self.s.flush()

    async def _attach_citations(self, spec: TaxKnowledgeDraftSpec,
                                version_id: uuid.UUID,
                                formula_id: uuid.UUID | None) -> None:
        """Link the draft to its governed provenance.

        The rule version gets every citation. A formula authored here gets them
        too, because a formula outlives the rule that introduced it: "the rule
        that uses it" stops identifying anything the moment two rules do.
        """
        from app.database.models import KnowledgeCitation

        registry = TaxSourceRegistry(self.s)
        existing_formula_links = set(await self.s.scalars(
            select(KnowledgeCitation.citation_id).where(
                KnowledgeCitation.formula_id == formula_id))) if formula_id else set()
        for citation_id in spec.citation_ids:
            await registry.attach(citation_id, rule_version_id=version_id)
            # A formula outlives the rule that introduced it, so the second rule
            # citing the same section must not add a duplicate link.
            if formula_id is not None and citation_id not in existing_formula_links:
                await registry.attach(citation_id, formula_id=formula_id)
                existing_formula_links.add(citation_id)

    # =======================================================================
    # Persisted reports
    # =======================================================================
    async def record_report(self, version_id: uuid.UUID,
                            report: PublishabilityReport) -> ValidationReport:
        """Store a governed verdict, idempotently per (version, spec_hash).

        Revalidating an unchanged draft returns the same row rather than
        accumulating reports that could later disagree with each other. The
        uniqueness is enforced by an index, so two concurrent validations of the
        same draft cannot both insert.
        """
        existing = await self.s.scalar(
            select(ValidationReport).where(
                ValidationReport.target_version_id == version_id,
                ValidationReport.spec_hash == report.spec_hash))
        if existing is not None:
            return existing

        row = ValidationReport(
            target_version_id=version_id,
            status=("failed" if report.errors
                    else "warnings" if report.warnings else "passed"),
            spec_hash=report.spec_hash,
            spec_schema_version=report.spec_schema_version,
            policy_version=report.publication_policy_version,
            publishable=report.publishable,
            readiness={str(name): str(state) for name, state in report.readiness},
            error_codes=list(report.error_codes()))
        try:
            # A SAVEPOINT, not a transaction rollback. The caller may have
            # staged the very draft this report is about; discarding their work
            # to recover from a lost race would be a cure worse than the race.
            async with self.s.begin_nested():
                self.s.add(row)
                await self.s.flush()
        except IntegrityError:
            # Lost the race to a concurrent validation of the identical draft.
            # Both computed the same verdict — the digest says so — so adopting
            # the winner's row is not a compromise.
            found = await self.s.scalar(
                select(ValidationReport).where(
                    ValidationReport.target_version_id == version_id,
                    ValidationReport.spec_hash == report.spec_hash))
            if found is None:  # pragma: no cover — the index guarantees one
                raise
            return found
        for finding in report.findings:
            self.s.add(ValidationFindingRow(
                report_id=row.id, rule_ref=finding.object_ref,
                severity=("error" if finding.severity is Severity.ERROR
                          else "warning"),
                code=str(finding.code), message=finding.detail))
        await self.s.flush()
        return row

    # =======================================================================
    # Publication
    # =======================================================================
    async def publish(
        self,
        publisher_id: uuid.UUID,
        version_ids: Sequence[uuid.UUID],
        *,
        expected_spec_hashes: Sequence[str],
        reference_data: Sequence[ReferenceDataSpec] = (),
        expected_reference_hashes: Sequence[str] = (),
    ) -> PublicationResult:
        """Publish a coherent set, all or nothing.

        `expected_spec_hashes` is the binding §71 requires. The caller states
        which specification it believes it is publishing; the service rebuilds
        that specification from the draft's CURRENT rows, hashes it, and refuses
        on any mismatch. A draft edited between validation and publication
        therefore cannot publish, and neither can a stale approval.

        Reference data publishes in the SAME transaction as the rules that pin
        it — §48's yearly update is brackets, credits, formulas and eligibility
        together, and a half-applied one is a tax year that computes wrong for
        everybody rather than a release that failed.

        Atomic across the whole set — §75. Nine valid rules out of a ten-rule
        release is not a partial success, it is a tax year that half exists.
        """
        from app.services.tkms.publication.service import PublicationService

        if len(version_ids) != len(expected_spec_hashes):
            raise Conflict(
                "every version being published must state the specification "
                "hash it was validated as")
        if len(reference_data) != len(expected_reference_hashes):
            raise Conflict(
                "every reference-data object being published must state the "
                "specification hash it was validated as")
        for rd, expected in zip(reference_data, expected_reference_hashes,
                                strict=True):
            if rd.spec_identity() != expected:
                raise SpecChangedSinceValidation(
                    f"{ValidationCode.SPEC_CHANGED_SINCE_VALIDATION}: "
                    f"{rd.kind} {rd.key!r} does not hash to what was validated")
        await self.admin.require_permission(publisher_id, "tkms.publish")

        await self._lock_rules(version_ids)

        specs: list[TaxKnowledgeDraftSpec] = []
        for version_id, expected in zip(version_ids, expected_spec_hashes,
                                        strict=True):
            # Checked FIRST so a retry of an already-published version is
            # refused for the reason it actually failed, rather than by a
            # downstream check noticing the rule now has a published version.
            current = await self.s.get(TaxRuleVersion, version_id)
            if current is None:
                raise NotFound("Rule version not found")
            RuleLifecycle.assert_transition(current.status, PUBLISHED)
            spec = await self.rebuild_spec(version_id)
            actual = spec.spec_identity()
            if actual != expected:
                raise SpecChangedSinceValidation(
                    f"{ValidationCode.SPEC_CHANGED_SINCE_VALIDATION}: draft "
                    f"{version_id} now hashes to {actual[:12]}… but was "
                    f"validated as {expected[:12]}…; revalidate before "
                    "publishing")
            specs.append(spec)

        # Revalidated here rather than trusted from storage. Between validation
        # and publication a cited source can be superseded, a reference-data
        # dependency can be published or a competing version can appear — §72
        # and §73 — and a stored verdict cannot know about any of it.
        pack = await self.validate_pack(specs, reference_data)
        if not pack.publishable:
            raise NotPublishable(pack)
        for version_id, expected in zip(version_ids, expected_spec_hashes,
                                        strict=True):
            stored = await self.s.scalar(
                select(ValidationReport).where(
                    ValidationReport.target_version_id == version_id,
                    ValidationReport.spec_hash == expected,
                    ValidationReport.publishable.is_(True)))
            if stored is None:
                raise NotPublishable(pack)

        pack_hash = pack_identity(
            [s.spec_identity() for s in specs]
            + [rd.spec_identity() for rd in reference_data])
        # Reference data first: a rule pinning a bracket table must not be
        # readable for a moment before the table it reads exists.
        for rd in reference_data:
            await self._publish_reference_data(rd)

        publisher = PublicationService(self.s, self.admin)
        published: list[uuid.UUID] = []
        superseded: list[uuid.UUID] = []
        for version_id, spec in zip(version_ids, specs, strict=True):
            before = await self._currently_published(spec.rule_code, spec.tax_year)
            version = await publisher.publish(publisher_id, version_id)
            published.append(version.id)
            if before is not None and before != version.id:
                superseded.append(before)
            await self._stamp_publication(version.id, spec.spec_identity(),
                                          pack_hash)
        return PublicationResult(
            pack_hash=pack_hash,
            published_version_ids=tuple(published),
            superseded_version_ids=tuple(superseded),
            spec_hashes=tuple(s.spec_identity() for s in specs),
            reference_data_hashes=tuple(
                rd.spec_identity() for rd in reference_data))

    async def _publish_reference_data(self, spec: ReferenceDataSpec) -> None:
        """Write one reference-data object and its provenance.

        INSERT only. The tables are unique on their semantic key and carry no
        version column, so an UPDATE path would rewrite what sealed snapshots
        were computed from; validation refuses a key that already exists, and
        the uniqueness makes that refusal enforced rather than merely intended.

        These values are read by the engine directly and never pass through a
        rule version, so their citations are attached explicitly — nothing could
        be inherited, which is exactly how they would otherwise become the
        unexplained constants §17 forbids.
        """
        registry = TaxSourceRegistry(self.s)
        if spec.kind == ReferenceDataKind.TAX_BRACKET_SET:
            jurisdiction_code, _, kind = spec.key.partition(":")
            jurisdiction = await self.s.scalar(
                select(Jurisdiction).where(Jurisdiction.code == jurisdiction_code))
            if jurisdiction is None:
                raise NotFound(f"jurisdiction {jurisdiction_code!r} not found")
            bracket_set = TaxBracketSet(
                jurisdiction_id=jurisdiction.id, tax_year=spec.tax_year,
                kind=kind or "income_tax")
            self.s.add(bracket_set)
            await self.s.flush()
            for bracket in spec.brackets:
                self.s.add(TaxBracket(
                    bracket_set_id=bracket_set.id, ordinal=bracket.ordinal,
                    lower_bound=bracket.lower_bound,
                    upper_bound=bracket.upper_bound, rate=bracket.rate))
            subject = {"tax_bracket_set_id": bracket_set.id}
        elif spec.kind == ReferenceDataKind.CONTRIBUTION_LIMIT:
            limit = ContributionLimit(
                registered_type=spec.key, tax_year=spec.tax_year,
                annual_limit=spec.annual_limit,
                lifetime_limit=spec.lifetime_limit,
                percent_of_income=spec.percent_of_income,
                allows_carryforward=spec.allows_carryforward)
            self.s.add(limit)
            await self.s.flush()
            subject = {"contribution_limit_id": limit.id}
        else:
            constant = CalcConstant(
                code=spec.key, tax_year=spec.tax_year, value=spec.value,
                unit=spec.unit, effective_from=spec.effective_from,
                effective_to=spec.effective_to)
            self.s.add(constant)
            await self.s.flush()
            subject = {"calc_constant_id": constant.id}
        await self.s.flush()
        for citation_id in spec.citation_ids:
            await registry.attach(citation_id, **subject)

    async def _lock_rules(self, version_ids: Sequence[uuid.UUID]) -> None:
        """Serialize publications of the same rule, at the database.

        Without this, two operators publishing DIFFERENT versions of one rule
        and tax year both succeed. Neither is a unique violation, because
        publication supersedes whatever is currently published — so the second
        transaction silently retires the first operator's version moments after
        it went live, and every check that would have objected ran before the
        first had committed.

        `uq_rule_version_published` still guarantees the end state is coherent;
        what it cannot do is make the SECOND publisher notice that the world
        changed under it. Locking the parent `tax_rule` row makes the two
        serialize, so the loser's revalidation sees the winner's version and
        refuses with SUPERSESSION_REQUIRED — a refusal an operator can act on
        instead of a silent replacement nobody was told about.

        Locked in a deterministic order so two packs touching the same rules
        cannot deadlock against each other.
        """
        if not version_ids:
            return
        rule_ids = sorted(
            str(rule_id) for rule_id in await self.s.scalars(
                select(TaxRuleVersion.tax_rule_id).where(
                    TaxRuleVersion.id.in_(list(version_ids)))))
        for rule_id in rule_ids:
            await self.s.execute(
                select(TaxRule.id).where(TaxRule.id == rule_id).with_for_update())

    async def _currently_published(self, rule_code: str,
                                   tax_year: int) -> uuid.UUID | None:
        found: uuid.UUID | None = await self.s.scalar(
            select(TaxRuleVersion.id)
            .join(TaxRule, TaxRule.id == TaxRuleVersion.tax_rule_id)
            .where(TaxRule.code == rule_code,
                   TaxRuleVersion.tax_year == tax_year,
                   TaxRuleVersion.status == PUBLISHED))
        return found

    async def _stamp_publication(self, version_id: uuid.UUID, spec_hash: str,
                                 pack_hash: str) -> None:
        row = await self.s.scalar(
            select(RulePublication)
            .where(RulePublication.tax_rule_version_id == version_id)
            .order_by(RulePublication.published_at.desc()))
        if row is None:  # pragma: no cover — publish() just wrote one
            return
        row.spec_hash = spec_hash
        row.pack_hash = pack_hash
        row.policy_version = PUBLICATION_POLICY_VERSION
        row.channel = GOVERNED_CHANNEL
        await self.s.flush()

    # =======================================================================
    # Rebuilding a specification from stored rows
    # =======================================================================
    async def rebuild_spec(self, version_id: uuid.UUID) -> TaxKnowledgeDraftSpec:
        """Reconstruct the specification a stored draft currently expresses.

        This is what makes the hash binding real. Hashing the submitted document
        would prove only that the caller sent the same bytes twice; hashing what
        the DATABASE holds proves the knowledge that will publish is the
        knowledge that was validated.
        """
        from app.services.tax_kb.authoring import spec as sp

        version = await self.s.get(TaxRuleVersion, version_id)
        if version is None:
            raise NotFound("Rule version not found")
        rule = await self.s.get(TaxRule, version.tax_rule_id)
        if rule is None:  # pragma: no cover — FK guarantees it
            raise NotFound("Rule not found")
        jurisdiction = await self.s.get(Jurisdiction, rule.jurisdiction_id)

        groups = list(await self.s.scalars(
            select(RuleConditionGroup)
            .where(RuleConditionGroup.rule_version_id == version_id)
            .order_by(RuleConditionGroup.sort_order, RuleConditionGroup.id)))
        conditions = list(await self.s.scalars(
            select(RuleCondition)
            .where(RuleCondition.group_id.in_([g.id for g in groups]))
            .order_by(RuleCondition.sort_order, RuleCondition.id))) if groups else []

        outcomes = list(await self.s.scalars(
            select(RuleOutcome).where(RuleOutcome.rule_version_id == version_id)
            .order_by(RuleOutcome.priority, RuleOutcome.id)))
        actions = list(await self.s.scalars(
            select(RuleAction).where(RuleAction.rule_version_id == version_id)
            .order_by(RuleAction.sort_order, RuleAction.id)))
        documents = list(await self.s.scalars(
            select(RuleRequiredDocument)
            .where(RuleRequiredDocument.rule_version_id == version_id)
            .order_by(RuleRequiredDocument.document_type_code)))
        dependencies = list(await self.s.scalars(
            select(RuleDependency)
            .where(RuleDependency.rule_version_id == version_id)
            .order_by(RuleDependency.depends_on_rule_code)))
        deadlines = list(await self.s.scalars(
            select(RuleDeadline).where(RuleDeadline.rule_version_id == version_id)
            .order_by(RuleDeadline.deadline_code)))
        resources = list(await self.s.scalars(
            select(RuleSharedResource)
            .where(RuleSharedResource.rule_version_id == version_id)
            .order_by(RuleSharedResource.resource_code)))
        formula = (await self.s.get(CalcFormula, version.formula_id)
                   if version.formula_id else None)
        formula_inputs = list(await self.s.scalars(
            select(CalcFormulaInput)
            .where(CalcFormulaInput.formula_id == version.formula_id)
            .order_by(CalcFormulaInput.param_name))) if formula else []
        vectors = list(await self.s.scalars(
            select(FormulaVector)
            .where(FormulaVector.formula_id == version.formula_id)
            .order_by(FormulaVector.name))) if formula else []
        examples = list(await self.s.scalars(
            select(RuleExample).where(RuleExample.rule_version_id == version_id)
            .order_by(RuleExample.sort_order, RuleExample.name)))
        citation_ids = await self._citation_ids(version_id)

        formula_code_by_id: dict[uuid.UUID | None, str] = (
            {formula.id: formula.code} if formula else {})
        return TaxKnowledgeDraftSpec(
            rule_code=rule.code,
            name=rule.name,
            category=rule.category,
            subcategory=rule.subcategory,
            jurisdiction_code=jurisdiction.code if jurisdiction else "",
            province_code=rule.province_code,
            tax_year=version.tax_year,
            effective_from=version.effective_date,
            effective_to=version.expiry_date,
            description=version.description,
            eligibility_basis_codes=tuple(version.eligibility_basis_codes or ()),
            condition_group=_rebuild_group(groups, conditions),
            outcomes=tuple(
                sp.OutcomeSpec(
                    outcome_type=o.outcome_type, priority=o.priority,
                    formula_code=formula_code_by_id.get(o.impact_formula_id),
                    title_template=o.title_template, mechanism=o.mechanism,
                    where_template=o.where_template, how_template=o.how_template,
                    why_template=o.why_template,
                    economic_effect_type=o.economic_effect_type,
                    reversibility=o.reversibility,
                    portfolio_lever_code=o.portfolio_lever_code,
                    lever_parameters=o.lever_parameters)
                for o in outcomes),
            actions=tuple(
                sp.ActionSpec(
                    action_code=a.action_code, description=a.description,
                    effort_rating=a.effort_rating, cost_type=a.cost_type,
                    cost_amount=a.cost_amount, deadline_code=a.deadline_code,
                    sort_order=a.sort_order)
                for a in actions),
            evidence_requirements=tuple(
                sp.EvidenceSpec(document_type_code=d.document_type_code,
                                necessity=d.necessity, note=d.note)
                for d in documents),
            dependencies=tuple(
                sp.DependencySpec(depends_on_rule_code=d.depends_on_rule_code,
                                  dependency_type=d.dependency_type, note=d.note)
                for d in dependencies),
            deadlines=tuple(
                sp.DeadlineSpec(
                    deadline_code=d.deadline_code, deadline_date=d.deadline_date,
                    description=d.description, is_hard=d.is_hard,
                    jurisdiction_code=d.jurisdiction_code)
                for d in deadlines),
            shared_resources=tuple(
                sp.SharedResourceSpec(resource_code=r.resource_code,
                                      pool_scope=r.pool_scope)
                for r in resources),
            formula=(
                sp.FormulaSpec(
                    code=formula.code, expression=formula.expression,
                    expression_lang=formula.expression_lang,
                    output_unit=formula.output_unit,
                    description=formula.description,
                    inputs=tuple(
                        sp.FormulaInputSpec(
                            param_name=i.param_name, fact_key=i.fact_key,
                            literal_value=i.literal_value)
                        for i in formula_inputs),
                    vectors=tuple(
                        sp.FormulaVector(name=vec.name, inputs=dict(vec.inputs),
                                         expected=Decimal(vec.expected))
                        for vec in vectors))
                if formula else None),
            examples=tuple(
                sp.ExampleSpec(
                    name=ex.name, facts=dict(ex.facts),
                    expect_eligible=ex.expect_eligible,
                    expected_outcome_types=tuple(ex.expected_outcome_types or ()))
                for ex in examples),
            citation_ids=citation_ids,
            supersedes_version_id=version.supersedes_version_id,
        )

    async def _citation_ids(self, version_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
        from app.database.models import KnowledgeCitation

        return tuple(sorted(await self.s.scalars(
            select(KnowledgeCitation.citation_id).where(
                KnowledgeCitation.rule_version_id == version_id))))


def _rebuild_group(groups: Sequence[Any],
                   conditions: Sequence[Any]) -> ConditionGroupSpec | None:
    """Rebuild the nested condition tree from its flat rows."""
    from app.services.tax_kb.authoring import spec as sp

    if not groups:
        return None
    by_parent: dict[object, list] = {}
    for group in groups:
        by_parent.setdefault(group.parent_group_id, []).append(group)
    conditions_by_group: dict[object, list] = {}
    for cond in conditions:
        conditions_by_group.setdefault(cond.group_id, []).append(cond)

    def build(row: Any) -> sp.ConditionGroupSpec:
        return sp.ConditionGroupSpec(
            logical_op=row.logical_op,
            conditions=tuple(
                sp.ConditionSpec(
                    fact_key=cnd.fact_key, operator=cnd.operator,
                    value_type=cnd.value_type,
                    value_number=_dec(cnd.value_number),
                    value_number_high=_dec(cnd.value_number_high),
                    value_text=cnd.value_text, value_boolean=cnd.value_boolean,
                    value_date=cnd.value_date)
                for cnd in conditions_by_group.get(row.id, ())),
            groups=tuple(build(child) for child in by_parent.get(row.id, ())))

    roots = by_parent.get(None, [])
    if not roots:  # pragma: no cover — every tree has a root
        return None
    return build(roots[0])


def _dec(value: Any) -> Decimal | None:
    """Normalize a numeric column back to the scale the spec hashed at.

    `numeric` round-trips as `Decimal('8000.000000')` where the spec wrote
    `Decimal('8000')`; the canonical serializers quantize both to one string, so
    normalizing here keeps the rebuilt spec hashing identically to the authored
    one rather than depending on a column's stored scale.
    """
    if value is None:
        return None
    return Decimal(value) if not isinstance(value, Decimal) else value


__all__ = [
    "GOVERNED_CHANNEL",
    "KnowledgeAuthoringService",
    "NotPublishable",
    "PublicationResult",
    "SpecChangedSinceValidation",
    "SpecError",
]
