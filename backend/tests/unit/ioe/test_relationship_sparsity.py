"""Sparse relationship derivation — release blocker 1.

The previous derivation compared every candidate with every other one. On a
populated database that produced 6,127 relationship rows, 6,011 of them
`overlaps` edges between candidates that merely wrote the same engine field,
and the row count grew as n²/2. These tests hold the new derivation to the
acceptance targets:

  * relationship rows ≤ 4 × candidate count
  * no O(n²) pair scan
  * deterministic output, independent of candidate input order
  * every relationship type still covered
  * exclusion, shared-limit and dependency semantics unchanged
  * authoritative rules-supplied edges are never silently dropped
  * every edge records why it exists
"""
import itertools
import random
from decimal import Decimal

import pytest

from app.services.ioe.domain import portfolio as pf
from app.services.ioe.domain import relationships as rel
from app.services.ioe.domain.enums import (
    CalculationBasis,
    DerivationSource,
    EconomicEffectType,
    EligibilityStatus,
    PortfolioMembership,
    RelationshipType,
)
from app.services.ioe.domain.models import (
    EconomicEffect,
    LeverApplication,
    OptimizationCandidate,
)


def _candidate(key, *, lever=None, resources=(), excludes=(), requires=(), opportunity=None):
    return OptimizationCandidate(
        candidate_key=key,
        opportunity_code=opportunity or key.lower(),
        rule_version_id=f"rv-{key}",
        eligibility_status=EligibilityStatus.ELIGIBLE,
        economic_effects=(EconomicEffect(
            effect_type=EconomicEffectType.CURRENT_YEAR_TAX_REDUCTION,
            amount=Decimal("100"), calculation_basis=CalculationBasis.ENGINE_DETERMINED),),
        shared_resource_codes=resources,
        excludes_codes=excludes,
        requires_codes=requires,
        lever_application=(
            LeverApplication(lever, {"amount": Decimal("100")}) if lever else None
        ),
    )


def _dense_population(n: int) -> list[OptimizationCandidate]:
    """The worst case for the old implementation: every candidate draws on the
    same pool AND writes the same engine field, so the pairwise scan produced
    n(n−1)/2 edges and this population is exactly what blew the row count up."""
    return [
        _candidate(f"C{i:04d}", lever="INCREASE_RRSP_DEDUCTION", resources=("RRSP_ROOM",))
        for i in range(n)
    ]


# ---- acceptance target: ≤ 4 × candidate count -------------------------------
@pytest.mark.parametrize("n", [2, 10, 50, 100, 250])
def test_edge_count_stays_within_four_per_candidate(n):
    edges = rel.derive(_dense_population(n))
    assert len(edges) <= rel.MAX_EDGES_PER_CANDIDATE * n, (
        f"{len(edges)} edges for {n} candidates"
    )


def test_a_shared_group_costs_one_edge_per_member_not_a_clique():
    """50 candidates on one pool is 49 edges, not 1,225."""
    edges = rel.derive(_dense_population(50))
    shares = [e for e in edges if e.relationship_type is RelationshipType.SHARES_LIMIT]
    assert len(shares) == 49
    # and the full membership of the pool is still recoverable from the edges
    members = {e.source_key for e in shares} | {e.target_key for e in shares}
    assert len(members) == 50


def test_growth_is_linear_not_quadratic():
    """Doubling the population must not quadruple the edges."""
    small = len(rel.derive(_dense_population(50)))
    large = len(rel.derive(_dense_population(200)))
    assert large <= small * 5, f"{small} → {large} looks superlinear"


class _CountingProxy:
    """Counts every attribute read the derivation performs on a candidate.

    A pair scan must read one candidate's fields while holding another, so it
    cannot avoid ~n²/2 reads. A grouped derivation reads O(n log n) — the sort —
    plus a constant per candidate.
    """

    __slots__ = ("_inner", "_counter")

    def __init__(self, inner, counter):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_counter", counter)

    def __getattr__(self, name):
        object.__getattribute__(self, "_counter")["count"] += 1
        return getattr(object.__getattribute__(self, "_inner"), name)


def test_no_pairwise_comparison_of_every_candidate():
    n = 120
    counter = {"count": 0}
    population = [
        _CountingProxy(
            _candidate(f"C{i:04d}", lever="INCREASE_RRSP_DEDUCTION",
                       resources=("RRSP_ROOM",)),
            counter,
        )
        for i in range(n)
    ]
    counter["count"] = 0
    edges = rel.derive(population)                   # type: ignore[arg-type]
    assert edges
    assert counter["count"] < n * n / 4, (
        f"{counter['count']} attribute reads for {n} candidates suggests a pair scan"
    )


