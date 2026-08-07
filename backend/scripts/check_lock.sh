#!/usr/bin/env bash
# Lock-consistency gate: the committed locks must be what pyproject.toml
# currently resolves to.
#
# The failure this closes is mundane and common: a dependency is added or
# bumped in pyproject.toml and the lock is not regenerated, so CI and production
# quietly install something the declaration no longer describes. Recompiling
# into a temporary directory and diffing makes that impossible to forget.
#
# Deterministic by construction: pip-compile sorts its output, and --no-header
# keeps the invocation out of the artifact, so an unchanged declaration
# recompiles byte-identically.
set -euo pipefail
cd "$(dirname "$0")/.."

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PIP_COMPILE=(python -m piptools compile --quiet --generate-hashes --strip-extras --no-header)

"${PIP_COMPILE[@]}" --output-file="$TMP/requirements.lock.txt" pyproject.toml
"${PIP_COMPILE[@]}" --extra=dev --output-file="$TMP/requirements-dev.lock.txt" pyproject.toml

STATUS=0
for lock in requirements.lock.txt requirements-dev.lock.txt; do
  if [ ! -f "$lock" ]; then
    echo "FAIL: $lock is missing; run ./scripts/lock_dependencies.sh" >&2
    STATUS=1
    continue
  fi
  if ! diff -u "$lock" "$TMP/$lock" > "$TMP/$lock.diff"; then
    echo "FAIL: $lock is stale — pyproject.toml resolves to something else." >&2
    head -40 "$TMP/$lock.diff" >&2
    STATUS=1
  else
    echo "ok: $lock matches pyproject.toml"
  fi
done

[ "$STATUS" -eq 0 ] || {
  echo >&2
  echo "Run ./scripts/lock_dependencies.sh and commit the regenerated locks." >&2
  exit 1
}
echo "dependency lock consistency OK"
