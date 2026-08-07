#!/usr/bin/env bash
# Regenerate the committed dependency locks from pyproject.toml.
#
# Run this whenever [project].dependencies or [project.optional-dependencies]
# changes, then commit the regenerated locks alongside pyproject.toml.
# scripts/check_lock.sh fails CI if the two are out of step.
#
# The installer stays plain pip: these are pip-format requirement files with
# hashes, so nothing in production or CI needs pip-tools to consume them.
set -euo pipefail
cd "$(dirname "$0")/.."

./scripts/check_python.sh

PIP_COMPILE=(python -m piptools compile
  --quiet
  --generate-hashes      # every artifact is content-verified at install time
  --strip-extras         # extras are resolved here, not re-resolved at install
  --no-header            # the invocation is documented here, not in the artifact
)

echo ">> compiling requirements.lock.txt (production)"
"${PIP_COMPILE[@]}" --output-file=requirements.lock.txt pyproject.toml

echo ">> compiling requirements-dev.lock.txt (production + dev)"
"${PIP_COMPILE[@]}" --extra=dev --output-file=requirements-dev.lock.txt pyproject.toml

echo "locks regenerated. Commit them together with pyproject.toml."