# ---- determinism ------------------------------------------------------------
def test_output_is_independent_of_input_order():
    population = [
        _candidate("A", lever="INCREASE_RRSP_DEDUCTION", resources=("RRSP_ROOM",)),
        _candidate("B", lever="INCREASE_RRSP_DEDUCTION", resources=("RRSP_ROOM",)),
        _candidate("C", lever="INCREASE_FHSA_DEDUCTION", resources=("RRSP_ROOM",)),
        _candidate("D", lever="REALIZE_CAPITAL_GAINS", excludes=("a",)),
        _candidate("E", lever="DEFER_CAPITAL_GAINS", requires=("b",)),
    ]
    expected = [e.as_canonical() for e in rel.derive(population)]
    rng = random.Random(20260803)
    for _ in range(25):
        shuffled = population[:]
        rng.shuffle(shuffled)
        assert [e.as_canonical() for e in rel.derive(shuffled)] == expected


def test_every_permutation_of_a_small_population_agrees():
    population = [
        _candidate("A", resources=("POOL",)),
        _candidate("B", resources=("POOL",)),
        _candidate("C", resources=("POOL",)),
    ]
    outputs = {
        tuple(sorted((e.source_key, e.target_key, e.relationship_type)
                     for e in rel.derive(list(order))))
        for order in itertools.permutations(population)
    }
    assert len(outputs) == 1


# ---- coverage: every relationship type still reachable ----------------------
def test_all_relationship_types_have_a_producer_or_are_declared_unused():
    produced = set()
    produced |= {
        e.relationship_type for e in rel.derive([
            _candidate("A", lever="INCREASE_RRSP_DEDUCTION", resources=("RRSP_ROOM",),
                       excludes=("b",)),
            _candidate("B", lever="INCREASE_RRSP_DEDUCTION", resources=("RRSP_ROOM",),
                       requires=("c",)),
            _candidate("C", lever="REALIZE_CAPITAL_GAINS"),
            _candidate("D", lever="DEFER_CAPITAL_GAINS"),
        ])
    }
    up, down = _candidate("U"), _candidate("V")
    down.standalone_potential, down.incremental_portfolio_benefit = (
        Decimal("250"), Decimal("100"))
    produced.add(rel.measured_edge(up, down).relationship_type)
    up2, down2 = _candidate("U"), _candidate("V")
    down2.standalone_potential, down2.incremental_portfolio_benefit = (
        Decimal("100"), Decimal("250"))
    produced.add(rel.measured_edge(up2, down2).relationship_type)

    assert produced == set(RelationshipType) - {RelationshipType.PRECEDES}
    # PRECEDES has no producer at this version but keeps a structured
    # explanation, so introducing one never needs a schema change.
    assert RelationshipType.PRECEDES in rel.EXPLANATION_CODES


def test_symmetric_and_directional_types_partition_every_type():
    assert rel.SYMMETRIC_TYPES | rel.DIRECTIONAL_TYPES == set(RelationshipType)
    assert not (rel.SYMMETRIC_TYPES & rel.DIRECTIONAL_TYPES)


# ---- symmetry, direction, deduplication -------------------------------------
def test_a_symmetric_relationship_is_stored_once_in_canonical_order():
    """Both candidates declaring the exclusion is one fact, not two rows."""
    edges = rel.derive([
        _candidate("B", excludes=("a",)),
        _candidate("A", excludes=("b",)),
    ])
    excludes = [e for e in edges if e.relationship_type is RelationshipType.EXCLUDES]
    assert len(excludes) == 1
    assert (excludes[0].source_key, excludes[0].target_key) == ("A", "B")


def test_direction_is_preserved_for_directional_types():
    edges = rel.derive([_candidate("Z", requires=("a",)), _candidate("A")])
    requires = [e for e in edges if e.relationship_type is RelationshipType.REQUIRES]
    assert len(requires) == 1
    assert (requires[0].source_key, requires[0].target_key) == ("Z", "A")


def test_a_prerequisite_is_recorded_whichever_way_the_keys_sort():
    """The pairwise implementation only looked at `requires_codes` on the
    lower-sorting candidate, so a prerequisite declared by the lower-sorting one
    was silently dropped. Both orientations are now recorded."""
    forward = rel.derive([_candidate("A", requires=("z",)), _candidate("Z")])
    backward = rel.derive([_candidate("Z", requires=("a",)), _candidate("A")])
    assert [e.relationship_type for e in forward] == [RelationshipType.REQUIRES]
    assert [e.relationship_type for e in backward] == [RelationshipType.REQUIRES]


def test_two_pools_between_the_same_pair_are_two_distinct_edges():
    edges = rel.derive([
        _candidate("A", resources=("RRSP_ROOM", "MEDICAL_POOL")),
        _candidate("B", resources=("RRSP_ROOM", "MEDICAL_POOL")),
    ])
    codes = sorted(e.shared_resource_code for e in edges
                   if e.relationship_type is RelationshipType.SHARES_LIMIT)
    assert codes == ["MEDICAL_POOL", "RRSP_ROOM"]


