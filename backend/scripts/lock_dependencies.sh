#!/usr/bin/env bash
# Regenerate the committed dependency locks from pyproject.toml.
#
# Run this whenever [project].dependencies or [project.optional-dependencies]
# changes, then commit the regenerated locks alongside pyproject.toml.
# scripts/check_lock.sh fails CI if the two are out of step.
#
#   ./scripts/lock_dependencies.sh              keep existing pins; change only
#                                               what the declaration forces
#   ./scripts/lock_dependencies.sh --upgrade    deliberately move every pin to
#                                               the newest compatible release
#
# The installer stays plain pip: these are pip-format requirement files with
# hashes, so nothing in production or CI needs pip-tools to consume them.
set -euo pipefail
cd "$(dirname "$0")/.."

./scripts/check_python.sh

cleanup() { rm -rf "${LOCK_TOOLS_DIR:-}"; }
trap cleanup EXIT

source ./scripts/_lock_tools.sh

UPGRADE=()
if [ "${1:-}" = "--upgrade" ]; then
  UPGRADE=(--upgrade)
  echo ">> --upgrade: every pin moves to the newest compatible release"
fi

echo ">> compiling requirements.lock.txt (production)"
"${PIP_COMPILE[@]}" "${UPGRADE[@]+"${UPGRADE[@]}"}" \
  --output-file=requirements.lock.txt pyproject.toml

echo ">> compiling requirements-dev.lock.txt (production + dev)"
"${PIP_COMPILE[@]}" "${UPGRADE[@]+"${UPGRADE[@]}"}" --extra=dev \
  --output-file=requirements-dev.lock.txt pyproject.toml

echo "locks regenerated. Commit them together with pyproject.toml."
