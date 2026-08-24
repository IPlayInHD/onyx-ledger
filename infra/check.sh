#!/usr/bin/env bash
# =============================================================================
# INFRASTRUCTURE GATE
# =============================================================================
# Format, validate and scan every root and every module. Runs with no AWS
# credentials and creates nothing: `terraform init -backend=false` resolves the
# providers without touching remote state.
#
# WHY EVERY DIRECTORY SEPARATELY. tfsec resolves module calls from the root it
# is pointed at, and a finding inside a module can be reported against the root
# or not reported at all depending on which directory it was given. Scanning
# each directory in its own right is the only way to see everything.
set -euo pipefail

cd "$(dirname "$0")"

ROOTS=(bootstrap envs/shared envs/staging envs/production)

# Validated standalone. Every one of these is a plain module with no provider
# aliases, so Terraform can treat it as a root and check it in isolation.
MODULES=(modules/capacity modules/network modules/database modules/cache
         modules/storage modules/secrets modules/compute
         modules/observability modules/github_oidc modules/cost_guardrails)

# `modules/edge` declares `configuration_aliases = [aws.us_east_1]`, which a
# module cannot satisfy when Terraform treats it as a root: validate reports
# "Provider configuration not present" whatever the module says. It is validated
# through BOTH environment roots above, which is where its alias is actually
# supplied, and it is scanned by tfsec in its own right below.
SCAN_ONLY=(modules/edge)

echo "── terraform fmt (repository-wide, check only)"
terraform fmt -recursive -check -diff .

echo "── terraform validate"
for d in "${ROOTS[@]}" "${MODULES[@]}"; do
  ( cd "$d"
    rm -rf .terraform
    terraform init -backend=false -input=false -no-color >/dev/null
    terraform validate -no-color >/dev/null
  ) || { echo "  FAIL  $d"; exit 1; }
  echo "  ok    $d"
done
echo "  --    ${SCAN_ONLY[*]}: validated through the environment roots (provider alias)"

echo "── tfsec"
FAILED=0
for d in "${ROOTS[@]}" "${MODULES[@]}" "${SCAN_ONLY[@]}"; do
  out=$(tfsec "$d" --no-color --concise-output --exclude-downloaded-modules 2>&1) || {
    # `envs/staging` legitimately runs without RDS deletion protection: it is
    # ephemeral by design and must be destroyable. That is the ONLY accepted
    # finding, it is accepted for that one directory, and it is named here
    # rather than suppressed in the module — where it would also hide the same
    # finding for production, which must never have it.
    if [ "$d" = "envs/staging" ] && [ "$(grep -c 'aws-rds-enable-deletion-protection\|Deletion Protection' <<<"$out")" -ge 1 ] \
       && [ "$(grep -cE '^Result' <<<"$out")" -le 1 ]; then
      echo "  ok    $d (1 accepted: ephemeral staging has no RDS deletion protection)"
      continue
    fi
    echo "$out" | tail -40
    echo "  FAIL  $d"
    FAILED=1
    continue
  }
  echo "  ok    $d"
done
[ "$FAILED" -eq 0 ] || exit 1

echo "── capacity profiles plan cleanly, and the connection guard is live"
( cd modules/capacity
  terraform plan -no-color -input=false -var profile=lean_launch >/dev/null
  terraform plan -no-color -input=false -var profile=high_availability >/dev/null
)
echo "  ok    both profiles"

echo "infra gate: PASS"
