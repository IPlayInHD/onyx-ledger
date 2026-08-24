# =============================================================================
# COMPUTE — ECR, ECS Fargate, and the load balancer
# =============================================================================
# One image, several commands. The API and every worker run the same artefact;
# what differs is the command and — the part that matters — WHICH DATABASE
# IDENTITY each one is handed.
#
# The worker split is not arbitrary and is not one-service-per-queue either.
# It follows the principal boundaries the schema already draws:
#
#   worker-app        the ordinary queues, as onyx_api  (member of onyx_app_rw)
#   worker-freshness  ioe_freshness + ioe_integrity, as onyx_freshness
#   worker-privacy    the privacy queue, as onyx_privacy — the one identity that
#                     destroys customer data, and the only one that can
#   beat              the scheduler. Exactly one task, ever.
#
# Collapsing these into one service with one role would undo PD-16, which was
# precisely this boundary being reachable from the application identity.

locals {
  tags = merge(var.tags, { Module = "compute" })

  # Environment shared by every task. Secrets are NOT here — they are injected
  # by ARN below, so no value appears in a task definition.
  common_environment = [
    { name = "ONYX_ENVIRONMENT", value = var.environment },
    { name = "ONYX_APP_PUBLIC_URL", value = var.app_public_url },
    { name = "ONYX_S3_BUCKET_DOCUMENTS", value = var.documents_bucket },
    { name = "ONYX_S3_BUCKET_LEGISLATION", value = var.legislation_bucket },
    { name = "ONYX_S3_REGION", value = var.region },
    { name = "ONYX_S3_SSE_ALGORITHM", value = "aws:kms" },
    { name = "ONYX_S3_SSE_KMS_KEY_ID", value = var.s3_kms_key_arn },
    { name = "ONYX_STORAGE_PROVIDER", value = "s3" },
    { name = "ONYX_EMAIL_PROVIDER", value = "ses" },
    { name = "ONYX_EMAIL_SENDER_ADDRESS", value = var.email_sender_address },
    { name = "ONYX_SES_REGION", value = var.region },
  ]
}

# ------------------------------------------------------------------- image --
# THE REGISTRY IS NOT OWNED HERE. It lives in envs/shared because staging is
# ephemeral: `terraform destroy` on this root must not take the images with it,
# and the image an environment is rebuilt from must be the same bytes that were
# there before. See envs/shared/main.tf.

# ----------------------------------------------------------------- cluster --
resource "aws_ecs_cluster" "this" {
  name = "${var.name}-onyx"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = local.tags
}

resource "aws_cloudwatch_log_group" "tasks" {
  name              = "/onyx/${var.name}/ecs"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.logs_kms_key_arn
  tags              = local.tags
}

# ---------------------------------------------------------- load balancer ----
# Public by construction: CloudFront reaches it over the internet, as every
# CloudFront-to-ALB origin does. What stops anyone ELSE using it is the LISTENER
# below, whose default action is a 403 and whose single forwarding rule requires
# a secret header that only our distribution sends.
#
# WHY NOT A PRIVATE ALB BEHIND A CLOUDFRONT VPC ORIGIN. That option was
# researched rather than assumed, and rejected on three findings, recorded in
# docs/operations/cost-model.md:
#   * ca-central-1 DOES support VPC origins (all AZs except cac1-az3), so the
#     older note in this file claiming they need VPC Lattice was wrong.
#   * The Terraform provider cannot manage a change to a VPC origin that is
#     attached to a distribution (hashicorp/terraform-provider-aws#40905, still
#     open): AWS returns 409 and the documented workaround is to destroy and
#     recreate the whole distribution. That is not production-ready lifecycle
#     management for the one resource that fronts the entire product.
#   * With a VPC origin CloudFront addresses the load balancer by its AWS-issued
#     `*.elb.amazonaws.com` name, and ACM will not issue a publicly-trusted
#     certificate for a domain we do not control — so keeping `https-only` to
#     the origin is not straightforward, and dropping to `http-only` would give
#     up TLS on that hop.
# The saving foregone is two public IPv4 addresses, $7.30/month.
# tfsec:ignore:aws-elb-alb-not-public
resource "aws_lb" "this" {
  name               = "${var.name}-onyx"
  load_balancer_type = "application"
  internal           = false
  subnets            = var.public_subnet_ids
  security_groups    = [var.alb_security_group_id]

  drop_invalid_header_fields = true
  enable_deletion_protection = var.deletion_protection

  access_logs {
    bucket  = var.access_logs_bucket
    prefix  = "alb"
    enabled = true
  }

  tags = local.tags
}

resource "aws_lb_target_group" "api" {
  name        = "${var.name}-onyx-api"
  port        = var.api_port
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id

  # `/healthz` answers without touching a dependency; `/readyz` opens a database
  # connection. The load balancer asks the cheap one — a database blip should
  # not make every task look dead and trigger a stampede of replacements. The
  # deployment pipeline checks `/readyz` once, deliberately, before shifting
  # traffic.
  health_check {
    path                = "/healthz"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
    matcher             = "200"
  }

  deregistration_delay = 30

  tags = local.tags
}

# THE ORIGIN GATE.
#
# This used to be a REGIONAL WAF web ACL whose default action was BLOCK and
# whose one rule allowed the secret header. It enforced exactly the property
# below and cost $5/month for the ACL plus $1/month for the rule. A listener
# whose DEFAULT action is 403, with one rule that forwards only when the header
# matches, refuses precisely the same requests at precisely the same point —
# before any target is chosen — for nothing.
#
# WHAT WAS GIVEN UP: the WAF's own `BlockedRequests` metric. What replaces it is
# `HTTPCode_ELB_4XX_Count` plus the access logs, which are already enabled and
# already record the matched rule. WHAT WAS NOT GIVEN UP: the CloudFront web ACL
# in modules/edge keeps every managed rule group and the volumetric bound. The
# edge protections are unchanged; only the duplicate ACL is gone.
resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  # DEFAULT DENY. A request that reaches this listener without the header never
  # reaches a target group.
  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "text/plain"
      status_code  = "403"
      message_body = "Forbidden"
    }
  }
}

resource "aws_lb_listener_rule" "from_our_distribution" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 1

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }

  condition {
    http_header {
      http_header_name = "X-Onyx-Origin-Verify"
      values           = [var.origin_verify_secret]
    }
  }
}
