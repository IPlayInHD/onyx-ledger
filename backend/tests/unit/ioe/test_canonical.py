"""Canonical serialization and hashing — the §9.1 rules, enforced.

Determinism is the whole point of this module, so these tests check the rules
that would silently break it: scale ambiguity, key order, array order, null vs
missing, enum ordinals, wall-clock leakage, and hash-seed sensitivity.
"""
import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.services.ioe.domain import canonical as c
from app.services.ioe.domain.enums import CalculationBasis, EconomicEffectType


# ---- scale discipline -------------------------------------------------------
def test_bare_decimal_is_rejected_so_scale_is_never_implicit():
    with pytest.raises(c.CanonicalizationError, match="bare Decimal"):
        c.canonicalize({"amount": Decimal("1.005")})


def test_float_is_rejected_everywhere():
    with pytest.raises(c.CanonicalizationError, match="float"):
        c.canonicalize({"amount": 1.5})
    with pytest.raises(c.CanonicalizationError, match="float"):
        c.money(1.5)


def test_money_and_rate_use_fixed_scales_and_half_up():
    assert c.money(Decimal("1.005")) == "1.01"      # ROUND_HALF_UP, not banker's
    assert c.money(Decimal("2.344")) == "2.34"
    assert c.money(Decimal("7")) == "7.00"
    assert c.rate(Decimal("0.1234565")) == "0.123457"
    assert c.rate(Decimal("0.03")) == "0.030000"


def test_negative_zero_normalizes():
    assert c.money(Decimal("-0.001")) == c.money(Decimal("0")) == "0.00"


def test_money_none_passes_through():
    assert c.money(None) is None


# ---- structure --------------------------------------------------------------
def test_object_keys_are_sorted_recursively():
    text = c.canonical_text({"b": 1, "a": {"z": 1, "y": 2}})
    assert text == '{"a":{"y":2,"z":1},"b":1}'


def test_array_order_is_preserved_not_sorted():
    """Array ordering is semantic, so the caller decides it."""
    assert c.canonical_text([3, 1, 2]) == "[3,1,2]"


def test_ordered_helper_gives_a_stable_order():
    assert c.ordered(["b", "a", "c"]) == ["a", "b", "c"]
    # and it is idempotent
    assert c.ordered(c.ordered(["b", "a", "c"])) == ["a", "b", "c"]


def test_sets_are_rejected_because_their_order_is_not_stable():
    with pytest.raises(c.CanonicalizationError, match="no stable order"):
        c.canonicalize({"codes": {"a", "b"}})


def test_null_and_missing_are_distinct():
    with_null = c.canonical_text({"a": 1, "b": None})
    without = c.canonical_text({"a": 1})
    assert with_null == '{"a":1,"b":null}'
    assert without == '{"a":1}'
    assert with_null != without
    assert c.canonical_hash({"a": 1, "b": None}) != c.canonical_hash({"a": 1})


def test_enums_serialize_by_value_not_ordinal():
    assert c.canonical_text({"basis": CalculationBasis.ENGINE_DETERMINED}) == (
        '{"basis":"engine_determined"}'
    )
    assert c.canonical_text([EconomicEffectType.TAX_DEFERRAL]) == '["tax_deferral"]'


def test_dates_serialize_iso_and_datetimes_are_rejected():
    assert c.canonical_text({"d": date(2025, 3, 2)}) == '{"d":"2025-03-02"}'
    with pytest.raises(c.CanonicalizationError, match="datetime is excluded"):
        c.canonicalize({"t": datetime(2025, 3, 2, 12, 0)})


def test_no_insignificant_whitespace():
    assert " " not in c.canonical_text({"a": [1, 2], "b": {"c": 3}})


def test_unicode_is_emitted_not_escaped():
    assert c.canonical_text({"name": "café"}) == '{"name":"café"}'


# ---- hashing ----------------------------------------------------------------
def test_hash_is_insensitive_to_input_key_order():
    assert c.canonical_hash({"a": 1, "b": 2}) == c.canonical_hash({"b": 2, "a": 1})


