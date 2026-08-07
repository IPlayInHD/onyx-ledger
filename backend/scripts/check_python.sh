#!/usr/bin/env bash
# Refuse to build, lock, or gate on an unsupported interpreter.
#
# The supported runtime is declared once, in pyproject.toml's
# [tool.onyx.quality_gate].python_version. Development, tests, CI, and the
# release gate all read it from there rather than each pinning their own.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python}"

EXPECTED=$("$PY" - <<'EOF'
import tomllib
with open("pyproject.toml", "rb") as fh:
    print(tomllib.load(fh)["tool"]["onyx"]["quality_gate"]["python_version"])
EOF
)
ACTUAL=$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')

if [ "$ACTUAL" != "$EXPECTED" ]; then
  cat >&2 <<MSG
FAIL: unsupported Python runtime.

  expected: $EXPECTED   (pyproject.toml [tool.onyx.quality_gate].python_version)
  found:    $ACTUAL     ($("$PY" -c 'import sys; print(sys.executable)'))

Onyx Ledger supports exactly one interpreter. Determinism of the dependency
lock, of canonical serialization, and of the sealed hashes is measured on that
runtime and nowhere else. Install $EXPECTED (see .python-version) and retry.
MSG
  exit 1
fi

echo "python runtime OK ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"
