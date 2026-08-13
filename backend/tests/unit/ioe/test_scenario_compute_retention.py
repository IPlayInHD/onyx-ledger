"""Entry 12B1 Phase A1 — retaining what the engine already produced.

`tests/unit/ioe/test_counterfactual_derived_state.py` proves the sealed SHAPE is
sound. This file proves the WIRING into `ScenarioService` is, which is a
different set of claims and a different set of ways to get it wrong:

  * the retained fields are additive, so no sealed v1 artifact moves;
  * `line_items` is the engine's own output, not a re-derivation of it;
  * an empty pinned rule set stays empty instead of becoming "resolve now".

The engine-execution count and the R1→R2 behavioural pin proof need real rule
rows and live a level up, in `tests/integration/test_counterfactual_derivation.py`.
"""
import ast
import hashlib
import inspect
import json
import pathlib
import textwrap
from decimal import Decimal

import pytest

from app.services.ioe.domain import canonical as c
from app.services.ioe.scenario.counterfactual import build_line_items
from app.services.ioe.scenario.held_evidence import build_snapshot
from app.services.ioe.scenario.service import ScenarioService
from app.services.tax_engine.core.engine import TaxInput, compute
from tests.unit.ioe.test_scenario_result_v1_protocol import NAMES, SPEC_HASH, _shapes

VECTORS = json.loads(
    (pathlib.Path(__file__).parents[2] / "golden"
     / "scenario_result_v1_vectors.json").read_text()
)


# ---------------------------------------------------------------------------
# §12 — the retention must be inert for every sealed v1 artifact
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", NAMES)
def test_retaining_the_engine_output_moves_no_v1_byte(name):
    """THE REGRESSION FOR THIS CHANGE.

    `_compute` now returns two keys it did not return before. If the v1
    canonicalizer consumed its input by iterating it — rather than selecting
    fields by name — every scenario sealed before today would rehash differently
    the moment this shipped, and replay would report the whole corpus as
    tampered.

    So the vectors are re-derived from computed dicts that CARRY the new keys and
    compared against the frozen bytes captured at 71b393c. Passing means the
    addition is invisible to v1; failing means it is a compatibility break and
    the implementation is wrong, never the vectors.
    """
    computed = dict(_shapes()[name])
    computed["line_items"] = [
        {"kind": "income", "label": "Total income", "amount": Decimal("95000.00")},
        {"kind": "tax", "label": "Federal tax", "amount": Decimal("14321.55")},
    ]
    computed["facts"] = {
        "profile.province": "ON",
        "income.total": Decimal("95000.00"),
        "derived.marginal_rate": Decimal("0.2965"),
    }

    payload = ScenarioService.canonical_result(
        computed, result_schema_version="1.0.0")

    assert payload == VECTORS[name]["canonical_payload"]
    text = c.canonical_text(payload)
    assert hashlib.sha256(text.encode()).hexdigest() == (
        VECTORS[name]["canonical_text_sha256"])
    assert c.scenario_result_hash(spec_hash=SPEC_HASH, result=payload) == (
        VECTORS[name]["scenario_result_hash"]), (
        f"{name}: retaining engine output moved the v1 result hash. Every "
        "sealed v1 scenario was hashed under the frozen contract, so this is a "
        "compatibility break, not a vector to refresh."
    )


@pytest.mark.parametrize("name", NAMES)
def test_the_retained_fields_never_reach_the_v1_payload(name):
    """Stated positively as well: not merely "the bytes match" but "these keys
    are absent", so a future edit that smuggled them in under a different name
    would still have to face the byte test above."""
    computed = dict(_shapes()[name])
    computed["line_items"] = [
        {"kind": "tax", "label": "Federal tax", "amount": Decimal("1.00")}]
    computed["facts"] = {"profile.province": "ON"}
    payload = ScenarioService.canonical_result(
        computed, result_schema_version="1.0.0")
    assert "line_items" not in payload
    assert "facts" not in payload