def test_hash_changes_when_a_value_changes():
    assert c.canonical_hash({"a": c.money(1)}) != c.canonical_hash({"a": c.money(2)})


HASH_STABILITY_SCRIPT = """
import sys
sys.path.insert(0, {path!r})
from decimal import Decimal
from app.services.ioe.domain import canonical as c
from app.services.ioe.domain.enums import CalculationBasis
payload = {{
    "basis": CalculationBasis.RULE_FORMULA_DETERMINED,
    "amount": c.money(Decimal("1234.565")),
    "codes": c.ordered(["zebra", "alpha", "mike"]),
    "nested": {{"b": None, "a": c.rate(Decimal("0.03"))}},
}}
print(c.canonical_hash(payload))
"""


def test_hash_is_stable_across_processes_and_hash_seeds(tmp_path):
    """Nothing here may depend on Python's salted hash(): a stored hash must
    still verify after a restart or a deploy with a different PYTHONHASHSEED."""
    import os

    root = os.getcwd()
    script = HASH_STABILITY_SCRIPT.format(path=root)
    digests = set()
    for seed in ("0", "1", "12345", "random"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env, check=True,
        )
        digests.add(out.stdout.strip())
    assert len(digests) == 1, f"hash differed across hash seeds: {digests}"
    # and it matches the in-process value
    assert digests.pop() == c.canonical_hash({
        "basis": CalculationBasis.RULE_FORMULA_DETERMINED,
        "amount": c.money(Decimal("1234.565")),
        "codes": c.ordered(["zebra", "alpha", "mike"]),
        "nested": {"b": None, "a": c.rate(Decimal("0.03"))},
    })


# ---- the four hashes --------------------------------------------------------
def _spec_kwargs(**overrides):
    base = dict(
        baseline_input_snapshot_hash="snap-1",
        tax_year=2025,
        jurisdiction="ON",
        rule_version_set=["r2", "r1"],
        version_manifest={"tax_engine_version": "py-1.0.0"},
        user_constraints={"available_cash": c.money(Decimal("5000"))},
        assumption_set=[],
    )
    base.update(overrides)
    return base


def test_spec_hash_ignores_rule_version_set_ordering():
    a = c.optimization_spec_hash(**_spec_kwargs(rule_version_set=["r1", "r2"]))
    b = c.optimization_spec_hash(**_spec_kwargs(rule_version_set=["r2", "r1"]))
    assert a == b


def test_spec_hash_changes_with_any_pinned_version():
    base = c.optimization_spec_hash(**_spec_kwargs())
    changed = c.optimization_spec_hash(
        **_spec_kwargs(version_manifest={"tax_engine_version": "py-1.1.0"})
    )
    assert base != changed


def test_result_hash_includes_its_spec_hash():
    spec_a = c.optimization_spec_hash(**_spec_kwargs())
    spec_b = c.optimization_spec_hash(**_spec_kwargs(tax_year=2024))
    result = {"portfolio_total_benefit": c.money(Decimal("100"))}
    assert (
        c.optimization_result_hash(spec_hash=spec_a, result=result)
        != c.optimization_result_hash(spec_hash=spec_b, result=result)
    )


def test_scenario_spec_hash_is_not_just_the_deltas():
    """The baseline snapshot and pinned versions are part of scenario identity."""
    kwargs = dict(
        canonical_scenario_input=[{"lever_code": "INCREASE_RRSP_DEDUCTION"}],
        tax_year=2025, jurisdiction="ON", lever_registry_version="1.0.0",
        engine_version="py-1.0.0", engine_config_version="1",
        rule_version_set=["r1"], reference_data_versions={"ref": "1"},
        calculation_policy_version="1", decimal_policy_version="1",
    )
    a = c.scenario_spec_hash(baseline_input_snapshot_hash="snap-A", **kwargs)
    b = c.scenario_spec_hash(baseline_input_snapshot_hash="snap-B", **kwargs)
    assert a != b, "identical deltas over different baselines must not collide"

    c_diff_engine = c.scenario_spec_hash(
        baseline_input_snapshot_hash="snap-A", **{**kwargs, "engine_version": "py-2.0.0"}
    )
    assert a != c_diff_engine


