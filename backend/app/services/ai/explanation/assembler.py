"""ExplanationInputV1 assembler — read-only composition of governed services.

One rule shapes this module: **it assembles, it never decides.** Every figure,
verdict, citation, deadline and assumption in the assembled input was produced
by the governed authority that owns it and is copied verbatim through that
authority's own read surface. The assembler adds exactly three kinds of
derived data, all deterministic and none of them tax facts: the value table
(formatting variants of supplied numbers), the subject binding (sealed
identities for validator use), and the display context (trust labels on
strings that already exist).

Tenant isolation is the owning services': every loader either goes through a
service that raises the non-enumerating `NotFound`, or applies the same
`user_id` ownership check those services apply.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound
from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisLineItem,
    AnalysisRun,
    LegislationReference,
    ReconciliationCheck,
    RuleAction,
    RuleDeadline,
    RuleRequiredDocument,
    TaxRule,
    TaxRuleVersion,
)
from app.schemas import AnalysisOut, LineItemOut
from app.schemas.assurance import TAX_ASSURANCE_SCHEMA_VERSION, TaxAssuranceOut
from app.schemas.before_you_act import BEFORE_YOU_ACT_SCHEMA_VERSION
from app.schemas.decision_journal import DECISION_JOURNAL_SCHEMA_VERSION
from app.schemas.explanation import (
    CitationRecordV1,
    DisplayContextV1,
    ExplanationInputV1,
    OpportunityActionV1,
    OpportunityContractSection,
    OpportunityDeadlineV1,
    OpportunityDocumentV1,
    OpportunitySection,
    ReconciliationRowV1,
    SubjectBinding,
    TaxPositionSection,
    ValueRecordV1,
    VersionManifestV1,
)
from app.schemas.ioe import FreshnessOut, IntegrityOut, MonetaryAmount
from app.schemas.retention import RETENTION_CHANGES_SCHEMA_VERSION
from app.services.ai.explanation import values as vf
from app.services.ai.explanation.untrusted import UNTRUSTED_SCENARIO_LABEL_KEY, UNTRUSTED_SCENARIO_NOTE_KEY
from app.services.ioe import (
    assurance_presentation,
    before_you_act_presentation,
    journal_presentation,
    presentation,
    retention_presentation,
)
from app.services.ioe.domain.integrity import integrity_warning, user_visible_state
from app.services.ioe.journal import DecisionJournalService
from app.services.ioe.read_repository import IoeReadRepository
from app.services.ioe.retention.service import RetentionService
from app.services.ioe.scenario.before_you_act import BeforeYouActService
from app.services.ioe.scenario.query_service import ScenarioQueryService
from app.services.state_graph.assurance import derive_assurance_map
from app.services.state_graph.service import TaxStateGraphService

#: The assurance/lifecycle user-visible integrity states, mapped back to the
#: storage-status vocabulary `IntegrityOut.integrity_status` uses. A pure
#: vocabulary projection of the same fact — no state is upgraded or invented.
_INTEGRITY_STATE_TO_STATUS = {
    "verified": "verified",
    "non_reproducible": "mismatch",
    "unavailable": "unavailable",
    "legacy_unverifiable": "unavailable",
    "not_checked": "not_checked",
}


class ExplanationInputAssembler:
    def __init__(self, session: AsyncSession, user_id: uuid.UUID):
        self.s = session
        self.user_id = user_id

    async def assemble(
        self,
        explanation_type: str,
        *,
        subject_id: str | None,
        tax_year: int | None,
        as_of: date | None = None,
    ) -> ExplanationInputV1:
        evaluation_date = as_of or datetime.now(tz=UTC).date()
        if explanation_type == "TAX_POSITION":
            return await self._tax_position(self._uuid(subject_id), evaluation_date)
        if explanation_type == "OPPORTUNITY":
            if tax_year is None or not subject_id:
                raise NotFound("Opportunity not found")
            return await self._opportunity(subject_id, tax_year, evaluation_date)
        if explanation_type == "PORTFOLIO":
            return await self._portfolio(self._uuid(subject_id), evaluation_date)
        if explanation_type == "SCENARIO":
            return await self._scenario(self._uuid(subject_id), evaluation_date)
        if explanation_type == "COMPARISON":
            return await self._comparison(self._uuid(subject_id), evaluation_date)
        if explanation_type == "EVIDENCE_READINESS":
            return await self._evidence(self._uuid(subject_id), evaluation_date)
        if explanation_type == "WHAT_CHANGED":
            if tax_year is None:
                raise NotFound("Nothing to explain for that subject")
            return await self._what_changed(tax_year, evaluation_date)
        raise NotFound("Nothing to explain for that subject")

    @staticmethod
    def _uuid(subject_id: str | None) -> uuid.UUID:
        """Parse, refusing non-enumeratingly: a malformed id and a missing row
        must be indistinguishable."""
        try:
            return uuid.UUID(subject_id or "")
        except ValueError as exc:
            raise NotFound("Nothing to explain for that subject") from exc

    # ------------------------------------------------------------ sections --
    async def _tax_position(
        self, analysis_id: uuid.UUID, as_of: date
    ) -> ExplanationInputV1:
        run = await self.s.get(AnalysisRun, analysis_id)
        if run is None or run.user_id != self.user_id:
            raise NotFound("Analysis not found")
        line_items = list(await self.s.scalars(
            select(AnalysisLineItem)
            .where(AnalysisLineItem.analysis_id == analysis_id)
            .order_by(AnalysisLineItem.sort_order, AnalysisLineItem.created_at)
        ))
        recon = list(await self.s.scalars(
            select(ReconciliationCheck)
            .where(ReconciliationCheck.analysis_id == analysis_id)
            .order_by(ReconciliationCheck.created_at)
        ))
        snapshot = await self.s.get(AnalysisInputSnapshot, analysis_id)

        section = TaxPositionSection(
            analysis=AnalysisOut.model_validate(run),
            line_items=[LineItemOut.model_validate(li, from_attributes=True)
                        for li in line_items],
            reconciliation=[ReconciliationRowV1.model_validate(r) for r in recon],
        )
        table: dict[str, ValueRecordV1] = {}
        self._add_year(table, run.tax_year)
        self._add_money(table, "tax_position.estimated_tax", run.estimated_tax)
        self._add_money(table, "tax_position.taxable_income", run.taxable_income)
        self._add_money(table, "tax_position.total_income", run.total_income)
        self._add_rate(table, "tax_position.marginal_rate", run.marginal_rate)
        self._add_rate(table, "tax_position.average_rate", run.average_rate)
        for idx, li in enumerate(line_items):
            self._add_money(table, f"tax_position.line_item.{idx}", li.amount)
            self._absorb_supplied_text(
                table, f"tax_position.line_item_label.{idx}", li.label)
        for idx, check in enumerate(recon):
            self._absorb_supplied_text(
                table, f"tax_position.reconciliation.{idx}",
                f"{check.label} {check.detail or ''}")

        return ExplanationInputV1(
            explanation_type="TAX_POSITION",
            tax_year=run.tax_year,
            jurisdiction=run.province_code,
            analysis_id=run.id,
            as_of=as_of.isoformat(),
            subject_binding=SubjectBinding(
                subject_type="analysis",
                subject_id=str(analysis_id),
                snapshot_hash=snapshot.snapshot_hash if snapshot else None,
            ),
            version_manifest=VersionManifestV1(
                engine_version=run.engine_version,
                # G3, stated rather than papered over: a bare analysis does not
                # record which reference-data version it resolved. Claiming
                # today's runtime version was pinned would be false.
                reference_data_binding="not_recorded_on_artifact",
            ),
            tax_position=section,
            value_table=table,
            freshness=FreshnessOut(freshness_status="unknown"),
            integrity=presentation.integrity_of(run),
        )

    async def _opportunity(
        self, source_id: str, tax_year: int, as_of: date
    ) -> ExplanationInputV1:
        graph = await TaxStateGraphService(self.s, self.user_id).build(tax_year=tax_year)
        assurance: TaxAssuranceOut = assurance_presentation.assurance_detail(
            derive_assurance_map(graph, as_of=as_of)
        )
        matches = [o for o in assurance.opportunities if o.source_id == source_id]
        if not matches:
            by_code = [o for o in assurance.opportunities
                       if o.opportunity_code == source_id]
            if len(by_code) == 1:
                matches = by_code
        if len(matches) != 1:
            raise NotFound("Opportunity not found")
        item = matches[0]

        contract, citations = await self._published_rule_contract(
            item.opportunity_code, tax_year
        )
        table: dict[str, ValueRecordV1] = {}
        self._add_year(table, tax_year)
        self._add_text_number(table, "opportunity.standalone_potential",
                              item.standalone_potential, kind="money")
        self._add_text_number(table, "opportunity.incremental_portfolio_benefit",
                              item.incremental_portfolio_benefit, kind="money")
        self._add_support(table, "opportunity.support",
                          item.support.display_support_score)
        if item.deadline is not None:
            self._add_date_string(table, "opportunity.deadline_date",
                                  item.deadline.deadline_date)
            self._add_count(table, "opportunity.deadline_days_remaining",
                            item.deadline.days_remaining)
        if contract is not None:
            for idx, action in enumerate(contract.actions):
                if action.cost_amount is not None:
                    self._add_text_number(
                        table, f"opportunity.action.{idx}.cost_amount",
                        action.cost_amount, kind="money",
                        field_scope=["required_cash_or_resource", "summary",
                                     "estimated_effect_explanation",
                                     "constraints_and_exclusions"],
                    )
            for idx, dl in enumerate(contract.deadlines):
                if dl.deadline_date:
                    self._add_date_string(
                        table, f"opportunity.contract_deadline.{idx}", dl.deadline_date
                    )
            self._absorb_supplied_text(
                table, "opportunity.rule_description", contract.rule_description)
            self._absorb_supplied_text(
                table, "opportunity.authored_explanation",
                contract.authored_explanation)
            for idx, action in enumerate(contract.actions):
                self._absorb_supplied_text(
                    table, f"opportunity.action_text.{idx}", action.description)
            for idx, dl in enumerate(contract.deadlines):
                self._absorb_supplied_text(
                    table, f"opportunity.deadline_text.{idx}", dl.description)
            for idx, doc in enumerate(contract.documents):
                self._absorb_supplied_text(
                    table, f"opportunity.document_note.{idx}", doc.note)
        for idx, citation in enumerate(citations):
            self._absorb_supplied_text(table, f"citation.{idx}", citation.citation_text)

        integrity_status = _INTEGRITY_STATE_TO_STATUS.get(item.integrity, "not_checked")
        return ExplanationInputV1(
            explanation_type="OPPORTUNITY",
            tax_year=tax_year,
            jurisdiction=None,
            analysis_id=None,
            as_of=assurance.as_of,
            subject_binding=SubjectBinding(
                subject_type="opportunity",
                subject_id=item.source_id,
                graph_hash=assurance.graph_hash,
            ),
            version_manifest=VersionManifestV1(
                product_contract_version=TAX_ASSURANCE_SCHEMA_VERSION,
            ),
            opportunity=OpportunitySection(standing=item, contract=contract),
            citations=citations,
            value_table=table,
            freshness=FreshnessOut(
                freshness_status=item.freshness
                if item.freshness in ("unknown", "current", "stale", "superseded")
                else "unknown",
                stale_reason_code=(item.stale_reason_codes[0]
                                   if item.stale_reason_codes else None),
            ),
            integrity=IntegrityOut(
                integrity_status=integrity_status,
                integrity_state=item.integrity,
                integrity_reason_code=item.integrity_reason_code or "NONE",
                integrity_warning=integrity_warning(
                    integrity_status, item.integrity_reason_code or "NONE"
                ),
            ),
        )

    async def _published_rule_contract(
        self, opportunity_code: str, tax_year: int
    ) -> tuple[OpportunityContractSection | None, list[CitationRecordV1]]:
        """Rule-authored contract data for the opportunity's published rule.

        This reads the RULES LAYER'S OWN authored rows (actions, documents,
        deadlines, legislation reference) for the published version — the same
        rows `RulesEvaluatorService` emits as `OpportunityContractV2`. No
        eligibility logic runs here; the standing came from the assurance
        surface and is never re-decided.
        """
        rule = await self.s.scalar(select(TaxRule).where(
            TaxRule.code == opportunity_code.upper()))
        if rule is None:
            rule = await self.s.scalar(select(TaxRule).where(
                TaxRule.code == opportunity_code))
        if rule is None:
            return None, []
        version = await self.s.scalar(
            select(TaxRuleVersion)
            .where(
                TaxRuleVersion.tax_rule_id == rule.id,
                TaxRuleVersion.tax_year == tax_year,
                TaxRuleVersion.status == "published",
            )
            .order_by(TaxRuleVersion.published_at.desc())
            .limit(1)
        )
        if version is None:
            return None, []

        actions = [
            OpportunityActionV1(
                action_code=a.action_code,
                description=a.description,
                effort_rating=a.effort_rating,
                cost_type=a.cost_type,
                cost_amount=vf_money_or_none(a.cost_amount),
                deadline_code=a.deadline_code,
            )
            for a in await self.s.scalars(
                select(RuleAction).where(RuleAction.rule_version_id == version.id)
                .order_by(RuleAction.sort_order, RuleAction.created_at)
            )
        ]
        documents = [
            OpportunityDocumentV1(
                document_type_code=d.document_type_code,
                necessity=d.necessity,
                note=d.note,
            )
            for d in await self.s.scalars(
                select(RuleRequiredDocument).where(
                    RuleRequiredDocument.rule_version_id == version.id)
            )
        ]
        deadlines = [
            OpportunityDeadlineV1(
                deadline_code=dl.deadline_code,
                deadline_date=dl.deadline_date.isoformat() if dl.deadline_date else None,
                description=dl.description,
                is_hard=dl.is_hard,
            )
            for dl in await self.s.scalars(
                select(RuleDeadline).where(RuleDeadline.rule_version_id == version.id)
            )
        ]
        citations: list[CitationRecordV1] = []
        if version.legislation_reference_id is not None:
            ref = await self.s.get(LegislationReference, version.legislation_reference_id)
            if ref is not None:
                citations.append(CitationRecordV1(
                    citation_id=str(ref.id),
                    citation_text=ref.citation,
                    title=ref.title,
                    source_url=ref.url or version.source_url,
                ))
        section = OpportunityContractSection(
            rule_description=version.description,
            authored_explanation=version.ai_explanation,
            actions=actions,
            documents=documents,
            deadlines=deadlines,
            eligibility_basis_codes=list(version.eligibility_basis_codes or []),
        )
        return section, citations

    async def _portfolio(
        self, run_id: uuid.UUID, as_of: date
    ) -> ExplanationInputV1:
        repo = IoeReadRepository(self.s, self.user_id)
        run = await repo.get_run(run_id)
        portfolio = await repo.portfolio_for_run(run_id)
        if portfolio is None:
            raise NotFound("Portfolio not found for this run")
        members = list(await repo.portfolio_members(portfolio.id))
        exclusions = list(await repo.portfolio_exclusions(portfolio.id))
        out = presentation.portfolio_detail(portfolio, members, exclusions, run.tax_year)

        table: dict[str, ValueRecordV1] = {}
        self._add_year(table, run.tax_year)
        self._add_monetary(table, "portfolio.total_benefit", out.portfolio_total_benefit)
        self._add_monetary(table, "portfolio.objective_value_baseline",
                           out.objective_value_baseline)
        self._add_monetary(table, "portfolio.objective_value_final",
                           out.objective_value_final)
        commitment_scope = ["required_cash_or_resource", "constraints_and_exclusions"]
        for key, amount, scope in (
            ("portfolio.total_tax_reduction", out.total_tax_reduction, None),
            ("portfolio.total_refund_impact", out.total_refund_impact, None),
            ("portfolio.total_refundable_benefit", out.total_refundable_benefit, None),
            ("portfolio.total_deferral_amount", out.total_deferral_amount, None),
            ("portfolio.total_liquidity_commitment",
             out.total_liquidity_commitment, commitment_scope),
            ("portfolio.total_asset_transfer", out.total_asset_transfer,
             commitment_scope),
            ("portfolio.total_nonrecoverable_expenditure",
             out.total_nonrecoverable_expenditure, commitment_scope),
            ("portfolio.total_implementation_cost", out.total_implementation_cost,
             commitment_scope),
        ):
            self._add_monetary(table, key, amount, field_scope=scope)
        for idx, member in enumerate(out.members):
            self._add_monetary(table, f"portfolio.member.{idx}.incremental_benefit",
                               member.incremental_benefit)
        self._add_count(table, "portfolio.member_count", len(out.members))
        self._add_count(table, "portfolio.exclusion_count", len(out.exclusions))

        manifest = run.version_manifest or {}
        return ExplanationInputV1(
            explanation_type="PORTFOLIO",
            tax_year=run.tax_year,
            jurisdiction=None,
            analysis_id=run.analysis_id,
            as_of=as_of.isoformat(),
            subject_binding=SubjectBinding(
                subject_type="optimization_run",
                subject_id=str(run_id),
                spec_hash=run.optimization_spec_hash,
                result_hash=run.optimization_result_hash,
            ),
            version_manifest=VersionManifestV1(
                # Echoed from the run's SEALED manifest — absent keys stay None.
                engine_version=manifest.get("engine_version"),
                reference_data_version=manifest.get("reference_data_version"),
                reference_data_binding="sealed_in_spec_hash",
            ),
            portfolio=out,
            value_table=table,
            freshness=FreshnessOut(
                freshness_status=run.freshness_status or "unknown",
                stale_reason_code=(run.stale_reason_codes[0]
                                   if run.stale_reason_codes else None),
                freshness_evaluated_at=run.evaluated_at,
            ),
            integrity=presentation.integrity_of(run),
        )

    async def _scenario(
        self, scenario_id: uuid.UUID, as_of: date
    ) -> ExplanationInputV1:
        out = await ScenarioQueryService(self.user_id).detail(scenario_id)
        table: dict[str, ValueRecordV1] = {}
        if out.tax_year is not None:
            self._add_year(table, out.tax_year)
        self._add_monetary(table, "scenario.baseline_tax", out.baseline_tax)
        self._add_monetary(table, "scenario.scenario_tax", out.scenario_tax)
        self._add_monetary(table, "scenario.tax_delta", out.tax_delta)
        self._add_monetary(table, "scenario.objective_delta", out.objective_delta)
        if out.support is not None:
            self._add_support(table, "scenario.support",
                              out.support.display_support_score)
        for assumption in out.assumptions:
            if assumption.value_number is not None:
                self._add_money(
                    table,
                    f"assumption.{assumption.assumption_code}.value_number",
                    assumption.value_number,
                )
        for idx, lever in enumerate(out.levers):
            raw_amount = lever.parameters.get("amount")
            if raw_amount is not None:
                self._add_text_number(table, f"scenario.lever.{idx}.amount",
                                      str(raw_amount), kind="money")
        for idx, change in enumerate(out.applied_changes):
            self._add_text_value(table, f"scenario.applied.{idx}.old",
                                 change.old_value)
            self._add_text_value(table, f"scenario.applied.{idx}.new",
                                 change.new_value)

        untrusted: dict[str, str] = {}
        if out.label:
            untrusted[UNTRUSTED_SCENARIO_LABEL_KEY] = out.label
        if out.note:
            untrusted[UNTRUSTED_SCENARIO_NOTE_KEY] = out.note

        return ExplanationInputV1(
            explanation_type="SCENARIO",
            tax_year=out.tax_year or 0,
            jurisdiction=out.jurisdiction,
            analysis_id=out.base_analysis_id,
            as_of=as_of.isoformat(),
            subject_binding=SubjectBinding(
                subject_type="scenario",
                subject_id=str(scenario_id),
                spec_hash=out.scenario_spec_hash,
                result_hash=out.scenario_result_hash,
            ),
            version_manifest=VersionManifestV1(
                result_schema_version=out.result_schema_version,
                objective_code=out.objective_code,
                objective_version=out.objective_version,
                reference_data_binding="sealed_in_spec_hash",
            ),
            scenario=out,
            assumptions=list(out.assumptions),
            value_table=table,
            freshness=out.freshness,
            integrity=out.integrity,
            display_context=DisplayContextV1(untrusted=untrusted),
        )

    async def _comparison(
        self, scenario_id: uuid.UUID, as_of: date
    ) -> ExplanationInputV1:
        scenario = await ScenarioQueryService(self.user_id).detail(scenario_id)
        loaded = await BeforeYouActService(self.s, self.user_id).comparison_for(
            scenario_id
        )
        out = before_you_act_presentation.comparison_detail(
            loaded, include_unchanged=False
        )
        table: dict[str, ValueRecordV1] = {}
        if out.scenario.tax_year is not None:
            self._add_year(table, out.scenario.tax_year)
        for assumption in scenario.assumptions:
            if assumption.value_number is not None:
                self._add_money(
                    table,
                    f"assumption.{assumption.assumption_code}.value_number",
                    assumption.value_number,
                )
        families = (
            ("tax_state", out.tax_state_changes),
            ("opportunity", out.opportunity_changes),
            ("evidence", out.evidence_changes),
            ("deadline", out.deadline_changes),
            ("fact", out.fact_changes),
            ("assumption", out.assumption_changes),
            ("scenario", out.scenario_changes),
        )
        for family, records in families:
            for record in records:
                for field_change in record.fields:
                    for side in ("before", "after", "delta"):
                        raw = getattr(field_change, side)
                        if raw is not None:
                            self._add_text_value(
                                table,
                                f"comparison.{family}.{record.key}."
                                f"{field_change.field}.{side}",
                                raw,
                            )

        untrusted: dict[str, str] = {}
        if scenario.label:
            untrusted[UNTRUSTED_SCENARIO_LABEL_KEY] = scenario.label

        return ExplanationInputV1(
            explanation_type="COMPARISON",
            tax_year=out.scenario.tax_year or scenario.tax_year or 0,
            jurisdiction=out.scenario.jurisdiction,
            analysis_id=scenario.base_analysis_id,
            as_of=as_of.isoformat(),
            subject_binding=SubjectBinding(
                subject_type="scenario_comparison",
                subject_id=str(scenario_id),
                comparison_hash=out.comparison_hash,
                result_hash=scenario.scenario_result_hash,
            ),
            version_manifest=VersionManifestV1(
                result_schema_version=out.scenario.result_schema_version,
                product_contract_version=BEFORE_YOU_ACT_SCHEMA_VERSION,
                reference_data_binding="sealed_in_spec_hash",
            ),
            comparison=out,
            assumptions=list(scenario.assumptions),
            value_table=table,
            freshness=scenario.freshness,
            integrity=scenario.integrity,
            display_context=DisplayContextV1(untrusted=untrusted),
        )

    async def _evidence(
        self, journal_id: uuid.UUID, as_of: date
    ) -> ExplanationInputV1:
        service = DecisionJournalService(self.s, self.user_id)
        detail = journal_presentation.journal_detail(await service.detail(journal_id))
        scenario = await ScenarioQueryService(self.user_id).detail(
            detail.scenario_reference.scenario_id
        )
        table: dict[str, ValueRecordV1] = {}
        if scenario.tax_year is not None:
            self._add_year(table, scenario.tax_year)

        return ExplanationInputV1(
            explanation_type="EVIDENCE_READINESS",
            tax_year=scenario.tax_year or 0,
            jurisdiction=scenario.jurisdiction,
            analysis_id=scenario.base_analysis_id,
            as_of=as_of.isoformat(),
            subject_binding=SubjectBinding(
                subject_type="decision_journal",
                subject_id=str(journal_id),
                result_hash=detail.scenario_reference.scenario_result_hash,
                comparison_hash=detail.scenario_reference.comparison_hash,
            ),
            version_manifest=VersionManifestV1(
                result_schema_version=(
                    detail.scenario_reference.scenario_result_schema_version
                ),
                product_contract_version=DECISION_JOURNAL_SCHEMA_VERSION,
                reference_data_binding="sealed_in_spec_hash",
            ),
            evidence=detail.evidence_context,
            value_table=table,
            freshness=scenario.freshness,
            integrity=scenario.integrity,
        )

    async def _what_changed(self, tax_year: int, as_of: date) -> ExplanationInputV1:
        changes, baseline = await RetentionService(self.s, self.user_id).changes_for(
            tax_year=tax_year, as_of=as_of
        )
        out = retention_presentation.changes_detail(changes, baseline)
        table: dict[str, ValueRecordV1] = {}
        self._add_year(table, tax_year)
        self._add_count(table, "changes.total", out.summary.total)
        for change in out.changes:
            for transition in change.transitions:
                for side in ("before", "after"):
                    raw = getattr(transition, side)
                    if raw is not None:
                        self._add_text_value(
                            table,
                            f"changes.{change.change_id}.{transition.field}.{side}",
                            raw,
                        )

        return ExplanationInputV1(
            explanation_type="WHAT_CHANGED",
            tax_year=tax_year,
            jurisdiction=None,
            analysis_id=None,
            as_of=out.as_of,
            subject_binding=SubjectBinding(
                subject_type="retention_changes",
                subject_id=str(tax_year),
                snapshot_hash=out.current_snapshot_hash,
            ),
            version_manifest=VersionManifestV1(
                product_contract_version=RETENTION_CHANGES_SCHEMA_VERSION,
            ),
            changes=out,
            value_table=table,
            freshness=FreshnessOut(freshness_status="unknown"),
            integrity=IntegrityOut(
                integrity_status="not_checked",
                integrity_state=user_visible_state("not_checked", "NONE"),
                integrity_reason_code="NONE",
                integrity_warning=integrity_warning("not_checked", "NONE"),
            ),
        )

    # --------------------------------------------------- value-table helpers --
    @staticmethod
    def _add_year(table: dict[str, ValueRecordV1], year: int) -> None:
        table["tax_year"] = ValueRecordV1(
            key="tax_year", rendered=str(year),
            accepted_forms=vf.year_forms(year), kind="year",
        )

    @staticmethod
    def _add_money(
        table: dict[str, ValueRecordV1], key: str, value: Decimal | None,
        field_scope: list[str] | None = None,
    ) -> None:
        if value is None:
            return
        canonical = f"{Decimal(value):.2f}"
        table[key] = ValueRecordV1(
            key=key, rendered=canonical,
            accepted_forms=vf.money_forms(canonical), kind="money",
            currency_code="CAD", field_scope=field_scope or [],
        )

    def _add_monetary(
        self, table: dict[str, ValueRecordV1], key: str,
        amount: MonetaryAmount | None, field_scope: list[str] | None = None,
    ) -> None:
        if amount is None:
            return
        self._add_money(table, key, amount.amount, field_scope=field_scope)
        table[key].effect_type = amount.effect_type

    @staticmethod
    def _add_rate(
        table: dict[str, ValueRecordV1], key: str, value: Decimal | None
    ) -> None:
        if value is None:
            return
        table[key] = ValueRecordV1(
            key=key, rendered=vf.rate_display(value),
            accepted_forms=vf.rate_forms(value), kind="rate",
        )

    @staticmethod
    def _add_count(
        table: dict[str, ValueRecordV1], key: str, value: int | None
    ) -> None:
        if value is None:
            return
        table[key] = ValueRecordV1(
            key=key, rendered=str(value),
            accepted_forms=vf.count_forms(value), kind="count",
        )

    @staticmethod
    def _add_support(
        table: dict[str, ValueRecordV1], key: str, value: Decimal | None
    ) -> None:
        if value is None:
            return
        table[key] = ValueRecordV1(
            key=key, rendered=format(value.normalize(), "f"),
            accepted_forms=vf.count_forms(value), kind="count",
        )

    @staticmethod
    def _add_text_number(
        table: dict[str, ValueRecordV1], key: str, raw: str | None,
        *, kind: str, field_scope: list[str] | None = None,
    ) -> None:
        """A number that arrives AS A STRING from a governed surface (canonical
        pass-through fields). Non-numeric strings are simply not values."""
        if raw is None:
            return
        try:
            value = Decimal(raw)
        except InvalidOperation:
            return
        canonical = f"{value:.2f}" if kind == "money" else format(value, "f")
        forms = set(vf.money_forms(canonical) if kind == "money"
                    else vf.count_forms(value))
        forms.add(raw)
        table[key] = ValueRecordV1(
            key=key, rendered=canonical, accepted_forms=sorted(forms),
            kind="money" if kind == "money" else "text_number",
            currency_code="CAD" if kind == "money" else None,
            field_scope=field_scope or [],
        )

    def _add_text_value(
        self, table: dict[str, ValueRecordV1], key: str, raw: str | None,
        *, kind: str = "text_number",
    ) -> None:
        """A governed string value: a clean number becomes a text_number
        record; anything else has its embedded numbers/dates absorbed."""
        self._add_text_number(table, key, raw, kind=kind)
        if raw is not None and key not in table:
            self._absorb_supplied_text(table, key, raw)

    def _absorb_supplied_text(
        self, table: dict[str, ValueRecordV1], key_prefix: str, text: str | None
    ) -> None:
        """Register every material number and date that already appears in a
        SUPPLIED governed string (citation text, rule-authored prose, engine
        labels), so prose quoting that string verbatim validates. Untrusted
        user-authored text is NEVER absorbed — a number a user typed into a
        label must not become a permitted tax figure."""
        if not text:
            return
        remaining = text
        for i, match in enumerate(vf.ISO_DATE_RE.findall(remaining)):
            self._add_date_string(table, f"{key_prefix}.date{i}", match)
        remaining = vf.ISO_DATE_RE.sub(" ", remaining)
        for i, match in enumerate(vf.SPELLED_DATE_RE.findall(remaining)):
            parsed = vf.parse_spelled_date(match)
            if parsed is not None:
                key = f"{key_prefix}.sdate{i}"
                table[key] = ValueRecordV1(
                    key=key, rendered=parsed.isoformat(),
                    accepted_forms=vf.date_forms(parsed), kind="date",
                )
        remaining = vf.SPELLED_DATE_RE.sub(" ", remaining)
        for i, match in enumerate(vf.NUMBER_TOKEN_RE.findall(remaining)):
            if not vf.is_material_token(match):
                continue
            token = vf.normalize_token(match)
            key = f"{key_prefix}.n{i}"
            table[key] = ValueRecordV1(
                key=key, rendered=token, accepted_forms=[token],
                kind="text_number",
            )

    @staticmethod
    def _add_date_string(
        table: dict[str, ValueRecordV1], key: str, iso: str | None
    ) -> None:
        if not iso:
            return
        try:
            parsed = date.fromisoformat(iso)
        except ValueError:
            return
        table[key] = ValueRecordV1(
            key=key, rendered=iso, accepted_forms=vf.date_forms(parsed), kind="date",
        )


def vf_money_or_none(value: Decimal | None) -> str | None:
    return None if value is None else f"{Decimal(value):.2f}"
