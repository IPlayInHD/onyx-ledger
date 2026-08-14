"""A State Graph assembled from a sealed historical bundle (Entry 12B1 §17).
Pure — no session, no clock, no network.

WHY THIS IS A SEPARATE ASSEMBLER AND NOT A FLAG ON THE 12A ONE
--------------------------------------------------------------
`GraphAssembler` consumes `GraphSources`: live ORM rows. This consumes a
`HistoricalSourceBundle`: sealed JSON that was written once and has been
immutable since. Same output type, same node identity, same hash construction —
different input, and the difference is the entire point of §16. Threading a
`historical=True` flag through the live assembler would have put a live-row read
path one boolean away from a sealed one.

WHAT IT MAY NOT DO, AND THEREFORE CANNOT
----------------------------------------
No tax engine, no rules evaluator, no optimizer, no latest-rule resolution, no
`docs.document`. It has no session, so none of those are reachable even by
mistake — which is the same argument that keeps the 12A assembler a projection.

WHAT IT DELIBERATELY DOES NOT INVENT
------------------------------------
`RESOURCE` — a single scenario has no portfolio, so there is no ledger. Zero
nodes are emitted and the INAPPLICABILITY is stated by the projection's
applicability metadata, never by an empty family.

`SUPPORTED_BY` and document ids — historical held evidence is sealed as governed
TYPE codes under Historical Held-Evidence Semantics (READINESS_SEMANTICS_ONLY).
Readiness is carried on the requirement node, where a `READY -> MISSING`
transition stays fully observable. No identity edge is fabricated to stand in
for one that was deliberately not retained.

`INELIGIBLE_BECAUSE`, `CONSTRAINED_BY`, `CONSUMES_RESOURCE`, `CONFLICTS_WITH` —
all four are portfolio outcomes and no sealed single-scenario source exists.
"""
from __future__ import annotations

import uuid
from typing import Any

from app.services.ioe.domain.integrity import IntegrityStatus
from app.services.ioe.scenario.held_evidence import historical_readiness
from app.services.ioe.scenario.historical_source import HistoricalSourceBundle
from app.services.state_graph.contracts import (
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
    summarize,
)
from app.services.state_graph.hashing import compute_graph_hash
from app.services.state_graph.readiness import (
    DocumentRequirement,
    RequirementCoverage,
    rollup_readiness,
)

#: Bumped when the assembled historical shape changes in a way that could alter
#: a graph hash for an unchanged sealed bundle.
HISTORICAL_GRAPH_CONTRACT_VERSION = "1.0.0"

#: `source_kind` values. Named rather than inlined so a node key says exactly
#: which sealed artifact it came out of, and so the two sides cannot drift into
#: describing the same artifact differently.
KIND_SNAPSHOT = "analysis.analysis_input_snapshot"
KIND_SEALED_RESULT = "ioe.scenario_result"
KIND_SEALED_LINE_ITEM = "ioe.scenario_result.line_items"
KIND_SEALED_CANDIDATE = "ioe.scenario_result.candidates"
KIND_DEADLINE = "rules.rule_deadline"
KIND_REQUIREMENT = "rules.rule_required_document"
#: Held evidence keyed by governed TYPE CODE. Not `docs.document`: there is no
#: document id in a sealed bundle and there will not be one.
KIND_HELD_EVIDENCE = "sealed.held_evidence"
KIND_ASSUMPTION = "ioe.scenario_assumption"
KIND_SCENARIO = "ioe.scenario"


class HistoricalGraphError(RuntimeError):
    """The sealed bundle cannot be assembled into a well-formed graph.

    Raised only where the seal contradicts ITSELF — two sealed values claiming
    one identity, or a change trace naming a field its own frozen snapshot does
    not carry. Both are conditions that no amount of careful reading downstream
    could recover from, so they are refused here rather than rendered.
    """


def assemble_historical_graph(
    bundle: HistoricalSourceBundle, *, user_id: uuid.UUID
) -> TaxStateGraph:
    """One sealed side, assembled. Deterministic in the bundle alone.

    `user_id` is the scope the graph is read under; it is not read FROM, and
    nothing about it can change what the sealed bundle says.
    """
    return _HistoricalAssembler(bundle, user_id=user_id).assemble()