def test_scenario_input_order_matters():
    """Levers apply in order, so a reordering is a different scenario."""
    kwargs = dict(
        baseline_input_snapshot_hash="snap", tax_year=2025, jurisdiction="ON",
        lever_registry_version="1.0.0", engine_version="e", engine_config_version="1",
        rule_version_set=["r1"], reference_data_versions={},
        calculation_policy_version="1", decimal_policy_version="1",
    )
    a = c.scenario_spec_hash(canonical_scenario_input=[{"l": "A"}, {"l": "B"}], **kwargs)
    b = c.scenario_spec_hash(canonical_scenario_input=[{"l": "B"}, {"l": "A"}], **kwargs)
    assert a != b


# ---------------------------------------------------------------------------
# Coverage confirmation for the eight required canonicalization concerns
# ---------------------------------------------------------------------------
def test_unicode_normalization_nfc():
    """Canonically equivalent spellings must hash identically."""
    composed = "café"            # é as a single code point
    decomposed = "café"         # e + combining acute
    assert composed != decomposed
    assert c.canonical_text({"n": composed}) == c.canonical_text({"n": decomposed})
    assert c.canonical_hash({"n": composed}) == c.canonical_hash({"n": decomposed})


def test_unicode_normalization_applies_to_keys_too():
    assert c.canonical_hash({"café": 1}) == c.canonical_hash({"café": 1})


def test_keys_colliding_under_normalization_are_rejected_not_silently_merged():
    with pytest.raises(c.CanonicalizationError, match="duplicate object key"):
        c.canonicalize({"café": 1, "café": 2})


def test_negative_zero_is_normalized_across_all_scale_helpers():
    assert c.money(Decimal("-0")) == c.money(Decimal("0")) == "0.00"
    assert c.rate(Decimal("-0.0000001")) == c.rate(Decimal("0")) == "0.000000"
    assert c.factor(Decimal("-0")) == "0.000000"
    assert c.canonical_hash({"a": c.money(Decimal("-0.00"))}) == c.canonical_hash(
        {"a": c.money(Decimal("0.00"))}
    )


