# =============================================================================
# GITHUB → AWS — short-lived credentials, no stored keys
# =============================================================================
# A permanent access key in a GitHub secret is a credential that never expires,
# that several people can read, and that leaves with anyone who leaves. OIDC
# replaces it: GitHub presents a signed token describing the exact repository,
# branch and environment, and AWS exchanges it for a session that lasts an hour.
#
# THE TRUST CONDITION IS THE WHOLE CONTROL. `token.actions.githubusercontent.com:sub`
# is matched against a specific repository AND a specific ref or environment. A
# wildcard here would let any fork, any branch and any pull request from a
# stranger assume the deployment role — which is how this pattern is usually got
# wrong.

locals { tags = merge(var.tags, { Module = "github-oidc" }) }

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_provider ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = var.thumbprints
  tags            = local.tags
}

data "aws_iam_openid_connect_provider" "existing" {
  count = var.create_provider ? 0 : 1
  url   = "https://token.actions.githubusercontent.com"
}

locals {
  provider_arn = var.create_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.existing[0].arn
}

data "aws_iam_policy_document" "assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Named subjects only. Staging trusts the default branch; production trusts
    # a GitHub Environment, so promotion runs through whatever approval that
    # environment requires rather than through a push.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = var.trusted_subjects
    }
  }
}

resource "aws_iam_role" "deploy" {
  name                 = "${var.name}-onyx-deploy"
  assume_role_policy   = data.aws_iam_policy_document.assume.json
  max_session_duration = 3600
  tags                 = local.tags
}

# Deployment, not administration. This role can push an image, register a task
# definition, update the named services and run the migration task. It cannot
# read a secret's value, cannot touch the database, cannot alter IAM, and cannot
# delete a bucket.
resource "aws_iam_role_policy" "deploy" {
  name = "deploy"
  role = aws_iam_role.deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrAuth"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid    = "EcrPush"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:CompleteLayerUpload",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
          "ecr:BatchGetImage",
          "ecr:DescribeImages",
        ]
        Resource = [var.ecr_repository_arn]
      },
      {
        Sid    = "RegisterTaskDefinitions"
        Effect = "Allow"
        Action = ["ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition"]
        # RegisterTaskDefinition takes no resource; the narrowing that matters
        # is PassRole below, which decides WHICH roles a task may be given.
        Resource = "*"
      },
      {
        Sid    = "UpdateNamedServices"
        Effect = "Allow"
        Action = [
          "ecs:UpdateService",
          "ecs:DescribeServices",
          "ecs:DescribeClusters",
          "ecs:ListTasks",
          "ecs:DescribeTasks",
          "ecs:RunTask",
        ]
        Resource = [
          var.cluster_arn,
          "arn:aws:ecs:${var.region}:${var.account_id}:service/${var.cluster_name}/*",
          "arn:aws:ecs:${var.region}:${var.account_id}:task/${var.cluster_name}/*",
          "arn:aws:ecs:${var.region}:${var.account_id}:task-definition/${var.name}-onyx-*:*",
        ]
      },
      {
        # The real limit on what a deployment can create: it may hand a task
        # only these roles. Without this, RegisterTaskDefinition would let the
        # pipeline attach any role in the account to a container it controls.
        Sid      = "PassOnlyOnyxTaskRoles"
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = var.passable_role_arns
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },
      {
        Sid      = "PublishFrontend"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
        Resource = [var.frontend_bucket_arn, "${var.frontend_bucket_arn}/*"]
      },
      {
        Sid      = "InvalidateFrontend"
        Effect   = "Allow"
        Action   = ["cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"]
        Resource = [var.distribution_arn]
      },
    ]
  })
}

# Stated as a deny so that a later broadening of the allow list above cannot
# quietly grant them. Reading a secret is the deployment role's classic
# privilege escalation: it does not need the values, only the ARNs.
resource "aws_iam_role_policy" "deploy_denies" {
  name = "deny-secret-values"
  role = aws_iam_role.deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Deny"
      Action = [
        "secretsmanager:GetSecretValue",
        "iam:CreateAccessKey",
        "iam:AttachRolePolicy",
        "iam:PutRolePolicy",
        "rds:DeleteDBInstance",
        "s3:DeleteBucket",
      ]
      Resource = "*"
    }]
  })
}
