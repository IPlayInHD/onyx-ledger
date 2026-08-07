#!/usr/bin/env bash
# Protected-scope type gate. Zero errors, no baseline, no tolerance.
#
# The scope is read from pyproject.toml rather than written here, so the gate,
# CI, and tests/unit/test_quality_gate_policy.py cannot disagree about what is
# protected. It is expressed as PACKAGE ROOTS, so a module added under one of
# them is checked from the moment it exists.
set -euo pipefail
cd "$(dirname "$0")/.."

SCOPE=$(python - <<'EOF'
import tomllib
with open("pyproject.toml", "rb") as fh:
    print(" ".join(tomllib.load(fh)["tool"]["onyx"]["quality_gate"]["protected_scope"]))
EOF
)

echo "protected scope: ${SCOPE}"
# No `|| true`, no error-count baseline: mypy's own exit code is the verdict.
mypy ${SCOPE}