# ---------------------------------------------------------------------------
# §2 — retained, not recomputed
# ---------------------------------------------------------------------------
def test_compute_retains_the_engine_result_rather_than_re_deriving_facts():
    """The fact map must come from the result already in hand.

    `eligibility.engine_facts_for` looks like the natural helper and re-enters
    `compute()` internally, so using it would double the scenario's engine cost
    and — worse — introduce a second execution that could disagree with the one
    the scenario is sealed from. Asserted over the parsed call, since a comment
    saying "no second run" is not a constraint.
    """
    source = textwrap.dedent(inspect.getsource(ScenarioService._compute))
    tree = ast.parse(source)

    calls = [
        ast.unparse(node.func) for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    ]
    assert "TaxEngineService.facts_for" in calls, (
        "the fact map must be built from the already-computed result")
    assert "engine_facts_for" not in calls, (
        "engine_facts_for re-runs the tax engine; _compute already holds a "
        "TaxResult and must not compute a second one")
    assert calls.count("compute") == 1, (
        f"_compute must run the tax engine exactly once, found {calls.count('compute')}")


def test_compute_returns_both_retained_keys():
    """Guard on the guard above: the parse test passes trivially if `_compute`
    stopped returning them at all."""
    source = textwrap.dedent(inspect.getsource(ScenarioService._compute))
    returns = [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
    ]
    assert len(returns) == 1
    keys = {
        k.value for k in returns[0].value.keys
        if isinstance(k, ast.Constant)
    }
    assert {"line_items", "facts"} <= keys
    # the pre-existing contract is not narrowed by the addition
    assert {"scenario_tax", "tax_delta", "objective_baseline",
            "objective_scenario", "objective_delta", "changes",
            "support"} <= keys


# ---------------------------------------------------------------------------
# §8 — sealed tax state is the engine's tax state
# ---------------------------------------------------------------------------
def test_the_sealed_line_items_are_the_engine_line_items():
    """Parity after canonical normalization only. Sealing a re-derived or
    filtered tax state would let the counterfactual disagree with the number the
    scenario was sealed from, and the disagreement would be invisible."""
    result = compute(TaxInput(
        year=2025, province="ON", employment_income=Decimal("95000"),
        rrsp_deduction=Decimal("5000"),
    ))
    sealed = build_line_items(result.line_items)

    assert len(sealed) == len(result.line_items), (
        "every engine line item must be sealed; dropping one would report it as "
        "REMOVED in a later comparison")
    assert {(i.kind, i.label, i.amount) for i in sealed} == {
        (item["kind"], item["label"], c.money(item["amount"]))
        for item in result.line_items
    }
    # ordering is the only difference the sealing is allowed to introduce
    assert list(sealed) == sorted(sealed, key=lambda i: (i.kind, i.label, i.amount))


def test_the_key_order_of_an_engine_line_item_does_not_reach_the_hash():
    """§10. `TaxResult.line_items` are plain dicts, and dict literals preserve
    insertion order. Sealing them by iteration rather than by name would make
    the hash depend on how the engine happened to write its literal."""
    forward = [{"kind": "tax", "label": "Federal tax", "amount": Decimal("100.00")}]
    reversed_keys = [{"amount": Decimal("100.00"), "label": "Federal tax",
                      "kind": "tax"}]
    assert list(forward[0]) != list(reversed_keys[0])   # the fixture is real
    assert build_line_items(forward) == build_line_items(reversed_keys)


def test_a_line_item_the_engine_did_not_emit_cannot_appear():
    """Parity in the other direction — the sealed set is not a superset."""
    result = compute(TaxInput(
        year=2025, province="ON", employment_income=Decimal("95000")))
    engine_labels = {item["label"] for item in result.line_items}
    assert {i.label for i in build_line_items(result.line_items)} == engine_labels


# ---------------------------------------------------------------------------
# §4 — None and empty are different questions
# ---------------------------------------------------------------------------
class _SessionThatMustNotBeUsed:
    """Any attribute access is a failure, which is what makes the next test a
    proof rather than an assertion about arguments."""

    def __getattr__(self, name):
        raise AssertionError(
            f"the rules evaluator reached the database (.{name}) for a scenario "
            "that pinned no rule versions — the empty pin set was converted to "
            "None somewhere, so today's published rules would have entered a "
            "historical counterfactual"
        )


#: A scenario whose user held nothing. Explicit rather than defaulted: the
#: builder refuses to invent held evidence, so a test must state it too.
_EMPTY_EVIDENCE = build_snapshot(())


