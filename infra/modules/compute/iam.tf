# =============================================================================
# IAM — one role per workload, and they are not interchangeable
# =============================================================================
# The execution role is what ECS itself uses to start a task: pull the image,
# read the injected secrets, write to the log group. The TASK roles are what the
# application code inside the container gets. Keeping them apart is what stops
# "the container can read a secret at boot" from becoming "the code can read
# every secret at will".

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# --------------------------------------------------------- execution role ----
resource "aws_iam_role" "execution" {
  name               = "${var.name}-onyx-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Injection reach: every secret any task might be handed. This role does not
# belong to the application — it belongs to the agent that starts it.
resource "aws_iam_role_policy" "execution_secrets" {
  name = "read-injected-secrets"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = var.all_secret_arns
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = [var.secrets_kms_key_arn]
      },
    ]
  })
}

# ------------------------------------------------------------- task roles ----
locals {
  task_roles = ["api", "worker-app", "worker-freshness", "worker-privacy", "beat", "migration"]
}

resource "aws_iam_role" "task" {
  for_each           = toset(local.task_roles)
  name               = "${var.name}-onyx-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = merge(local.tags, { Workload = each.key })
}

# Every task may write its own logs and use ECS Exec for a supported debug
# session. Nothing else is common.
resource "aws_iam_role_policy" "task_common" {
  for_each = aws_iam_role.task
  name     = "logs"
  role     = each.value.id
  # `<log-group-arn>:*` is the only expressible form: stream names are created
  # at runtime and cannot be enumerated in advance. The grant is still bounded
  # to one log group, which is the narrowing that matters.
  # tfsec:ignore:aws-iam-no-policy-wildcards
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "${aws_cloudwatch_log_group.tasks.arn}:*"
    }]
  })
}

# --- API -------------------------------------------------------------------
# Reads and writes customer documents, sends the three transactional messages,
# and nothing else. Notably it CANNOT delete an object version: erasure is the
# privacy worker's job and the API has no reason to hold the capability.
resource "aws_iam_role_policy" "api" {
  name = "api"
  role = aws_iam_role.task["api"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
        Resource = ["${var.documents_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [var.documents_bucket_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
        Resource = [var.s3_kms_key_arn]
      },
      {
        # SES v2. Scoped to the verified identity so a compromised task cannot
        # send as some other domain this account happens to own.
        Effect   = "Allow"
        Action   = ["ses:SendEmail"]
        Resource = [var.ses_identity_arn]
      },
    ]
  })
}

# --- ordinary workers ------------------------------------------------------
resource "aws_iam_role_policy" "worker_app" {
  name = "worker-app"
  role = aws_iam_role.task["worker-app"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = ["${var.documents_bucket_arn}/*", "${var.legislation_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [var.documents_bucket_arn, var.legislation_bucket_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
        Resource = [var.s3_kms_key_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["ses:SendEmail"]
        Resource = [var.ses_identity_arn]
      },
    ]
  })
}

# --- freshness -------------------------------------------------------------
# Relays the outbox and verifies sealed integrity. Touches no object storage at
# all, so it is granted none.
resource "aws_iam_role_policy" "worker_freshness" {
  name = "worker-freshness"
  role = aws_iam_role.task["worker-freshness"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Deny"
      Action   = ["s3:*"]
      Resource = ["*"]
    }]
  })
}

# --- privacy ---------------------------------------------------------------
# THE ONE ROLE THAT CAN ERASE. `hard_erase` enumerates every version and delete
# marker for a key and removes them by version id, so it needs the versioned
# forms explicitly — `s3:DeleteObject` alone would write a delete marker and
# report success while every prior version stayed readable, which is the exact
# defect B2A closed.
resource "aws_iam_role_policy" "worker_privacy" {
  name = "worker-privacy"
  role = aws_iam_role.task["worker-privacy"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:GetObjectVersion",
          "s3:DeleteObject",
          "s3:DeleteObjectVersion",
        ]
        Resource = ["${var.documents_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:ListBucketVersions"]
        Resource = [var.documents_bucket_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = [var.s3_kms_key_arn]
      },
    ]
  })
}

# --- beat ------------------------------------------------------------------
# Schedules; executes nothing. It needs the broker and no data reach whatever.
resource "aws_iam_role_policy" "beat" {
  name = "beat"
  role = aws_iam_role.task["beat"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Deny"
      Action   = ["s3:*", "ses:*"]
      Resource = ["*"]
    }]
  })
}

# --- migration -------------------------------------------------------------
# DDL only, and it runs as its own task from the pipeline — never inside an API
# container. §15: every API container running migrations on start is how two
# revisions race the same ALTER.
resource "aws_iam_role_policy" "migration" {
  name = "migration"
  role = aws_iam_role.task["migration"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Deny"
      Action   = ["s3:*", "ses:*"]
      Resource = ["*"]
    }]
  })
}
