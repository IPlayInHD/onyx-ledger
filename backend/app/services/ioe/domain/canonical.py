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
import uuid
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any

CANONICAL_SERIALIZATION_VERSION = "1.0.0"

MONEY_SCALE = Decimal("0.01")        # 2 dp
RATE_SCALE = Decimal("0.000001")     # 6 dp
FACTOR_SCALE = Decimal("0.000001")   # 6 dp


class CanonicalizationError(TypeError):
    """A value cannot be canonicalized deterministically."""


# ---------------------------------------------------------------------------
# Explicit scale helpers — the ONLY way a Decimal enters a canonical payload
# ---------------------------------------------------------------------------
def _quantize(value: Decimal | int | str, scale: Decimal) -> str:
    if isinstance(value, float):
        raise CanonicalizationError(
            "float is not permitted in canonical payloads (use Decimal): "
            f"{value!r}"
        )
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    if not d.is_finite():
        raise CanonicalizationError(f"non-finite Decimal is not canonicalizable: {d!r}")
    q = d.quantize(scale, rounding=ROUND_HALF_UP)
    # Normalize negative zero so -0.00 and 0.00 hash identically.
    if q == 0:
        q = q.copy_abs()
    return format(q, "f")


def money(value: Decimal | int | str | None) -> str | None:
    """Money at scale 2, ROUND_HALF_UP. `None` passes through as null."""
    return None if value is None else _quantize(value, MONEY_SCALE)


def rate(value: Decimal | int | str | None) -> str | None:
    """Rate/percentage at scale 6."""
    return None if value is None else _quantize(value, RATE_SCALE)


def factor(value: Decimal | int | str | None) -> str | None:
    """Weight/normalized factor at scale 6."""
    return None if value is None else _quantize(value, FACTOR_SCALE)


def ordered(items: Sequence[Any], key=None) -> list:
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

    if value is None or isinstance(value, (str, bool)):
        return value

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
        # keys sorted by Unicode code point, recursively; None VALUES are kept
        out = {}
        for k in sorted(value.keys(), key=_key_text):
            out[_key_text(k)] = canonicalize(value[k])
        return out

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [canonicalize(v) for v in value]   # caller-supplied order preserved

    if hasattr(value, "as_canonical"):
        return canonicalize(value.as_canonical())

    raise CanonicalizationError(f"cannot canonicalize {type(value).__name__}: {value!r}")


def _key_text(key: Any) -> str:
    if isinstance(key, str):
        return key
    if isinstance(key, Enum):
        return str(key.value)
    raise CanonicalizationError(f"object keys must be strings, got {type(key).__name__}")


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
    """SHA-256 (hex) of the canonical UTF-8 encoding of `payload`."""
    return hashlib.sha256(canonical_text(payload).encode("utf-8")).hexdigest()


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
    return canonical_hash({
        "baseline_input_snapshot_hash": baseline_input_snapshot_hash,
        "tax_year": tax_year,
        "jurisdiction": jurisdiction,
        "rule_version_set": ordered([str(r) for r in rule_version_set]),
        "version_manifest": version_manifest,
        "user_constraints": user_constraints,
        "assumption_set": assumption_set,
    })


def optimization_result_hash(*, spec_hash: str, result: Mapping[str, Any]) -> str:
    return canonical_hash({"optimization_spec_hash": spec_hash, "result": result})


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
    return canonical_hash({
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
    return canonical_hash({"scenario_spec_hash": spec_hash, "result": result})
