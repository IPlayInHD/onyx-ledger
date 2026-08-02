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
