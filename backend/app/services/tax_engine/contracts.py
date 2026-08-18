"""Opportunity contract v2 — the rules layer's output contract.

This module is owned by the RULES layer, which is the only authority permitted to
determine legal eligibility and interpret legislation. It exists so that every
legally meaningful field a downstream consumer (notably the Income Optimization
Engine) needs is *supplied* here rather than inferred from rule prose.

Design rules encoded by this contract:

* Legal determinations — eligibility, required actions, required documents,
  dependencies, deadlines, citations, expiry — are authored as rule data and
  published through TKMS four-eyes governance. Consumers use them verbatim.
* `portfolio_lever_ref` names a lever CODE plus parameters. Rule data never names
  an engine input field; the IOE lever registry is the sole authority for what a
  lever does to a TaxInput.
* Absence is meaningful. A rule version with no contract metadata is emitted with
  `eligibility_status = 'indeterminate'` so consumers exclude it rather than
  guessing.

Backward compatibility: `OpportunityContractV2` is a strict superset of the v1
`Opportunity` shape, so existing consumers (AnalysisService, the optimization
ranker) keep working against the same attribute names.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

CONTRACT_VERSION = "opportunity/2.0.0"

# ---- shared enums (string sets; the IOE mirrors these, never redefines them) --
ELIGIBILITY_STATUSES = frozenset(
    {"eligible", "conditionally_eligible", "ineligible", "indeterminate"}
)
CALCULATION_BASES = frozenset(
    {"engine_determined", "rule_formula_determined", "scenario_estimate", "projection_estimate"}
)
ECONOMIC_EFFECT_TYPES = frozenset({
    "immediate_refund_impact", "current_year_tax_reduction", "tax_deferral",
    "refundable_benefit", "recurring_annual_benefit",
    "multi_year_projected_benefit", "future_option_value",
})
COST_TYPES = frozenset({
    # legacy (retained: required_cash_contribution predates the split)
    "required_cash_contribution", "required_expenditure", "implementation_cost",
    # P4 taxonomy — commitments that constrain feasibility are kept distinct from
    # expenditure that is actually lost.
    "liquidity_commitment", "asset_transfer", "nonrecoverable_expenditure",
})
PROJECTION_ELIGIBILITY = frozenset(
    {"eligible", "not_eligible", "conditionally_eligible"}
)
PROJECTION_METHODS = frozenset(
    {"flat_recurring", "indexed_recurring", "fixed_term"}
)
REVERSIBILITY = frozenset({"reversible", "partially_reversible", "irreversible"})
DEPENDENCY_TYPES = frozenset({"requires", "precedes", "excludes", "substitutes"})
DOCUMENT_NECESSITY = frozenset({"required", "recommended", "conditional"})
POOL_SCOPES = frozenset({"individual", "household"})


@dataclass(frozen=True)
class ActionSpec:
    """One action the user must take. Effort and cost are rules-supplied."""

    action_code: str
    description: str
    effort_rating: int = 3           # 1 trivial .. 5 complex
    cost_type: str | None = None
    cost_amount: Decimal | None = None
    deadline_code: str | None = None


@dataclass(frozen=True)
class DocumentSpec:
    document_type_code: str
    necessity: str = "required"
    note: str | None = None


@dataclass(frozen=True)
class DependencySpec:
    depends_on_rule_code: str
    dependency_type: str             # requires | precedes | excludes | substitutes
    note: str | None = None


@dataclass(frozen=True)
class DeadlineSpec:
    deadline_code: str
    deadline_date: date | None = None
    description: str | None = None
    is_hard: bool = True
    jurisdiction_code: str | None = None


@dataclass(frozen=True)
class CitationSpec:
    citation_text: str
    legislation_reference_id: uuid.UUID | None = None
    source_url: str | None = None


@dataclass(frozen=True)
class ExpiryInfo:
    expiry_date: date | None = None
    effective_date: date | None = None
    is_expiring: bool = False


@dataclass(frozen=True)
class AssumptionRequirement:
    """An assumption the rules layer says a consumer must state to use this."""

    code: str
    materiality: str = "medium"      # high | medium | low
    affects_eligibility: bool = False


@dataclass(frozen=True)
class ProjectionAuthorization:
    """Whether published legislation permits projecting this opportunity forward.

    Authored as RULE DATA under four-eyes governance. The IOE consumes it
    verbatim: it never infers that an opportunity recurs, and it never projects
    further than the rule authorizes.
    """

    eligibility: str                          # eligible | not_eligible | conditionally_eligible
    method: str | None = None                 # flat_recurring | indexed_recurring | fixed_term
    maximum_horizon: int | None = None
    required_assumption_codes: tuple[str, ...] = ()

    @property
    def is_authorized(self) -> bool:
        """Only an explicit, complete authorization permits a projection."""
        return (
            self.eligibility in ("eligible", "conditionally_eligible")
            and self.method is not None
            and self.maximum_horizon is not None
            and self.maximum_horizon > 0
        )


@dataclass(frozen=True)
class PortfolioLeverRef:
    """A reference into the IOE lever registry — NOT a field mapping.

    `lever_code` must exist in the registry at the pinned lever_registry_version;
    an unknown code makes the candidate non-evaluable rather than guessed.
    """

    lever_code: str
    parameter_bindings: dict[str, str] = field(default_factory=dict)


@dataclass
class OpportunityContractV2:
    """What RulesEvaluatorService emits for each matched published rule.

    Superset of the v1 `Opportunity`: every v1 attribute is still present with the
    same name and meaning, so existing consumers are unaffected.
    """

    # ---- identity & provenance ----
    rule_version_id: object
    opportunity_code: str
    title: str
    category: str
    jurisdiction: str | None = None
    tax_year: int | None = None
    contract_version: str = CONTRACT_VERSION
    #: Distinguishes the opportunities of a rule version that emits MORE THAN
    #: ONE. `opportunity_code` is the rule's code, so on its own it cannot tell
    #: two outcomes of one version apart, and the semantic candidate identity
    #: built from it collapsed them into a single key.
    #:
    #: `None` means "this version emits exactly one opportunity", which is every
    #: version authored before multi-outcome support. Keeping it absent there is
    #: deliberate: the identity of an existing candidate stays byte-identical, so
    #: sealed scenarios, counterfactual comparisons and historical graphs keep
    #: matching the keys they were sealed with.
    outcome_discriminator: str | None = None

    # ---- v1 display fields (unchanged) ----
    mechanism: str | None = None
    where_text: str | None = None
    how_text: str | None = None
    why_text: str | None = None
    priority: int = 3
    citation: str | None = None

    # ---- legal determinations (RULES LAYER ONLY — never inferred downstream) ----
    eligibility_status: str = "indeterminate"
    eligibility_basis_codes: tuple[str, ...] = ()
    required_actions: tuple[ActionSpec, ...] = ()
    required_documents: tuple[DocumentSpec, ...] = ()
    dependencies: tuple[DependencySpec, ...] = ()
    applicable_deadlines: tuple[DeadlineSpec, ...] = ()
    citations: tuple[CitationSpec, ...] = ()
    expiry_information: ExpiryInfo | None = None
    assumptions_required: tuple[AssumptionRequirement, ...] = ()

    # ---- economics (supplied, not inferred) ----
    calculation_basis: str | None = None
    calculated_impact: Decimal | None = None
    economic_effect_type: str | None = None
    reversibility: str | None = None
    shared_resource_codes: tuple[str, ...] = ()
    portfolio_lever_ref: PortfolioLeverRef | None = None
    # Governed projection metadata. None means the rule said nothing,
    # which is NOT the same as saying the opportunity does not recur —
    # it means no projection may be generated.
    projection: ProjectionAuthorization | None = None

    @property
    def estimated_impact(self) -> Decimal | None:
        """v1 alias. `calculated_impact` is the canonical contract-v2 name."""
        return self.calculated_impact

    @property
    def has_contract_metadata(self) -> bool:
        """True when this version carries contract-v2 authoring.

        Consumers use this to decide whether an opportunity may take part in
        portfolio evaluation; it is never used to synthesize missing legal facts.
        """
        return bool(self.eligibility_basis_codes)

    @property
    def is_portfolio_evaluable(self) -> bool:
        """Portfolio membership requires a lever reference AND a definite status."""
        return (
            self.portfolio_lever_ref is not None
            and self.eligibility_status in ("eligible", "conditionally_eligible")
        )


# The v1 name remains importable so existing call sites and type hints keep working.
Opportunity = OpportunityContractV2
