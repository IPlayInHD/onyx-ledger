#!/usr/bin/env bash
# Lock-consistency gate: the committed locks must be what pyproject.toml
# currently resolves to.
#
# The failure this closes is mundane and common: a dependency is added or
# bumped in pyproject.toml and the lock is not regenerated, so CI and production
# quietly install something the declaration no longer describes.
#
# THE QUESTION THIS ASKS is "do the committed locks still satisfy the
# declaration?" — NOT "has anything new been published?". Those are different,
# and getting them confused makes the gate fail on an unrelated upstream
# release, which trains people to ignore it. So the committed lock is copied
# into the scratch directory FIRST: pip-compile preserves pins it already has
# and changes only what the declaration forces. Deliberate upgrades go through
# `lock_dependencies.sh --upgrade`.
set -euo pipefail
cd "$(dirname "$0")/.."

TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP" "${LOCK_TOOLS_DIR:-}"; }
trap cleanup EXIT

source ./scripts/_lock_tools.sh

STATUS=0
for lock in requirements.lock.txt requirements-dev.lock.txt; do
  if [ ! -f "$lock" ]; then
    echo "FAIL: $lock is missing; run ./scripts/lock_dependencies.sh" >&2
    STATUS=1
    continue
  fi
  cp "$lock" "$TMP/$lock"
done
[ "$STATUS" -eq 0 ] || exit 1

"${PIP_COMPILE[@]}" --output-file="$TMP/requirements.lock.txt" pyproject.toml
"${PIP_COMPILE[@]}" --extra=dev --output-file="$TMP/requirements-dev.lock.txt" pyproject.toml

for lock in requirements.lock.txt requirements-dev.lock.txt; do
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