class _HistoricalAssembler:
    """One assembly. Instances are not reused; the state below is scratch."""

    def __init__(self, bundle: HistoricalSourceBundle, *, user_id: uuid.UUID):
        self.b = bundle
        self.scope = GraphScope(
            user_id=str(user_id),
            tax_year=bundle.tax_year,
            view=GraphView.HISTORICAL,
        )
        self._nodes: list[GraphNode] = []
        self._edges: list[GraphEdge] = []
        #: rule_version_id -> requirement node keys reached through it
        self._requirement_keys: dict[str, list[str]] = {}
        #: rule_version_id -> deadline node keys reached through it
        self._deadline_keys: dict[str, list[str]] = {}
        #: rule_version_id -> its rolled-up sealed readiness
        self._rule_readiness: dict[str, EvidenceReadiness] = {}
        #: assumption_code -> its node key
        self._assumption_keys: dict[str, str] = {}
        #: The sealed result header every line item derives from. Assigned from
        #: the node itself in `_tax_state`, never rebuilt from its parts: two
        #: places composing one key is two places that can drift into composing
        #: it differently, and the edge would silently dangle.
        self._result_key = ""

    def assemble(self) -> TaxStateGraph:
        self._facts()
        self._tax_state()
        self._evidence()
        self._deadlines()
        self._opportunities()
        self._assumptions()
        self._scenario()

        nodes = tuple(sorted(self._nodes, key=lambda n: n.key))
        edges = tuple(sorted(self._edges, key=lambda e: e.sort_key))
        _assert_unique_keys(nodes)
        anchors = self._anchors()
        summary = summarize(nodes, edges)
        return TaxStateGraph(
            scope=self.scope,
            anchors=anchors,
            nodes=nodes,
            edges=edges,
            summary=summary,
            graph_hash=compute_graph_hash(
                scope=self.scope, anchors=anchors,
                nodes=nodes, edges=edges, summary=summary,
            ),
        )

    def _add(self, node: GraphNode) -> str:
        self._nodes.append(node)
        return node.key

    # -------------------------------------------------------------- nodes --
    def _facts(self) -> None:
        """FACT — the frozen snapshot, with this side's sealed lever changes.

        Both sides speak ENGINE-INPUT facts rather than per-row declarations,
        because the frozen snapshot is a canonicalized `TaxInput` and holds no
        `income_source.id`. A baseline assembled from live rows instead would
        diff per-row declarations against engine input fields and report every
        baseline fact as removed — the exact failure the parity matrix named.

        Applying a change is a SUBSTITUTION of a sealed value, never a
        computation: `ioe.scenario_input_change` recorded the field, the old
        value and the new value when the scenario was sealed, and the baseline
        side carries no changes at all.
        """
        raw = self.b.facts.get("inputs")
        inputs: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}

        #: field -> the lever that wrote it. Applied in the sealed
        #: `apply_order`, so two levers touching one field resolve exactly as
        #: they resolved at seal time.
        applied: dict[str, dict[str, Any]] = {}
        for change in sorted(
            self.b.applied_changes, key=lambda ch: (ch.get("apply_order") or 0)
        ):
            field = change.get("field")
            if isinstance(field, str) and field:
                applied[field] = change

        # A change naming a field the frozen snapshot does not carry means the
        # seal's own change trace and its own snapshot disagree. Iterating the
        # snapshot alone would drop that change silently — the counterfactual
        # would simply not show a lever the user applied — so it is refused.
        orphaned = sorted(set(applied) - set(inputs))
        if orphaned:
            raise HistoricalGraphError(
                f"sealed lever changes name fields absent from the frozen "
                f"snapshot: {orphaned}; the seal contradicts itself"
            )

        for field in sorted(inputs):
            written_by = applied.get(field)
            self._add(GraphNode(
                node_type=NodeType.FACT,
                source_kind=KIND_SNAPSHOT,
                source_id=field,
                provenance=Provenance.USER_DECLARED,
                freshness=NodeFreshness.NOT_TRACKED,
                attributes={
                    "fact_kind": "engine_input",
                    "field": field,
                    "value": _text(
                        written_by["new_value"] if written_by is not None
                        else inputs[field]
                    ),
                    # Present only where a sealed lever wrote this field, so a
                    # reader can tell a scenario-changed input from a frozen one
                    # without the comparator having to infer it.
                    "changed_by_lever_code": (
                        _text(written_by.get("lever_code"))
                        if written_by is not None else None
                    ),
                },
            ))

    def _tax_state(self) -> None:
        """TAX_STATE — the sealed result header plus its sealed components.

        The header is emitted on BOTH sides and keyed identically, because it
        names the scenario this side belongs to rather than the side itself. A
        header keyed by side would never match its counterpart and would read as
        an addition and a removal of the same thing.

        A side with no sealed line items emits none. That is a comparable family
        holding nothing, which the projection reports as comparable-with-zero —
        never as a family that does not apply.
        """
        metadata = self.b.scenario_metadata
        self._result_key = self._add(GraphNode(
            node_type=NodeType.TAX_STATE,
            source_kind=KIND_SEALED_RESULT,
            source_id=str(self.b.scenario_id),
            provenance=Provenance.ENGINE_COMPUTED,
            freshness=NodeFreshness.NOT_TRACKED,
            # NOT_CHECKED and never softened: this path reads a seal, it does
            # not verify one. Replay is the authority that may say VERIFIED, and
            # claiming it here would report a check that never ran.
            integrity=IntegrityStatus.NOT_CHECKED,
            attributes={
                "tax_year": metadata.get("tax_year"),
                "jurisdiction": metadata.get("jurisdiction"),
                "result_schema_version": metadata.get("result_schema_version"),
                "scenario_result_hash": metadata.get("scenario_result_hash"),
            },
        ))

        for item in self.b.line_items:
            kind = _text(item.get("kind")) or ""
            label = _text(item.get("label")) or ""
            key = self._add(GraphNode(
                node_type=NodeType.TAX_STATE,
                source_kind=KIND_SEALED_LINE_ITEM,
                # Semantic identity. A sealed line item has no row id, and
                # keying by list position would change identity whenever the
                # sealed order changed — which is why the seal sorts by
                # (kind, label) rather than by engine emission order.
                source_id=f"{kind}:{label}",
                provenance=Provenance.ENGINE_COMPUTED,
                freshness=NodeFreshness.NOT_TRACKED,
                attributes={
                    "kind": kind,
                    "label": label,
                    "amount": _text(item.get("amount")),
                    "fact_key": _text(item.get("fact_key")),
                    "tax_rule_version_id": _text(
                        item.get("tax_rule_version_id")),
                },
            ))
            self._edges.append(GraphEdge(
                edge_type=EdgeType.DERIVED_FROM,
                source_key=key,
                target_key=self._result_key,
            ))

    def _evidence(self) -> None:
        """EVIDENCE — sealed requirements, sealed held TYPES, derived readiness.

        Readiness runs through `historical_readiness`, which runs through
        `readiness_for` — the same function the current graph uses. A historical
        verdict and a current verdict therefore cannot differ by implementation,
        only by the held state they were computed from, which is the point.
        """
        for type_code in sorted(set(self.b.held_evidence.document_type_codes)):
            # A governed `ref.document_type.code`, and nothing else. No id, no
            # filename, no object key, no content hash, no count: readiness asks
            # whether the type is present and never how many.
            self._add(GraphNode(
                node_type=NodeType.EVIDENCE,
                source_kind=KIND_HELD_EVIDENCE,
                source_id=type_code,
                provenance=Provenance.DOCUMENT_EXTRACTED,
                freshness=NodeFreshness.NOT_TRACKED,
                attributes={
                    "evidence_kind": "held_evidence_type",
                    "document_type_code": type_code,
                },
            ))

        resolved = historical_readiness(
            self.b.held_evidence, self.b.required_evidence)

        # DEDUPLICATED, because the sealed bundle re-derives requirements PER
        # CANDIDATE: two candidates pinning one rule version each carry that
        # version's required documents, so the same requirement arrives twice
        # and would otherwise become two nodes sharing one key.
        #
        # Collapsing on the whole value is exact rather than lossy:
        # `rules.rule_required_document` is `UNIQUE (rule_version_id,
        # document_type_code)`, so necessity is a function of the key and two
        # entries with the same key cannot disagree about it.
        seen: set[DocumentRequirement] = set()
        by_version: dict[str, list[RequirementCoverage]] = {}
        for requirement, readiness in resolved:
            if requirement in seen:
                continue
            seen.add(requirement)
            by_version.setdefault(requirement.rule_version_id, []).append(
                RequirementCoverage(
                    requirement=requirement,
                    readiness=readiness,
                    # Empty because no document identity was sealed — NOT
                    # because nothing satisfies the requirement. Readiness above
                    # already carries the answer to that question.
                    satisfying_document_ids=(),
                )
            )

        for version_id, coverages in by_version.items():
            self._rule_readiness[version_id] = rollup_readiness(coverages)
            for coverage in coverages:
                self._add_requirement(version_id, coverage)

    def _add_requirement(
        self, version_id: str, coverage: RequirementCoverage
    ) -> None:
        requirement: DocumentRequirement = coverage.requirement
        key = self._add(GraphNode(
            node_type=NodeType.EVIDENCE,
            source_kind=KIND_REQUIREMENT,
            # The same identity basis the current graph uses, so a requirement
            # is the same thing on both sides of the entry.
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

    def _deadlines(self) -> None:
        """DEADLINE — through each candidate's PINNED rule version.

        Keyed by `(rule_version_id, deadline_code)` rather than by
        `rule_deadline.id`, because that pair is what the sealed bundle carries
        and it is stable across the row moving. No date is emitted: the bundle
        does not carry one, and inventing one from today's rule data is exactly
        what a sealed read may not do.
        """
        for version_id, deadline_code in self.b.deadlines:
            key = self._add(GraphNode(
                node_type=NodeType.DEADLINE,
                source_kind=KIND_DEADLINE,
                source_id=f"{version_id}:{deadline_code}",
                provenance=Provenance.RULE_DATA,
                freshness=NodeFreshness.NOT_TRACKED,
                attributes={
                    "deadline_code": deadline_code,
                    "tax_rule_version_id": version_id,
                },
            ))
            self._deadline_keys.setdefault(version_id, []).append(key)

    def _opportunities(self) -> None:
        """OPPORTUNITY — the sealed normalized candidates.

        Keyed by `candidate_key`, the SEMANTIC identity
        (`opportunity_code:rule_version_id`) the seal already computed through
        `OpportunityNormalizationService`. No physical row id and no evaluation
        order enters it, which is what lets a later comparison match a baseline
        opportunity to its counterfactual twin at all.
        """
        for candidate in self.b.candidates:
            candidate_key = _text(candidate.get("candidate_key")) or ""
            version_id = _text(candidate.get("rule_version_id"))
            readiness = (
                self._rule_readiness.get(version_id)
                if version_id is not None else None
            )
            key = self._add(GraphNode(
                node_type=NodeType.OPPORTUNITY,
                source_kind=KIND_SEALED_CANDIDATE,
                source_id=candidate_key,
                provenance=Provenance.ENGINE_COMPUTED,
                freshness=NodeFreshness.NOT_TRACKED,
                integrity=IntegrityStatus.NOT_CHECKED,
                attributes={
                    "opportunity_code": _text(
                        candidate.get("opportunity_code")),
                    "eligibility_status": _text(
                        candidate.get("eligibility_status")),
                    "eligibility_basis_codes": _codes(
                        candidate.get("eligibility_basis_codes")),
                    "calculation_basis": _text(
                        candidate.get("calculation_basis")),
                    "calculated_impact": _text(
                        candidate.get("calculated_impact")),
                    "economic_effect_type": _text(
                        candidate.get("economic_effect_type")),
                    "reversibility": _text(candidate.get("reversibility")),
                    # A REQUIREMENT DECLARATION — "draws on this pool" — and
                    # never an allocation. There is no ledger for one scenario,
                    # so there is no capacity and no remainder to carry.
                    "shared_resource_codes": _codes(
                        candidate.get("shared_resource_codes")),
                    "raw_support_score": _text(
                        candidate.get("raw_support_score")),
                    "assumption_adjusted_score": _text(
                        candidate.get("assumption_adjusted_score")),
                    "display_support_score": _text(
                        candidate.get("display_support_score")),
                    "support_cap_applied": bool(
                        candidate.get("support_cap_applied")),
                    "support_cap_reason_code": _text(
                        candidate.get("support_cap_reason_code")),
                    "readiness": (
                        readiness.value if readiness is not None else None),
                    "tax_rule_version_id": version_id,
                },
            ))
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
        """ASSUMPTION — the sealed scenario assumptions, keyed by code.

        An entry with no code is skipped rather than keyed by list position: a
        node whose identity moved when the sealed order changed would report a
        difference the user never caused.
        """
        for assumption in self.b.assumptions:
            code = _text(assumption.get("assumption_code"))
            if not code:
                continue
            self._assumption_keys[code] = self._add(GraphNode(
                node_type=NodeType.ASSUMPTION,
                source_kind=KIND_ASSUMPTION,
                source_id=code,
                provenance=Provenance.ASSUMPTION_DECLARED,
                freshness=NodeFreshness.NOT_TRACKED,
                attributes={
                    "assumption_code": code,
                    "materiality": _text(assumption.get("materiality")),
                    "source": _text(assumption.get("source")),
                    "certainty": _text(assumption.get("certainty")),
                    "affects_eligibility": bool(
                        assumption.get("affects_eligibility")),
                },
            ))

    def _scenario(self) -> None:
        """SCENARIO — the sealed scenario row.

        `label` and `note` are absent by construction: the bundle never loaded
        them, matching the column comments that exclude them from the scenario's
        own hashes because renaming must not change identity.
        """
        metadata = self.b.scenario_metadata
        key = self._add(GraphNode(
            node_type=NodeType.SCENARIO,
            source_kind=KIND_SCENARIO,
            source_id=str(self.b.scenario_id),
            provenance=Provenance.ENGINE_COMPUTED,
            # A sealed scenario is frozen, so there is nothing for freshness to
            # measure. Reporting CURRENT would be a claim about live state that
            # this path is forbidden from reading.
            freshness=NodeFreshness.NOT_TRACKED,
            integrity=IntegrityStatus.NOT_CHECKED,
            attributes={
                "tax_year": metadata.get("tax_year"),
                "jurisdiction": metadata.get("jurisdiction"),
                "objective_code": metadata.get("objective_code"),
                "objective_version": metadata.get("objective_version"),
                "result_schema_version": metadata.get("result_schema_version"),
                "scenario_spec_hash": metadata.get("scenario_spec_hash"),
                "scenario_result_hash": metadata.get("scenario_result_hash"),
            },
        ))
        self._edges.append(GraphEdge(
            edge_type=EdgeType.REFERENCES_SCENARIO,
            source_key=key,
            target_key=self._result_key,
        ))
        # Over the keys `_assumptions` actually emitted, so an assumption it
        # skipped can never become an edge to a node that does not exist.
        for assumption_key in self._assumption_keys.values():
            self._edges.append(GraphEdge(
                edge_type=EdgeType.ASSUMES,
                source_key=key,
                target_key=assumption_key,
            ))

    def _anchors(self) -> tuple[GraphAnchor, ...]:
        """The sealed artifact this graph is a deterministic function of.

        One anchor, and it is why nothing here is persisted: the scenario result
        cannot change, so a graph derived from it is derivable rather than
        storable.
        """
        return (
            GraphAnchor(
                artifact=KIND_SCENARIO,
                artifact_id=str(self.b.scenario_id),
                content_hash=_text(
                    self.b.scenario_metadata.get("scenario_result_hash")),
            ),
        )


def _assert_unique_keys(nodes: tuple[GraphNode, ...]) -> None:
    """Two nodes may never share a key.

    A key is the identity a comparator matches on, so a duplicate would make one
    sealed value silently stand in for another. Every family here is keyed on a
    basis the database already makes unique — `UNIQUE (rule_version_id,
    document_type_code)`, `UNIQUE (rule_version_id, deadline_code)`,
    `UNIQUE (scenario_id, assumption_code)`, and the seal's own semantic
    `candidate_key` — so a collision means an assumption behind one of those
    stopped holding. Failing says so; emitting both would not.
    """
    seen: set[str] = set()
    for node in nodes:
        if node.key in seen:
            raise HistoricalGraphError(
                f"duplicate node key {node.key!r} in the assembled historical "
                "graph; two sealed values would share one identity"
            )
        seen.add(node.key)


def _text(value: Any) -> str | None:
    """Sealed JSON carries strings; anything else is reported as absent rather
    than coerced, so a malformed seal cannot be laundered into a plausible
    value."""
    return value if isinstance(value, str) and value != "" else None


def _codes(value: Any) -> tuple[str, ...] | None:
    """A sealed code list.

    `None` and `()` are kept apart deliberately: the seal uses `None` for "the
    rule said nothing" and an empty list for "it said there are none", and
    collapsing the two would turn silence into a positive statement.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return None