class _Pinned:
    def __init__(self, ids):
        self.tax_year = 2025
        self.pinned_rule_version_ids = ids


@pytest.mark.asyncio
async def test_an_empty_pinned_set_yields_no_candidates_without_querying():
    """`evaluate` returns immediately for an empty pin set and only queries when
    it was given None. So a session that explodes on contact separates the two
    cases behaviourally: this passes only if `[]` travelled through as `[]`."""
    state = await ScenarioService(user_id=None).build_counterfactual_derived_state(
        _SessionThatMustNotBeUsed(),          # type: ignore[arg-type]
        _Pinned([]),                          # type: ignore[arg-type]
        {"line_items": [], "facts": {}},
        baseline_held_evidence=_EMPTY_EVIDENCE,
    )
    assert state.candidates == ()
    assert state.pinned_rule_version_ids == ()
    # the builder took the snapshot it was handed; it did not go looking for one
    assert state.baseline_held_evidence == _EMPTY_EVIDENCE


def test_the_builder_does_not_collapse_an_empty_pin_set():
    """The structural half. `pins or None` is the one-token edit that turns the
    behavioural test above into a query against today's rules, so the value
    forwarded must be a plain name with no conditional in it."""
    source = textwrap.dedent(
        inspect.getsource(ScenarioService.build_counterfactual_derived_state))
    forwarded = [
        node.value for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.keyword) and node.arg == "pinned_rule_version_ids"
    ]
    assert forwarded, "the builder must pin the rule set explicitly"
    for value in forwarded:
        assert isinstance(value, ast.Name), (
            f"pinned_rule_version_ids is forwarded as {ast.unparse(value)!r}; "
            "any expression here can convert an empty pin set into None, which "
            "means 'resolve today's rules'")


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["line_items", "facts"])
async def test_deriving_without_the_retained_engine_output_is_refused(missing):
    """§10's absence-versus-emptiness rule, at the entry point. Defaulting a
    missing key would build a well-formed derived state describing nothing, and
    nothing is exactly what an ineligible scenario also looks like."""
    computed = {"line_items": [], "facts": {}}
    del computed[missing]
    with pytest.raises(ValueError, match=missing):
        await ScenarioService(user_id=None).build_counterfactual_derived_state(
            _SessionThatMustNotBeUsed(),      # type: ignore[arg-type]
            _Pinned([]),                      # type: ignore[arg-type]
            computed,
            baseline_held_evidence=_EMPTY_EVIDENCE,
        )


# ---------------------------------------------------------------------------
# A v1 seal still carries no counterfactual state
# ---------------------------------------------------------------------------
def test_a_v1_seal_never_builds_or_writes_counterfactual_state():
    """SUPERSEDES the Phase A1 boundary test, which asserted that `_persist`
    mentioned nothing counterfactual at all. Phase A2 wires the builder in, so
    that assertion is obsolete — but the property it protected is not, and is
    asserted more precisely here: every counterfactual write in `_persist` must
    sit behind a v2 guard.

    A v1 seal binds nothing to the derived state, so building one would cost a
    rules evaluation per scenario to produce something no hash covers and no
    column stores — and writing one would put evidence beside a hash that does
    not commit to it.
    """
    source = pathlib.Path(inspect.getfile(ScenarioService)).read_text()
    persist = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_persist"
    )

    guarded: list[ast.AST] = []
    for node in ast.walk(persist):
        if isinstance(node, ast.If) and "SCENARIO_RESULT_SCHEMA_V2" in (
                ast.unparse(node.test)):
            guarded.extend(ast.walk(node))
    guarded_ids = {id(n) for n in guarded}

    unguarded = [
        f"line {node.lineno}: {ast.unparse(node)}"
        for node in ast.walk(persist)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func).endswith("build_counterfactual_derived_state")
        and id(node) not in guarded_ids
    ]
    assert unguarded == [], (
        "these counterfactual derivations run for every scenario, including "
        "v1 seals that bind nothing to them:\n  " + "\n  ".join(unguarded))

    # the column values must likewise be conditional, never a bare build
    assert "SCENARIO_RESULT_SCHEMA_V2" in ast.unparse(persist), (
        "_persist writes counterfactual columns without distinguishing v2")