def test_nan_and_infinity_are_rejected():
    for bad in (Decimal("NaN"), Decimal("sNaN")):
        with pytest.raises(c.CanonicalizationError, match="NaN"):
            c.money(bad)
    for bad in (Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(c.CanonicalizationError, match="Infinity"):
            c.money(bad)
    with pytest.raises(c.CanonicalizationError, match="float"):
        c.money(float("nan"))


def test_maximum_decimal_precision_is_bounded():
    too_many_digits = Decimal("1." + "1" * (c.MAX_SIGNIFICANT_DIGITS + 5))
    with pytest.raises(c.CanonicalizationError, match="maximum canonical precision"):
        c.money(too_many_digits)


def test_magnitude_bounds_match_the_database_domains():
    assert c.money(c.MONEY_MAX) == "999999999999.99"
    with pytest.raises(c.CanonicalizationError, match="magnitude bound"):
        c.money(c.MONEY_MAX + Decimal("1"))
    assert c.rate(c.RATE_MAX) == "999.999999"
    with pytest.raises(c.CanonicalizationError, match="magnitude bound"):
        c.rate(Decimal("1000"))


def test_dictionary_key_restrictions():
    with pytest.raises(c.CanonicalizationError, match="object keys must be strings"):
        c.canonicalize({1: "a"})
    with pytest.raises(c.CanonicalizationError, match="object keys must be strings"):
        c.canonicalize({("a", "b"): "x"})
    # enum keys are permitted and serialize by value
    assert c.canonical_text({CalculationBasis.ENGINE_DETERMINED: 1}) == (
        '{"engine_determined":1}'
    )


def test_date_handling_covers_date_reject_datetime_and_reject_time():
    import datetime as dt

    assert c.canonicalize(dt.date(2025, 12, 31)) == "2025-12-31"
    with pytest.raises(c.CanonicalizationError, match="datetime is excluded"):
        c.canonicalize(dt.datetime(2025, 12, 31, 23, 59))
    with pytest.raises(c.CanonicalizationError, match="cannot canonicalize"):
        c.canonicalize(dt.time(12, 0))


def test_explicit_collection_sort_keys():
    """Collections destined for a hash are ordered by an EXPLICIT key."""
    artifacts = [
        {"artifact_kind": "calc_formula", "artifact_key": "B", "content_hash": "h2"},
        {"artifact_kind": "calc_formula", "artifact_key": "A", "content_hash": "h1"},
        {"artifact_kind": "tax_rule_version", "artifact_key": "A", "content_hash": "h3"},
    ]
    ordered = c.ordered(artifacts, key=lambda a: (a["artifact_kind"], a["artifact_key"]))
    assert [a["artifact_key"] for a in ordered] == ["A", "B", "A"]
    # and the snapshot hash is independent of the input order
    assert c.rule_snapshot_hash(artifacts) == c.rule_snapshot_hash(list(reversed(artifacts)))


def test_hashes_are_domain_separated_per_artifact_type():
    """An identical payload must not collide across artifact types."""
    payload = {"same": "payload"}
    digests = {
        domain: c.domain_hash(domain, payload) for domain in c.ALL_HASH_DOMAINS
    }
    assert len(set(digests.values())) == len(c.ALL_HASH_DOMAINS)
    # and none equals the undomained hash
    assert c.canonical_hash(payload) not in set(digests.values())


def test_unknown_hash_domain_is_rejected():
    with pytest.raises(c.CanonicalizationError, match="unknown hash domain"):
        c.domain_hash("not_a_domain", {"a": 1})


def test_domain_tag_pins_the_serialization_version():
    """A change to the §9.1 rules must change every digest rather than silently
    reinterpreting stored ones."""
    tag = c.domain_tag(c.DOMAIN_OPTIMIZATION_SPEC)
    assert tag.startswith("onyx.ioe.optimization_spec.v")
    assert c.CANONICAL_SERIALIZATION_VERSION in tag


def test_spec_and_result_hashes_of_the_same_payload_differ():
    payload = {"x": 1}
    assert c.domain_hash(c.DOMAIN_OPTIMIZATION_SPEC, payload) != c.domain_hash(
        c.DOMAIN_OPTIMIZATION_RESULT, payload
    )
    assert c.domain_hash(c.DOMAIN_SCENARIO_SPEC, payload) != c.domain_hash(
        c.DOMAIN_OPTIMIZATION_SPEC, payload
    )


def test_decimal_bounds_are_semantic_type_specific():
    """A legitimate intermediate must not be rejected by a bound meant for a
    different semantic type (P3 requirement 4)."""
    economic_raw = Decimal("3000")          # a raw economic value, not a rate
    assert c.quantity(economic_raw) == "3000.000000"
    with pytest.raises(c.CanonicalizationError, match="magnitude bound"):
        c.factor(economic_raw)              # correctly rejected AS A RATE

    # each helper enforces its own column's bound
    assert c.quantity(c.QUANTITY_MAX) == "999999999999.999999"
    with pytest.raises(c.CanonicalizationError, match="magnitude bound"):
        c.quantity(c.QUANTITY_MAX + Decimal("1"))
    assert c.MAX_SIGNIFICANT_DIGITS > 30    # a sanity guard, not a policy bound


def test_score_breakdown_with_large_raw_value_canonicalizes():
    """Regression: ScoreComponent.raw_value holds NUMERIC(18,6) quantities."""
    from app.services.ioe.domain.enums import ScoreFactor
    from app.services.ioe.domain.models import ScoreBreakdown, ScoreComponent

    breakdown = ScoreBreakdown(
        overall=Decimal("87.50"),
        components=(ScoreComponent(
            factor_code=ScoreFactor.ECONOMIC_VALUE,
            raw_value=Decimal("4250.00"), normalized_value=Decimal("0.85"),
            weight=Decimal("0.35"), contribution=Decimal("0.2975"),
        ),),
    )
    assert c.canonical_hash(breakdown.as_canonical())
