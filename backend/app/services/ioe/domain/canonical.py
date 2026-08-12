"""Canonical serialization and hashing — normative rules from architecture §9.1.

The determinism guarantee this module implements:

    Identical canonical inputs and pinned dependency versions produce
    semantically identical domain outputs and identical canonical result hashes.

Rules enforced here:

  * UTF-8, no BOM; separators ``,`` and ``:`` with no insignificant whitespace.
  * Object keys sorted lexicographically by Unicode code point, recursively.
  * Arrays keep the ORDER THE CALLER GAVE. Ordering is a caller decision because
    it is semantic; `ordered()` is provided for the common cases. Sets are
    rejected outright — their iteration order is not stable across processes.
  * Decimals serialize as strings at a FIXED SCALE PER SEMANTIC TYPE. A bare
    `Decimal` is rejected: the caller must say `money()` or `rate()`, so a scale
    can never be chosen silently.
  * Rounding is ROUND_HALF_UP, applied once, at serialization.
  * `None` is emitted as `null`; a field that does not apply is OMITTED by the
    caller. The two are different and are never interchanged.
  * Enums serialize as their string value, never an ordinal.
  * `date` serializes ISO-8601. `datetime` is REJECTED — wall-clock time is
    excluded from every hash.

Nothing here depends on Python's salted `hash()`, so results are stable across
process restarts and `PYTHONHASHSEED` values (proven by test).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, Literal

CANONICAL_SERIALIZATION_VERSION = "1.1.0"

MONEY_SCALE = Decimal("0.01")        # 2 dp
RATE_SCALE = Decimal("0.000001")     # 6 dp
FACTOR_SCALE = Decimal("0.000001")   # 6 dp
QUANTITY_SCALE = Decimal("0.000001")  # 6 dp

# Magnitude bounds are SEMANTIC-TYPE-SPECIFIC, each mirroring the database column
# that stores that kind of value. A single global bound would be wrong in both
# directions: too tight for legitimate diagnostic quantities, too loose to catch
# an unstorable money value.
#
#   money()     ref.money_amt   NUMERIC(14,2)  → 12 integer digits
#   rate()      ref.rate        NUMERIC(9,6)   → 3 integer digits  (fractions, 0..1)
#   factor()    weights/normalized components   NUMERIC(9,6)
#   quantity()  diagnostic raw values           NUMERIC(18,6) → 12 integer digits
#
# `quantity()` exists precisely so a legitimate intermediate — a raw economic
# value of several thousand dollars recorded beside a normalized 0..1 factor —
# is not rejected by a bound meant for rates.
MONEY_MAX = Decimal("999999999999.99")
RATE_MAX = Decimal("999.999999")
QUANTITY_MAX = Decimal("999999999999.999999")
# Sanity guard against pathological inputs before quantization. Deliberately far
# above every semantic bound: it catches absurd values, it does not police them.
MAX_SIGNIFICANT_DIGITS = 38

# Unicode: text is normalized to NFC so two byte-different but canonically
# equivalent spellings (e.g. "é" as U+00E9 vs "e"+U+0301) hash identically.
UNICODE_FORM: Literal["NFC"] = "NFC"


class CanonicalizationError(TypeError):
    """A value cannot be canonicalized deterministically."""


def normalize_text(value: str) -> str:
    """NFC-normalize a string. Applied to every string and every object key."""
    return unicodedata.normalize(UNICODE_FORM, value)


# ---------------------------------------------------------------------------
# Explicit scale helpers — the ONLY way a Decimal enters a canonical payload
# ---------------------------------------------------------------------------
def _quantize(value: Decimal | int | str, scale: Decimal, maximum: Decimal | None) -> str:
    if isinstance(value, float):
        raise CanonicalizationError(
            "float is not permitted in canonical payloads (use Decimal): "
            f"{value!r}"
        )
    d = value if isinstance(value, Decimal) else Decimal(str(value))

    # NaN and ±Infinity have no canonical form and must never reach a hash.
    if d.is_nan():
        raise CanonicalizationError("NaN is not canonicalizable")
    if d.is_infinite():
        raise CanonicalizationError("Infinity is not canonicalizable")

    digits = len(d.as_tuple().digits)
    if digits > MAX_SIGNIFICANT_DIGITS:
        raise CanonicalizationError(
            f"Decimal exceeds the maximum canonical precision of "
            f"{MAX_SIGNIFICANT_DIGITS} significant digits (got {digits})"
        )
    if maximum is not None and d.copy_abs() > maximum:
        raise CanonicalizationError(
            f"value {d} exceeds the canonical magnitude bound {maximum} "
            f"(it could not be stored in the corresponding database domain)"
        )

    q = d.quantize(scale, rounding=ROUND_HALF_UP)
    # Normalize negative zero so -0.00 and 0.00 hash identically.
    if q == 0:
        q = q.copy_abs()
    return format(q, "f")


def money(value: Decimal | int | str | None) -> str | None:
    """Money at scale 2, ROUND_HALF_UP. `None` passes through as null."""
    return None if value is None else _quantize(value, MONEY_SCALE, MONEY_MAX)


def rate(value: Decimal | int | str | None) -> str | None:
    """Rate/percentage at scale 6."""
    return None if value is None else _quantize(value, RATE_SCALE, RATE_MAX)


def factor(value: Decimal | int | str | None) -> str | None:
    """Weight/normalized factor at scale 6. Bounded like a rate (0..1 domain)."""
    return None if value is None else _quantize(value, FACTOR_SCALE, RATE_MAX)


def quantity(value: Decimal | int | str | None) -> str | None:
    """A diagnostic raw quantity at scale 6, bounded like NUMERIC(18,6).

    Used where a value is recorded for explainability rather than as money or a
    rate — e.g. the raw economic value behind a normalized score factor, which is
    routinely in the thousands and must not be judged by a rate bound.
    """
    return None if value is None else _quantize(value, QUANTITY_SCALE, QUANTITY_MAX)


def ordered(
    items: Sequence[Any], key: Callable[[Any], Any] | None = None
) -> list[Any]:
    """Impose an explicit, stable order on a collection destined for a hash.

    Use this rather than relying on set or query ordering. With no key, items are
    ordered by their canonical string form, which is deterministic.
    """
    if key is None:
        return sorted(items, key=lambda i: dumps(canonicalize(i)))
    return sorted(items, key=key)


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------
def canonicalize(value: Any) -> Any:
    """Convert a value into a JSON-ready structure under the §9.1 rules."""
    # Enum is checked FIRST: StrEnum members are also `str` instances, and we
    # must serialize the declared VALUE rather than relying on the subclass
    # happening to render the same way.
    if isinstance(value, Enum):
        return canonicalize(value.value)

    if value is None or isinstance(value, bool):
        return value

    if isinstance(value, str):
        return normalize_text(value)

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        raise CanonicalizationError(
            f"float is not permitted in canonical payloads (use Decimal): {value!r}"
        )

    if isinstance(value, Decimal):
        raise CanonicalizationError(
            "bare Decimal is ambiguous in a canonical payload — wrap it with "
            "money(), rate(), or factor() so the scale is explicit"
        )

    if isinstance(value, _dt.datetime):
        raise CanonicalizationError(
            "datetime is excluded from canonical payloads (wall-clock time must "
            "not affect a hash)"
        )

    if isinstance(value, _dt.date):
        return value.isoformat()

    if isinstance(value, uuid.UUID):
        return str(value)

    if isinstance(value, (set, frozenset)):
        raise CanonicalizationError(
            "set/frozenset has no stable order — pass an explicitly ordered "
            "sequence (see ordered())"
        )

    if isinstance(value, Mapping):
        # Keys are NFC-normalized, then sorted by Unicode code point, recursively.
        # None VALUES are kept (null ≠ missing).
        normalized: list[tuple[str, Any]] = [
            (_key_text(k), value[k]) for k in value.keys()
        ]
        seen: set[str] = set()
        for key, _ in normalized:
            if key in seen:
                # two distinct source keys collapsed to one under NFC; silently
                # dropping one would make the hash depend on iteration order
                raise CanonicalizationError(
                    f"duplicate object key after Unicode normalization: {key!r}"
                )
            seen.add(key)
        return {
            key: canonicalize(val) for key, val in sorted(normalized, key=lambda kv: kv[0])
        }

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [canonicalize(v) for v in value]   # caller-supplied order preserved

    if hasattr(value, "as_canonical"):
        return canonicalize(value.as_canonical())

    raise CanonicalizationError(f"cannot canonicalize {type(value).__name__}: {value!r}")


def _key_text(key: Any) -> str:
    """Object keys must be text. Integers, tuples, and enums-by-ordinal are
    rejected because their textual form is not stable across languages."""
    if isinstance(key, Enum):
        return normalize_text(str(key.value))
    if isinstance(key, str):
        return normalize_text(key)
    raise CanonicalizationError(
        f"object keys must be strings, got {type(key).__name__}: {key!r}"
    )


def dumps(value: Any) -> str:
    """Canonical JSON text (already-canonicalized input)."""
    return json.dumps(
        value,
        ensure_ascii=False,       # UTF-8 output; no escaping of non-ASCII
        separators=(",", ":"),    # no insignificant whitespace
        sort_keys=False,          # canonicalize() already ordered the keys
        allow_nan=False,
    )


def canonical_text(payload: Any) -> str:
    """Canonicalize then serialize, in one step."""
    return dumps(canonicalize(payload))


def canonical_hash(payload: Any) -> str:
    """SHA-256 (hex) of the canonical UTF-8 encoding of `payload`.

    Prefer `domain_hash()` for stored artifacts: an undomained hash of two
    different artifact types could coincide if their payloads happened to match.
    """
    return hashlib.sha256(canonical_text(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Domain separation
#
# Every stored artifact hashes under its own domain tag, so an optimization spec
# and a scenario spec (or a snapshot, or a portfolio result) can never produce
# the same digest even if their canonical payloads were identical. The tag also
# carries the serialization version, so a change to the §9.1 rules necessarily
# changes every digest rather than silently reinterpreting stored ones.
# ---------------------------------------------------------------------------
HASH_DOMAIN_PREFIX = "onyx.ioe"

DOMAIN_OPTIMIZATION_SPEC = "optimization_spec"
DOMAIN_OPTIMIZATION_RESULT = "optimization_result"
DOMAIN_SCENARIO_SPEC = "scenario_spec"
DOMAIN_SCENARIO_RESULT = "scenario_result"
DOMAIN_RULE_SNAPSHOT = "rule_snapshot"
DOMAIN_ASSUMPTION_SET = "assumption_set"
DOMAIN_PORTFOLIO_RESULT = "portfolio_result"
DOMAIN_VERSION_MANIFEST = "version_manifest"
DOMAIN_WEIGHT_CONFIG = "weight_config"
# Entry 12A. The Personal Tax State Graph is an assembled read model rather than
# a sealed artifact, but its hash has to be domain-separated from every sealed
# hash for the same reason they are separated from each other. Registering here
# is how the existing scheme is REUSED: `domain_hash` refuses an unregistered
# domain, so there is no second canonicalizer and no second hash construction.
DOMAIN_TAX_STATE_GRAPH = "tax_state_graph"
# Entry 12B1. The sealed counterfactual derived state — the tax line items and
# the pinned-rule candidate set a scenario evaluated. Separated from
# DOMAIN_SCENARIO_RESULT because it is bound INTO that result rather than being
# it: the same bytes must not be able to stand in for both.
DOMAIN_COUNTERFACTUAL_DERIVED_STATE = "counterfactual_derived_state"

ALL_HASH_DOMAINS = (
    DOMAIN_OPTIMIZATION_SPEC, DOMAIN_OPTIMIZATION_RESULT,
    DOMAIN_SCENARIO_SPEC, DOMAIN_SCENARIO_RESULT,
    DOMAIN_RULE_SNAPSHOT, DOMAIN_ASSUMPTION_SET,
    DOMAIN_PORTFOLIO_RESULT, DOMAIN_VERSION_MANIFEST, DOMAIN_WEIGHT_CONFIG,
    DOMAIN_TAX_STATE_GRAPH, DOMAIN_COUNTERFACTUAL_DERIVED_STATE,
)


def domain_tag(domain: str) -> str:
    return f"{HASH_DOMAIN_PREFIX}.{domain}.v{CANONICAL_SERIALIZATION_VERSION}"


def domain_hash(domain: str, payload: Any) -> str:
    """SHA-256 (hex) over a domain tag plus the canonical payload."""
    if domain not in ALL_HASH_DOMAINS:
        raise CanonicalizationError(f"unknown hash domain: {domain!r}")
    body = f"{domain_tag(domain)}\n{canonical_text(payload)}"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The four hashes (architecture §9.2)
#
# Spec hashes NEVER include results. Result hashes ALWAYS include their spec
# hash. A scenario spec is never hashed from its deltas alone — the baseline
# snapshot, jurisdiction, and every pinned version are part of its identity.
# ---------------------------------------------------------------------------
def optimization_spec_hash(
    *,
    baseline_input_snapshot_hash: str,
    tax_year: int,
    jurisdiction: str,
    rule_version_set: Sequence[Any],
    version_manifest: Mapping[str, Any],
    user_constraints: Mapping[str, Any],
    assumption_set: Sequence[Any],
) -> str:
    return domain_hash(DOMAIN_OPTIMIZATION_SPEC, {
        "baseline_input_snapshot_hash": baseline_input_snapshot_hash,
        "tax_year": tax_year,
        "jurisdiction": jurisdiction,
        "rule_version_set": ordered([str(r) for r in rule_version_set]),
        "version_manifest": version_manifest,
        "user_constraints": user_constraints,
        "assumption_set": assumption_set,
    })


def optimization_result_hash(*, spec_hash: str, result: Mapping[str, Any]) -> str:
    return domain_hash(
        DOMAIN_OPTIMIZATION_RESULT,
        {"optimization_spec_hash": spec_hash, "result": result},
    )


def scenario_spec_hash(
    *,
    baseline_input_snapshot_hash: str,
    canonical_scenario_input: Sequence[Any],
    tax_year: int,
    jurisdiction: str,
    lever_registry_version: str,
    engine_version: str,
    engine_config_version: str,
    rule_version_set: Sequence[Any],
    reference_data_versions: Mapping[str, Any],
    calculation_policy_version: str,
    decimal_policy_version: str,
) -> str:
    return domain_hash(DOMAIN_SCENARIO_SPEC, {
        "baseline_input_snapshot_hash": baseline_input_snapshot_hash,
        "canonical_scenario_input": list(canonical_scenario_input),   # apply order
        "tax_year": tax_year,
        "jurisdiction": jurisdiction,
        "lever_registry_version": lever_registry_version,
        "engine_version": engine_version,
        "engine_config_version": engine_config_version,
        "rule_version_set": ordered([str(r) for r in rule_version_set]),
        "reference_data_versions": reference_data_versions,
        "calculation_policy_version": calculation_policy_version,
        "decimal_policy_version": decimal_policy_version,
    })


def scenario_result_hash(*, spec_hash: str, result: Mapping[str, Any]) -> str:
    return domain_hash(
        DOMAIN_SCENARIO_RESULT, {"scenario_spec_hash": spec_hash, "result": result}
    )


def rule_snapshot_hash(artifacts: Sequence[Mapping[str, Any]]) -> str:
    """Content-address a rule snapshot (architecture Revision 2.1 §A.2).

    Artifacts are explicitly ordered by (kind, key) — never by query order — so
    the digest depends only on content.
    """
    return domain_hash(DOMAIN_RULE_SNAPSHOT, {
        "artifacts": ordered(
            list(artifacts),
            key=lambda a: (str(a["artifact_kind"]), str(a["artifact_key"])),
        ),
    })


def assumption_set_hash(assumptions: Sequence[Mapping[str, Any]]) -> str:
    """Assumptions ordered explicitly by code; `display_note` is excluded by the
    caller's `as_canonical()`, so rewording a note cannot change identity."""
    return domain_hash(DOMAIN_ASSUMPTION_SET, {
        "assumptions": ordered(list(assumptions), key=lambda a: str(a["code"])),
    })


def portfolio_result_hash(portfolio: Mapping[str, Any]) -> str:
    return domain_hash(DOMAIN_PORTFOLIO_RESULT, {"portfolio": portfolio})


def version_manifest_hash(manifest: Mapping[str, Any]) -> str:
    return domain_hash(DOMAIN_VERSION_MANIFEST, {"manifest": manifest})


def weight_config_checksum(weights: Mapping[str, Any]) -> str:
    return domain_hash(DOMAIN_WEIGHT_CONFIG, {"weights": weights})
