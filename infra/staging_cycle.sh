#!/usr/bin/env bash
# =============================================================================
# THE EPHEMERAL STAGING CYCLE
# =============================================================================
# Staging exists to PROVE things, and a proof is a run, not an estate. This
# script is the run: create the environment, put it through everything that
# would matter in production, keep what it proved, and destroy it.
#
# NOTHING HERE HAS EVER BEEN EXECUTED. There is no AWS account yet. Every step
# is written against the interfaces the rest of this repository already defines,
# and every step that could silently do nothing checks that it did something —
# but until it runs against real infrastructure this is a plan, and it is
# labelled one. Do not quote a step below as evidence that it works.
#
# WHY EPHEMERAL AT ALL. A staging estate held 24/7 costs roughly $145/month and
# is idle for almost all of it. The same estate, created for a two-day proving
# run, costs roughly $10 — and, more usefully, is created from the committed
# configuration every time, so "staging drifted" stops being a thing that can
# happen quietly.
#
# WHAT SURVIVES THE DESTROY, and where it lives:
#   Terraform state       the state bucket from infra/bootstrap
#   container images      envs/shared, ECR, prevent_destroy
#   OIDC provider         envs/shared
#   evidence              envs/shared, the evidence bucket, versioned
#   certificates and DNS  outside these roots entirely; passed in as ARNs
#
# WHAT DOES NOT SURVIVE, on purpose: the VPC, the NAT gateway, the database, the
# cache, the load balancer, the tasks and the distribution. Recreating them is
# what makes the next run a real one.
#
# DRIFT. `terraform destroy` on this root removes only what this root owns, and
# the persistent roots are untouched by it. The one thing to get wrong is
# leaving the environment half-destroyed after a failure — so STEP 10 is
# unconditional, and STEP 11 reports what is still standing.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RUN_ID="${RUN_ID:?set RUN_ID to something that identifies this proving run}"
EVIDENCE="${EVIDENCE_BUCKET:?set EVIDENCE_BUCKET (see envs/shared outputs)}"
STAGING="$HERE/envs/staging"
OUT="$(mktemp -d)"

step() { printf '\n=== %s\n' "$*"; }
keep() { aws s3 cp "$1" "s3://$EVIDENCE/$RUN_ID/$(basename "$1")" --only-show-errors; }

cleanup() {
  step "STEP 10  destroy — unconditional"
  terraform -chdir="$STAGING" destroy -auto-approve -input=false || {
    echo "DESTROY FAILED. The environment is still standing and still costing money."
    echo "Do not walk away from this: run 'terraform -chdir=$STAGING destroy' until it succeeds."
    exit 1
  }
  step "STEP 11  what remains"
  terraform -chdir="$STAGING" state list || true
}
trap cleanup EXIT

step "STEP 1   provision"
terraform -chdir="$STAGING" init -input=false
terraform -chdir="$STAGING" apply -auto-approve -input=false
terraform -chdir="$STAGING" output -json > "$OUT/outputs.json"
keep "$OUT/outputs.json"

CLUSTER=$(terraform -chdir="$STAGING" output -raw cluster_name)
MIGRATION=$(terraform -chdir="$STAGING" output -raw migration_task_family)

step "STEP 2   migrate — and require exit code 0"
task=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$MIGRATION" \
  --launch-type FARGATE --query 'tasks[0].taskArn' --output text \
  --network-configuration "awsvpcConfiguration={subnets=[$PRIVATE_SUBNET_IDS],securityGroups=[$TASKS_SECURITY_GROUP_ID],assignPublicIp=DISABLED}")
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$task"
code=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task" \
  --query 'tasks[0].containers[0].exitCode' --output text)
[ "$code" = "0" ] || { echo "migration exited $code"; exit 1; }

step "STEP 3   verify the estate answers"
base=$(terraform -chdir="$STAGING" output -raw distribution_domain_name)
curl -fsS "https://$base/readyz" | tee "$OUT/readyz.json"; keep "$OUT/readyz.json"

step "STEP 4   end-to-end journeys"
( cd "$HERE/../frontend" && ONYX_E2E_API="https://$base" npx playwright test ) \
  2>&1 | tee "$OUT/e2e.log"; keep "$OUT/e2e.log"

step "STEP 5   security invariants against the real estate"
( cd "$HERE/../backend" && ./scripts/prove_security_gate.sh ) 2>&1 | tee "$OUT/security.log"
keep "$OUT/security.log"

step "STEP 6   privacy hard-erasure, end to end, against real versioned S3"
( cd "$HERE/../backend" && PYTHONPATH=. python scripts/document_storage_audit.py ) \
  2>&1 | tee "$OUT/erasure.log"; keep "$OUT/erasure.log"

step "STEP 7   restore — the backup is not a backup until it has been restored"
db=$(terraform -chdir="$STAGING" output -raw database_identifier 2>/dev/null || echo "")
aws rds describe-db-instances --db-instance-identifier "$db" \
  --query 'DBInstances[0].LatestRestorableTime' --output text | tee "$OUT/pitr.txt"
# The restore itself is a runbook, not a one-liner: see
# docs/operations/runbooks.md, "Restoring the database".
keep "$OUT/pitr.txt"

step "STEP 8   twelve-persona tax regression"
( cd "$HERE/../backend" && PGHOST=localhost PGPORT=5432 PGSUPER=onyx_migrator \
  ./scripts/run_backend_tests.sh tests/acceptance ) 2>&1 | tee "$OUT/personas.log"
keep "$OUT/personas.log"

step "STEP 9   evidence preserved"
aws s3 ls "s3://$EVIDENCE/$RUN_ID/" | tee "$OUT/manifest.txt"
grep -q . "$OUT/manifest.txt" || { echo "nothing was preserved"; exit 1; }
