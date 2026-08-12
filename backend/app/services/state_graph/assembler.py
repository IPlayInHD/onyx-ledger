"""Assembly: sealed rows in, one deterministic graph out. Pure — no session,
no clock, no network.

The assembler cannot reach back to the database for a value it forgot, because
it has no session. That is deliberate, and it is what keeps this a projection
rather than a place where a second engine could grow.

EVERY NUMBER ARRIVES ALREADY COMPUTED. Money passes through `canonical.money`
so the scale is explicit and the hash is stable; nothing is added up. The one
total in the graph is `strategy_portfolio.portfolio_total_benefit`, carried
through from the service documented as the only source of a user-facing total.
Summing `standalone_potential` across candidates would be exactly the
double-count the resource ledger exists to prevent.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from app.services.ioe.domain import canonical as c
from app.services.ioe.domain.integrity import IntegrityStatus

from .contracts import (
    EdgeType,
    EvidenceReadiness,
    GraphAnchor,
    GraphEdge,
    GraphNode,
    GraphScope,
    GraphView,
    NodeFreshness,
    NodeType,
    Provenance,
    TaxStateGraph,
    freshness_from_source,
    summarize,
)
from .hashing import compute_graph_hash
from .loader import GraphSources
from .readiness import DocumentRequirement, resolve_requirement, rollup_readiness


def _money(value: Decimal | None) -> str | None:
    return c.money(value) if value is not None else None


def _rate(value: Decimal | None) -> str | None:
    return c.rate(value) if value is not None else None


def _integrity(value: str) -> IntegrityStatus:
    """Carried through verbatim, refusing anything unrecognised.

    `not_checked` is reported as `not_checked` and never softened into
    `verified` — the difference between "we checked and it reproduced" and "we
    have not looked" is the whole point of the integrity axis.
    """
    try:
        return IntegrityStatus(value)
    except ValueError:
        raise ValueError(f"unrecognised integrity status: {value!r}") from None


class GraphAssembler:
    """One assembly. Instances are not reused; the state below is scratch."""

    def __init__(self, sources: GraphSources, *, user_id: uuid.UUID, tax_year: int):
        self.src = sources
        self.scope = GraphScope(
            user_id=str(user_id), tax_year=tax_year, view=GraphView.CURRENT
        )
        self._nodes: list[GraphNode] = []
        self._edges: list[GraphEdge] = []
        #: candidate id -> its OPPORTUNITY node key, so edges never re-derive one
        self._candidate_keys: dict[uuid.UUID, str] = {}
        #: resource_code -> RESOURCE node key
        self._resource_keys: dict[str, str] = {}
        #: rule_version_id -> DEADLINE node keys
        self._deadline_keys: dict[uuid.UUID, list[str]] = {}
        #: rule_version_id -> EVIDENCE requirement node keys
        self._requirement_keys: dict[uuid.UUID, list[str]] = {}
        #: rule_version_id -> its rolled-up readiness
        self._rule_readiness: dict[uuid.UUID, EvidenceReadiness] = {}
        #: scenario id -> its ASSUMPTION node keys
        self._scenario_assumption_keys: dict[uuid.UUID, list[str]] = {}

    # ----------------------------------------------------------------- api --
    def assemble(self) -> TaxStateGraph:
        self._facts()
        self._tax_state()
        self._evidence()
        self._deadlines()
        self._resources()
        self._opportunities()
        self._assumptions()
        self._scenarios()
        self._edges_from_portfolio()
        self._edges_from_relationships()

        anchors = self._anchors()
        portfolio_total = (
            _money(self.src.portfolio.portfolio_total_benefit)
            if self.src.portfolio is not None
            else None
        )
        summary = summarize(
            self._nodes, self._edges, portfolio_total_benefit=portfolio_total
        )
        return TaxStateGraph(
            scope=self.scope,
            anchors=anchors,
            nodes=tuple(sorted(self._nodes, key=lambda n: n.key)),
            edges=tuple(sorted(self._edges, key=lambda e: e.sort_key)),
            summary=summary,
            graph_hash=compute_graph_hash(
                scope=self.scope,
                anchors=anchors,
                nodes=self._nodes,
                edges=self._edges,
                summary=summary,
            ),
        )

    # -------------------------------------------------------------- nodes --
    def _add(self, node: GraphNode) -> str:
        self._nodes.append(node)
        return node.key

    def _facts(self) -> None:
        """Producer 1 — the tenant's own declarations.

        `verification_status` decides provenance rather than the presence of a
        document id: a row can name a document and still be unverified, and
        claiming DOCUMENT_EXTRACTED for it would overstate where the number came
        from.
        """
        for income in self.src.income:
            self._add(GraphNode(
                node_type=NodeType.FACT,
                source_kind="finance.income_source",
                source_id=f"{income.id}:{income.tax_year}",
                provenance=_declared_provenance(income.verification_status),
                freshness=NodeFreshness.NOT_TRACKED,
                attributes={
                    "fact_kind": "income",
                    "amount": _money(income.amount),
                    "currency_code": income.currency_code,
                    "frequency": income.frequency,
                    "province_code": income.province_code,
                    "verification_status": income.verification_status,
                    "has_supporting_document": income.document_id is not None,
                },
            ))
        for expense in self.src.expenses:
            self._add(GraphNode(
                node_type=NodeType.FACT,
                source_kind="finance.expense_record",
                source_id=f"{expense.id}:{expense.tax_year}",
                provenance=_declared_provenance(expense.verification_status),
                freshness=NodeFreshness.NOT_TRACKED,
                attributes={
                    "fact_kind": "expense",
                    "amount": _money(expense.amount),
                    "currency_code": expense.currency_code,
                    "incurred_on": expense.incurred_on,
                    "verification_status": expense.verification_status,
                    "has_supporting_document": expense.receipt_document_id is not None,
                },
            ))
        for asset in self.src.assets:
            self._add(GraphNode(
                node_type=NodeType.FACT,
                source_kind="wealth.asset",
                source_id=str(asset.id),
                provenance=Provenance.USER_DECLARED,
                freshness=NodeFreshness.NOT_TRACKED,
                attributes={
                    "fact_kind": "asset",
                    "current_value": _money(asset.current_value),
                    "current_value_as_of": asset.current_value_as_of,
                    "acquisition_cost": _money(asset.acquisition_cost),
                    "currency_code": asset.currency_code,
                },
            ))
        for liability in self.src.liabilities:
            self._add(GraphNode(
                node_type=NodeType.FACT,
                source_kind="wealth.liability",
                source_id=str(liability.id),
                provenance=Provenance.USER_DECLARED,
                freshness=NodeFreshness.NOT_TRACKED,
                attributes={
                    "fact_kind": "liability",
                    "current_balance": _money(liability.current_balance),
                    "current_balance_as_of": liability.current_balance_as_of,
                    "interest_rate": _rate(liability.interest_rate),
                    "status": liability.status,
                    "currency_code": liability.currency_code,
                },
            ))

        profile = self.src.tax_profile
        if profile is not None:
            # Structural facts only. Date of birth, employer name and industry
            # are identity rather than tax state, and a read model that carries
            # them widens the personal-data surface for no analytical gain.
            self._add(GraphNode(
                node_type=NodeType.FACT,
                source_kind="profile.tax_profile",
                source_id=str(profile.user_id),
                provenance=Provenance.USER_DECLARED,
                freshness=NodeFreshness.NOT_TRACKED,
                attributes={
                    "fact_kind": "profile",
                    "province_code": profile.province_code,
                    "residency_status": profile.residency_status,
                    "marital_status": profile.marital_status,
                    "is_student": profile.is_student,
                    "has_disability": profile.has_disability,
                    "first_time_home_buyer": profile.first_time_home_buyer,
                    "is_self_employed": profile.is_self_employed,
                    "owns_home": profile.owns_home,
                    "has_investments": profile.has_investments,
                    "has_rental_income": profile.has_rental_income,
                    "has_foreign_income": profile.has_foreign_income,
                    "has_crypto": profile.has_crypto,
                },
            ))

    def _tax_state(self) -> None:
        """Producer 2 — TaxEngineService output.

        The run and its line items are all TAX_STATE; a line item is a component
        of the state, not a different kind of thing. `DERIVED_FROM` links each
        component to the run it came out of.

        There is deliberately no FACT -> TAX_STATE edge. The engine's inputs are
        frozen as one JSON blob in `analysis_input_snapshot`, so which
        declaration produced which line item is not recorded — and deriving it
        would be inference.
        """
        analysis = self.src.analysis
        if analysis is None:
            return
        run_key = self._add(GraphNode(
            node_type=NodeType.TAX_STATE,
            source_kind="analysis.analysis_run",
            source_id=str(analysis.id),
            provenance=Provenance.ENGINE_COMPUTED,
            freshness=NodeFreshness.NOT_TRACKED,
            attributes={
                "tax_year": analysis.tax_year,
                "province_code": analysis.province_code,
                "engine_version": analysis.engine_version,
                "total_income": _money(analysis.total_income),
                "taxable_income": _money(analysis.taxable_income),
                "estimated_tax": _money(analysis.estimated_tax),
                "estimated_savings": _money(analysis.estimated_savings),
                "marginal_rate": _rate(analysis.marginal_rate),
                "average_rate": _rate(analysis.average_rate),
                "confidence_score": analysis.confidence_score,
                "data_verified": analysis.data_verified,
            },
        ))
        for item in self.src.line_items:
            item_key = self._add(GraphNode(
                node_type=NodeType.TAX_STATE,
                source_kind="analysis.analysis_line_item",
                source_id=str(item.id),
                provenance=Provenance.ENGINE_COMPUTED,
                freshness=NodeFreshness.NOT_TRACKED,
                attributes={
                    "kind": item.kind,
                    "label": item.label,
                    "amount": _money(item.amount),
                    "fact_key": item.fact_key,
                    # Rule identity is an ATTRIBUTE, not an edge: there is no
                    # RULE node type in this entry, and inventing one to carry
                    # an edge would be the ninth live type.
                    "tax_rule_version_id": item.tax_rule_version_id,
                },
            ))
            self._edges.append(GraphEdge(
                edge_type=EdgeType.DERIVED_FROM,
                source_key=item_key,
                target_key=run_key,
            ))

    def _evidence(self) -> None:
        """Producer 5 — governed requirements resolved against held documents.

        Two shapes of EVIDENCE node, distinguished by `source_kind`: the
        requirement (which exists whether or not anything satisfies it — that is
        what lets MISSING be a state rather than an absence) and the held
        document.
        """
        held_by_type: dict[str, list[str]] = {}
        for document_id, type_code in self.src.held_documents:
            held_by_type.setdefault(type_code, []).append(str(document_id))

        document_keys: dict[str, str] = {}
        for document_id, type_code in self.src.held_documents:
            # No object key, no bucket, no content hash, no filename: the graph
            # says a document of this type exists, never where it is stored or
            # what it contains.
            document_keys[str(document_id)] = self._add(GraphNode(
                node_type=NodeType.EVIDENCE,
                source_kind="docs.document",
                source_id=str(document_id),
                provenance=Provenance.DOCUMENT_EXTRACTED,
                freshness=NodeFreshness.NOT_TRACKED,
                attributes={
                    "evidence_kind": "held_document",
                    "document_type_code": type_code,
                },
            ))

        by_version: dict[uuid.UUID, list[DocumentRequirement]] = {}
        for required in self.src.required_documents:
            by_version.setdefault(required.rule_version_id, []).append(
                DocumentRequirement(
                    rule_version_id=str(required.rule_version_id),
                    document_type_code=required.document_type_code,
                    necessity=required.necessity,
                )
            )

        for version_id, requirements in by_version.items():
            coverages = [resolve_requirement(r, held_by_type) for r in requirements]
            self._rule_readiness[version_id] = rollup_readiness(coverages)
            for coverage in coverages:
                requirement = coverage.requirement
                key = self._add(GraphNode(
                    node_type=NodeType.EVIDENCE,
                    source_kind="rules.rule_required_document",
                    source_id=(
                        f"{requirement.rule_version_id}:{requirement.document_type_code}"
                    ),
                    provenance=Provenance.DERIVED_DETERMINISTIC,
                    freshness=NodeFreshness.NOT_TRACKED,
                    attributes={
                        "evidence_kind": "requirement",
                        "document_type_code": requirement.document_type_code,
                        "necessity": requirement.necessity,
                        "readiness": coverage.readiness.value,
                        "tax_rule_version_id": requirement.rule_version_id,
                    },
                ))
                self._requirement_keys.setdefault(version_id, []).append(key)
                for satisfying_id in coverage.satisfying_document_ids:
                    self._edges.append(GraphEdge(
                        edge_type=EdgeType.SUPPORTED_BY,
                        source_key=key,
                        target_key=document_keys[satisfying_id],
                    ))

    def _deadlines(self) -> None:
        """Producer 4 — governed rule data, reached through the pinned versions."""
        for deadline in self.src.deadlines:
            key = self._add(GraphNode(
                node_type=NodeType.DEADLINE,
                source_kind="rules.rule_deadline",
                source_id=str(deadline.id),
                provenance=Provenance.RULE_DATA,
                freshness=NodeFreshness.NOT_TRACKED,
                attributes={
                    "deadline_code": deadline.deadline_code,
                    "deadline_date": deadline.deadline_date,
                    "is_hard": deadline.is_hard,
                    "jurisdiction_code": deadline.jurisdiction_code,
                    "tax_rule_version_id": deadline.rule_version_id,
                },
            ))
            self._deadline_keys.setdefault(deadline.rule_version_id, []).append(key)

    def _resources(self) -> None:
        """Producer 6 — the anti-double-counting ledger."""
        for entry in self.src.ledger:
            self._resource_keys[entry.resource_code] = self._add(GraphNode(
                node_type=NodeType.RESOURCE,
                source_kind="ioe.resource_ledger_entry",
                source_id=str(entry.id),
                provenance=Provenance.ENGINE_COMPUTED,
                freshness=self._run_freshness(),
                stale_reason_codes=self._run_stale_reasons(),
                attributes={
                    "resource_code": entry.resource_code,
                    "pool_scope": entry.pool_scope,
                    "capacity": _money(entry.capacity),
                    "allocated": _money(entry.allocated),
                    "remaining": _money(entry.remaining),
                },
            ))

    def _opportunities(self) -> None:
        """Producer 3 — RulesEvaluatorService verdicts sealed as candidates.

        Both evidence axes are carried: `evidence_status` describes how well the
        inputs to the figure are supported, `readiness` describes whether the
        documents the rule demands are held. Neither is derived from the other.
        """
        for candidate in self.src.candidates:
            readiness = (
                self._rule_readiness.get(candidate.tax_rule_version_id)
                if candidate.tax_rule_version_id is not None
                else None
            )
            key = self._add(GraphNode(
                node_type=NodeType.OPPORTUNITY,
                source_kind="ioe.optimization_candidate",
                source_id=str(candidate.id),
                provenance=Provenance.ENGINE_COMPUTED,
                freshness=self._run_freshness(),
                stale_reason_codes=self._run_stale_reasons(),
                integrity=(
                    _integrity(self.src.run.integrity_status)
                    if self.src.run is not None
                    else IntegrityStatus.NOT_CHECKED
                ),
                integrity_reason_code=(
                    self.src.run.integrity_reason_code
                    if self.src.run is not None else "NONE"
                ),
                attributes={
                    "opportunity_code": candidate.opportunity_code,
                    "eligibility_status": candidate.eligibility_status,
                    "calculation_basis": candidate.calculation_basis,
                    "evidence_status": candidate.evidence_status,
                    "readiness": readiness.value if readiness is not None else None,
                    "standalone_potential": _money(candidate.standalone_potential),
                    "incremental_portfolio_benefit": _money(
                        candidate.incremental_portfolio_benefit
                    ),
                    "portfolio_membership": candidate.portfolio_membership,
                    "exclusion_reason_code": candidate.exclusion_reason_code,
                    "candidate_rank": candidate.candidate_rank,
                    "raw_support_score": _rate(candidate.raw_support_score),
                    "assumption_adjusted_score": _rate(
                        candidate.assumption_adjusted_score
                    ),
                    "display_support_score": _rate(candidate.display_support_score),
                    "support_cap_applied": candidate.support_cap_applied,
                    "support_cap_reason_code": candidate.support_cap_reason_code,
                    "requires_re_evaluation": candidate.requires_re_evaluation,
                    "re_evaluation_reason_code": candidate.re_evaluation_reason_code,
                    "tax_rule_version_id": candidate.tax_rule_version_id,
                },
            ))
            self._candidate_keys[candidate.id] = key

            version_id = candidate.tax_rule_version_id
            if version_id is None:
                continue
            for requirement_key in self._requirement_keys.get(version_id, ()):
                self._edges.append(GraphEdge(
                    edge_type=EdgeType.REQUIRES,
                    source_key=key,
                    target_key=requirement_key,
                ))
            for deadline_key in self._deadline_keys.get(version_id, ()):
                self._edges.append(GraphEdge(
                    edge_type=EdgeType.EXPIRES_AT,
                    source_key=key,
                    target_key=deadline_key,
                ))

    def _assumptions(self) -> None:
        """Producer 7 — declared assumptions.

        Run assumptions come from `optimization_run.assumption_set`, the
        canonicalized set that entered the spec hash. It is NULL on runs sealed
        before that column existed, which makes their assumptions unavailable
        rather than empty — reported by emitting nothing rather than by
        inventing an empty set.

        An entry with no recognisable code is skipped. The alternative is a node
        keyed by list position, which would change identity whenever the sealed
        order changed.
        """
        run = self.src.run
        if run is not None and isinstance(run.assumption_set, list):
            for entry in run.assumption_set:
                if not isinstance(entry, dict):
                    continue
                code = entry.get("code") or entry.get("assumption_code")
                if not isinstance(code, str) or not code:
                    continue
                self._add(GraphNode(
                    node_type=NodeType.ASSUMPTION,
                    source_kind="ioe.optimization_run.assumption_set",
                    source_id=f"{run.id}:{code}",
                    provenance=Provenance.ASSUMPTION_DECLARED,
                    freshness=self._run_freshness(),
                    stale_reason_codes=self._run_stale_reasons(),
                    attributes={
                        "assumption_code": code,
                        "source": _text_or_none(entry.get("source")),
                        "certainty": _text_or_none(entry.get("certainty")),
                        "materiality": _text_or_none(entry.get("materiality")),
                        "affects_eligibility": bool(entry.get("affects_eligibility")),
                    },
                ))

        by_scenario: dict[uuid.UUID, list[str]] = {}
        for assumption in self.src.scenario_assumptions:
            key = self._add(GraphNode(
                node_type=NodeType.ASSUMPTION,
                source_kind="ioe.scenario_assumption",
                source_id=str(assumption.id),
                provenance=Provenance.ASSUMPTION_DECLARED,
                freshness=NodeFreshness.NOT_TRACKED,
                attributes={
                    "assumption_code": assumption.assumption_code,
                    "value_number": _quantity(assumption.value_number),
                    "value_text": assumption.value_text,
                    "value_boolean": assumption.value_boolean,
                    "materiality": assumption.materiality,
                    "source": assumption.source,
                    "certainty": assumption.certainty,
                    "affects_eligibility": assumption.affects_eligibility,
                },
            ))
            by_scenario.setdefault(assumption.scenario_id, []).append(key)
        self._scenario_assumption_keys = by_scenario

    def _scenarios(self) -> None:
        """Producer 8 — ScenarioService results.

        `label` and `note` are omitted. They carry column comments saying they
        are excluded from the scenario's own hashes because renaming must not
        change identity, and a graph hash that moved when a scenario was renamed
        would report a change that did not happen.
        """
        analysis = self.src.analysis
        for scenario in self.src.scenarios:
            key = self._add(GraphNode(
                node_type=NodeType.SCENARIO,
                source_kind="ioe.scenario",
                source_id=str(scenario.id),
                provenance=Provenance.ENGINE_COMPUTED,
                freshness=freshness_from_source(scenario.freshness_status),
                stale_reason_codes=(
                    (scenario.stale_reason_code,)
                    if scenario.stale_reason_code else ()
                ),
                integrity=_integrity(scenario.integrity_status),
                integrity_reason_code=scenario.integrity_reason_code,
                attributes={
                    "workflow_status": scenario.workflow_status,
                    "tax_year": scenario.tax_year,
                    "jurisdiction": scenario.jurisdiction,
                    "objective_code": scenario.objective_code,
                    "objective_version": scenario.objective_version,
                    "baseline_tax": _money(scenario.baseline_tax),
                    "scenario_result_hash": scenario.scenario_result_hash,
                },
            ))
            if analysis is not None and scenario.base_analysis_id == analysis.id:
                self._edges.append(GraphEdge(
                    edge_type=EdgeType.REFERENCES_SCENARIO,
                    source_key=key,
                    target_key=(
                        f"{NodeType.TAX_STATE.value}:analysis.analysis_run:{analysis.id}"
                    ),
                ))
            for assumption_key in self._scenario_assumption_keys.get(scenario.id, ()):
                self._edges.append(GraphEdge(
                    edge_type=EdgeType.ASSUMES,
                    source_key=key,
                    target_key=assumption_key,
                ))

    # -------------------------------------------------------------- edges --
    def _edges_from_portfolio(self) -> None:
        """Exclusions and allocations, both endpoints already present.

        `INELIGIBLE_BECAUSE` points at the candidate that blocked this one;
        `CONSTRAINED_BY` points at the resource that ran out. Two different
        answers to "why not", and collapsing them would lose which one applies.
        """
        for exclusion in self.src.exclusions:
            source_key = self._candidate_keys.get(exclusion.candidate_id)
            if source_key is None:
                continue
            blocking_key = (
                self._candidate_keys.get(exclusion.blocking_candidate_id)
                if exclusion.blocking_candidate_id is not None
                else None
            )
            if blocking_key is not None:
                self._edges.append(GraphEdge(
                    edge_type=EdgeType.INELIGIBLE_BECAUSE,
                    source_key=source_key,
                    target_key=blocking_key,
                    attributes={
                        "reason_code": exclusion.reason_code,
                        "membership": exclusion.membership,
                    },
                ))
            resource_key = (
                self._resource_keys.get(exclusion.shared_resource_code)
                if exclusion.shared_resource_code is not None
                else None
            )
            if resource_key is not None:
                self._edges.append(GraphEdge(
                    edge_type=EdgeType.CONSTRAINED_BY,
                    source_key=source_key,
                    target_key=resource_key,
                    attributes={"reason_code": exclusion.reason_code},
                ))

        for member in self.src.members:
            source_key = self._candidate_keys.get(member.candidate_id)
            if source_key is None or not isinstance(member.resource_allocations, dict):
                continue
            for resource_code in sorted(member.resource_allocations):
                resource_key = self._resource_keys.get(resource_code)
                if resource_key is None:
                    continue
                self._edges.append(GraphEdge(
                    edge_type=EdgeType.CONSUMES_RESOURCE,
                    source_key=source_key,
                    target_key=resource_key,
                    attributes={
                        "allocated": _allocation_text(
                            member.resource_allocations[resource_code]
                        ),
                    },
                ))

    def _edges_from_relationships(self) -> None:
        """`CONFLICTS_WITH`, budgeted rather than exhaustive.

        Only rows that exist are emitted; the graph never derives the missing
        pairs. `ioe.recommendation_relationship` is pairwise and the optimization
        work already met O(n²) growth there, so `derivation_source` is carried
        through — `rules_contract` edges are authoritative, and the rest are
        derived and may already have been trimmed upstream by the
        sparse-derivation budget.
        """
        for relationship in self.src.relationships:
            source_key = self._candidate_keys.get(relationship.source_candidate_id)
            target_key = self._candidate_keys.get(relationship.target_candidate_id)
            if source_key is None or target_key is None:
                continue
            self._edges.append(GraphEdge(
                edge_type=EdgeType.CONFLICTS_WITH,
                source_key=source_key,
                target_key=target_key,
                attributes={
                    "relationship_type": relationship.relationship_type,
                    "explanation_code": relationship.explanation_code,
                    "derivation_source": relationship.derivation_source,
                    "shared_resource_code": relationship.shared_resource_code,
                    "measured_delta": _money(relationship.measured_delta),
                },
            ))

    # ------------------------------------------------------------ helpers --
    def _run_freshness(self) -> NodeFreshness:
        run = self.src.run
        if run is None:
            return NodeFreshness.NOT_TRACKED
        return freshness_from_source(run.freshness_status)

    def _run_stale_reasons(self) -> tuple[str, ...]:
        run = self.src.run
        if run is None:
            return ()
        return tuple(sorted(run.stale_reason_codes or ()))

    def _anchors(self) -> tuple[GraphAnchor, ...]:
        """The frozen and sealed artifacts this graph is derived from.

        These are why no graph snapshot table is needed: a historical graph is a
        deterministic function of artifacts that cannot change, so persisting one
        would persist a derivable value.
        """
        anchors: list[GraphAnchor] = []
        if self.src.analysis is not None:
            anchors.append(GraphAnchor(
                artifact="analysis.analysis_run",
                artifact_id=str(self.src.analysis.id),
                content_hash=(
                    self.src.snapshot.snapshot_hash
                    if self.src.snapshot is not None else None
                ),
            ))
        if self.src.run is not None:
            anchors.append(GraphAnchor(
                artifact="ioe.optimization_run",
                artifact_id=str(self.src.run.id),
                content_hash=self.src.run.optimization_result_hash,
            ))
        for scenario in self.src.scenarios:
            anchors.append(GraphAnchor(
                artifact="ioe.scenario",
                artifact_id=str(scenario.id),
                content_hash=scenario.scenario_result_hash,
            ))
        return tuple(anchors)


def _declared_provenance(verification_status: str) -> Provenance:
    return (
        Provenance.DOCUMENT_EXTRACTED
        if verification_status == "verified"
        else Provenance.USER_DECLARED
    )


def _quantity(value: Decimal | None) -> str | None:
    return c.quantity(value) if value is not None else None


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _allocation_text(value: Any) -> str | None:
    """Allocations arrive as JSON, so the numeric type is whatever was stored.

    Rendered through `money` when it is a number the scale applies to, and
    dropped otherwise — a float would be refused by the canonicalizer anyway,
    and guessing a scale for an unknown shape would be worse than saying nothing.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return c.money(Decimal(value))
    if isinstance(value, str):
        try:
            return c.money(Decimal(value))
        except Exception:  # noqa: BLE001 — malformed stored value, not a crash
            return None
    return None


def assemble_graph(
    sources: GraphSources, *, user_id: uuid.UUID, tax_year: int
) -> TaxStateGraph:
    return GraphAssembler(sources, user_id=user_id, tax_year=tax_year).assemble()