# ---- provenance -------------------------------------------------------------
def test_every_edge_records_its_derivation_source():
    edges = rel.derive([
        _candidate("A", lever="INCREASE_RRSP_DEDUCTION", resources=("RRSP_ROOM",),
                   excludes=("d",)),
        _candidate("B", lever="INCREASE_RRSP_DEDUCTION", resources=("RRSP_ROOM",)),
        _candidate("C", lever="REALIZE_CAPITAL_GAINS"),
        _candidate("D", lever="DEFER_CAPITAL_GAINS"),
    ])
    assert edges
    assert all(isinstance(e.derivation_source, DerivationSource) for e in edges)
    by_type = {e.relationship_type: e.derivation_source for e in edges}
    assert by_type[RelationshipType.EXCLUDES] is DerivationSource.RULES_CONTRACT
    assert by_type[RelationshipType.SHARES_LIMIT] is DerivationSource.SHARED_RESOURCE
    assert by_type[RelationshipType.SUBSTITUTES] is DerivationSource.RELATIONSHIP_REGISTRY
    assert by_type[RelationshipType.OVERLAPS] is DerivationSource.RELATIONSHIP_REGISTRY


def test_measured_edges_are_tagged_as_measured():
    a, b = _candidate("A"), _candidate("B")
    b.standalone_potential, b.incremental_portfolio_benefit = Decimal("250"), Decimal("40")
    assert rel.measured_edge(a, b).derivation_source is DerivationSource.MEASURED_INTERACTION


def test_derivation_source_is_part_of_the_canonical_payload():
    a, b = _candidate("A", resources=("P",)), _candidate("B", resources=("P",))
    edge = rel.derive([a, b])[0]
    assert edge.as_canonical()["derivation_source"] == "shared_resource"


# ---- authoritative edges are never dropped ----------------------------------
def test_the_budget_never_drops_a_rules_supplied_edge():
    """A population where rules-supplied exclusions alone exceed the ceiling.

    Ten candidates in one exclusion group declare 90 directed exclusions (45
    after canonical deduplication) against a budget of 40. Every one survives:
    dropping a declared exclusion would silently change who gets recommended.
    """
    codes = [f"c{i}" for i in range(10)]
    population = [
        _candidate(f"C{i}", opportunity=codes[i],
                   excludes=tuple(c for c in codes if c != codes[i]))
        for i in range(10)
    ]
    edges = rel.derive(population)
    excludes = [e for e in edges if e.relationship_type is RelationshipType.EXCLUDES]
    assert len(excludes) == 45                       # every declared pair, deduplicated
    assert all(e.derivation_source is DerivationSource.RULES_CONTRACT for e in excludes)


def test_the_budget_trims_only_explanatory_edges():
    """Authoritative edges plus a large explanatory population: the ceiling is
    honoured by removing `overlaps`/`shares_limit`, never a decision."""
    codes = [f"c{i}" for i in range(8)]
    declared = [
        _candidate(f"A{i}", opportunity=codes[i], lever="INCREASE_RRSP_DEDUCTION",
                   resources=("RRSP_ROOM",),
                   excludes=tuple(c for c in codes if c != codes[i]))
        for i in range(8)
    ]
    edges = rel.derive(declared)
    assert len(edges) <= rel.MAX_EDGES_PER_CANDIDATE * len(declared)
    kept = [e for e in edges if e.relationship_type in rel.DECISION_BEARING_TYPES]
    assert len(kept) == 28                           # 8 choose 2, all preserved


def test_trimming_is_deterministic():
    codes = [f"c{i}" for i in range(8)]
    population = [
        _candidate(f"A{i}", opportunity=codes[i], lever="INCREASE_RRSP_DEDUCTION",
                   resources=("RRSP_ROOM",),
                   excludes=tuple(c for c in codes if c != codes[i]))
        for i in range(8)
    ]
    rng = random.Random(7)
    expected = [e.as_canonical() for e in rel.derive(population)]
    for _ in range(10):
        shuffled = population[:]
        rng.shuffle(shuffled)
        assert [e.as_canonical() for e in rel.derive(shuffled)] == expected


# ---- structured explanation and resolution options survive ------------------
def test_structured_explanations_and_resolution_options_are_preserved():
    edges = rel.derive(
        [
            _candidate("A", resources=("RRSP_ROOM",), excludes=("b",)),
            _candidate("B", resources=("RRSP_ROOM",)),
        ],
        pool_capacities={"RRSP_ROOM": Decimal("5000")},
    )
    by_type = {e.relationship_type: e for e in edges}
    pool = by_type[RelationshipType.SHARES_LIMIT]
    assert pool.explanation_code == "SHARED_POOL"
    assert pool.maximum_shared_amount == Decimal("5000")
    assert pool.resolution_options == ("APPLY_HIGHER_RANKED", "SPLIT_ALLOCATION")
    conflict = by_type[RelationshipType.EXCLUDES]
    assert conflict.explanation_code == "MUTUALLY_EXCLUSIVE"
    assert conflict.resolution_options == ("CHOOSE_ONE",)


