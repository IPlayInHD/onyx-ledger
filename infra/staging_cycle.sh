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
#
# THE FOUR VERBS, and which of them carries the destroy:
#
#   ./staging_cycle.sh cycle     up, certify, down — destroy TRAPPED on exit.
#                                The default, and the only one that cannot
#                                leave an environment standing.
#   ./staging_cycle.sh up        provision and STOP. No trap: the environment
#                                stands, and it bills, until `down`.
#   ./staging_cycle.sh certify   run steps 2-9 against a standing environment.
#                                No trap either — a failed proof is the case
#                                where you most want the estate to look at.
#   ./staging_cycle.sh down      destroy. Idempotent; safe to run twice.
#   ./staging_cycle.sh rebuild   down, then up.
#
# `up` AND `certify` DELIBERATELY DO NOT DESTROY, and that is the one way to
# leave money running. Both print the exact `down` command on the way out, and
# `cycle` remains the default precisely so that the safe path is the one you
# get by typing nothing. Anything automated should call `cycle`.
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
ensure_init() {
  # MUST run before anything reads state. `terraform state list` in an
  # uninitialised directory fails, and `standing()` cannot tell that apart from
  # "the environment is already gone" — so without this, `down` on a fresh
  # checkout reports nothing to destroy and exits 0 while staging keeps
  # billing. That is the exact failure this whole script exists to prevent.
  #
  # Idempotent and cheap. If it fails (no credentials, unreachable backend)
  # `set -e` stops here, loudly, rather than proceeding on a false reading.
  terraform -chdir="$STAGING" init -input=false >/dev/null
}

standing() {
  # What `down` would remove. Empty output means nothing is standing, which is
  # what makes `down` safe to run twice and `rebuild` safe to run first.
  # Only meaningful AFTER ensure_init.
  terraform -chdir="$STAGING" state list 2>/dev/null || true
}

remind() {
  printf '\n%s\n' "STAGING IS STANDING AND IS BILLING. Bring it down with:"
  printf '%s\n\n' "    RUN_ID=$RUN_ID EVIDENCE_BUCKET=$EVIDENCE $0 down"
}

provision() {
step "STEP 1   provision"
terraform -chdir="$STAGING" init -input=false
terraform -chdir="$STAGING" apply -auto-approve -input=false
terraform -chdir="$STAGING" output -json > "$OUT/outputs.json"
keep "$OUT/outputs.json"

CLUSTER=$(terraform -chdir="$STAGING" output -raw cluster_name)
MIGRATION=$(terraform -chdir="$STAGING" output -raw migration_task_family)

}

certify() {
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
# The CloudWatch log group belongs to the environment and dies with it, so the
# application's own account of the run has to be pulled out before the destroy.
# `filter-log-events` rather than `create-export-task`: the export API needs a
# bucket policy granting the regional logs service principal, and one more
# bucket policy to get right is not worth it for text.
LOGS=$(terraform -chdir="$STAGING" output -raw log_group_name 2>/dev/null || echo "")
if [ -n "$LOGS" ]; then
  aws logs filter-log-events --log-group-name "$LOGS" \
    --start-time "$(( ( $(date +%s) - 86400 * 14 ) * 1000 ))" \
    --output json > "$OUT/task-logs.json" || echo "log export failed; continuing to destroy"
  keep "$OUT/task-logs.json"
fi

aws s3 ls "s3://$EVIDENCE/$RUN_ID/" | tee "$OUT/manifest.txt"
grep -q . "$OUT/manifest.txt" || { echo "nothing was preserved"; exit 1; }
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
case "${1:-cycle}" in
  cycle)
    # The safe default: whatever happens below, STEP 10 runs.
    trap cleanup EXIT
    provision
    certify
    ;;
  up)
    provision
    remind
    ;;
  certify)
    ensure_init
    [ -n "$(standing)" ] || { echo "nothing is standing; run '$0 up' first"; exit 1; }
    certify
    remind
    ;;
  down)
    ensure_init
    # Idempotent. `cleanup` is the same code the trap runs, so there is exactly
    # one destroy path in this file rather than two that can drift apart.
    if [ -z "$(standing)" ]; then
      echo "nothing is standing; nothing to destroy"
      exit 0
    fi
    cleanup
    ;;
  rebuild)
    ensure_init
    if [ -n "$(standing)" ]; then cleanup; fi
    provision
    remind
    ;;
  *)
    echo "usage: $0 [cycle|up|certify|down|rebuild]" >&2
    exit 64
    ;;
esac
