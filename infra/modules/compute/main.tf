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
resource "aws_ecr_repository" "this" {
  name                 = "onyx/backend"
  image_tag_mutability = "IMMUTABLE" # a tag must never come to mean other bytes
  force_delete         = false

  image_scanning_configuration { scan_on_push = true }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.ecr_kms_key_arn
  }

  tags = local.tags
}

resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 30 images; older ones are recoverable from git."
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 30
      }
      action = { type = "expire" }
    }]
  })
}

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
# CloudFront-to-ALB origin does. What stops anyone ELSE using it is the regional
# WAF in modules/edge, whose default action is BLOCK and whose single allow rule
# requires a secret header only our distribution sends. An internal ALB would
# require VPC origins, which CloudFront supports only through VPC Lattice — more
# moving parts than a two-person team should run for the same property.
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

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