# ---- portfolio semantics unchanged ------------------------------------------
_BASE = {
    "employment_income": Decimal("95000"), "rrsp_deduction": Decimal(0),
    "donations": Decimal(0), "province": "ON",
}


def _pf_candidate(key, lever, amount, *, opportunity=None, excludes=(), requires=()):
    candidate = _candidate(key, lever=lever, opportunity=opportunity,
                           excludes=excludes, requires=requires)
    candidate.lever_application = LeverApplication(lever, {"amount": Decimal(amount)})
    return candidate


def _linear_engine():
    def evaluate(inputs: dict) -> Decimal:
        taxable = inputs["employment_income"] - inputs.get("rrsp_deduction", Decimal(0))
        return (taxable * Decimal("0.30")).quantize(Decimal("0.01"))
    return evaluate


def test_exclusion_still_blocks_the_lower_ranked_candidate():
    a = _pf_candidate("A", "INCREASE_RRSP_DEDUCTION", "1000", opportunity="a",
                      excludes=("b",))
    b = _pf_candidate("B", "INCREASE_RRSP_DEDUCTION", "500", opportunity="b")
    edges = rel.derive([a, b])
    result = pf.assemble([a, b], edges, _BASE, _linear_engine())
    assert [m.candidate_key for m in result.members] == ["A"]
    assert b.portfolio_membership is PortfolioMembership.EXCLUDED_CONFLICT


def test_dependency_still_defers_when_the_prerequisite_is_not_selected():
    """A declared prerequisite that exists as a candidate but cannot be applied
    still blocks its dependant."""
    prerequisite = _candidate("A", opportunity="a")   # no lever → not evaluable
    dependant = _pf_candidate("B", "INCREASE_RRSP_DEDUCTION", "500", opportunity="b",
                              requires=("a",))
    population = [prerequisite, dependant]
    edges = rel.derive(population)
    assert [e.relationship_type for e in edges] == [RelationshipType.REQUIRES]
    result = pf.assemble(population, edges, _BASE, _linear_engine())
    assert [m.candidate_key for m in result.members] == []
    assert dependant.portfolio_membership is PortfolioMembership.DEFERRED_TIMING


def test_a_prerequisite_that_produced_no_candidate_yields_no_edge():
    """Documented limitation, unchanged by this work: a `requires_codes` entry
    naming an opportunity that produced no candidate in this run generates no
    edge, so the assembler has nothing to enforce. The pairwise implementation
    behaved identically. Pinned here so a future change to it is deliberate."""
    lonely = _pf_candidate("B", "INCREASE_RRSP_DEDUCTION", "500", opportunity="b",
                           requires=("never_generated",))
    assert rel.derive([lonely]) == []


def test_a_three_way_exclusion_group_still_admits_exactly_one():
    codes = ("a", "b", "c")
    population = [
        _pf_candidate(key, "INCREASE_RRSP_DEDUCTION", amount, opportunity=code,
                      excludes=tuple(x for x in codes if x != code))
        for key, code, amount in (("A", "a", "3000"), ("B", "b", "2000"), ("C", "c", "1000"))
    ]
    edges = rel.derive(population)
    result = pf.assemble(population, edges, _BASE, _linear_engine())
    assert len(result.members) == 1


def test_measured_edges_cover_the_selected_members_and_nothing_else():
    selected = []
    for i, (standalone, incremental) in enumerate(
        [(Decimal("300"), Decimal("300")), (Decimal("250"), Decimal("100")),
         (Decimal("100"), Decimal("140"))]
    ):
        candidate = _candidate(f"S{i}")
        candidate.standalone_potential = standalone
        candidate.incremental_portfolio_benefit = incremental
        selected.append(candidate)

    edges = rel.measured_edges(selected)
    assert len(edges) == 2
    assert {e.relationship_type for e in edges} == {
        RelationshipType.REDUCES_VALUE, RelationshipType.ENHANCES
    }
    assert all(e.derivation_source is DerivationSource.MEASURED_INTERACTION for e in edges)
    assert all(e.measured_delta is not None for e in edges)
    # at most one edge per member: never a clique over the portfolio
    assert len(edges) <= len(selected) - 1


def test_measured_edges_ignore_candidates_the_engine_never_combined():
    """A candidate with no incremental benefit was never applied in context, so
    there is nothing measured to record — and nothing is invented."""
    a, b = _candidate("A"), _candidate("B")
    a.standalone_potential = Decimal("300")
    assert rel.measured_edges([a, b]) == []
