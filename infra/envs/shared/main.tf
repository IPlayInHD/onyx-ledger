# =============================================================================
# SHARED — the things that must OUTLIVE an environment
# =============================================================================
# Staging is ephemeral by default: it is created for a proving run and destroyed
# when the run finishes. That is only safe if `terraform destroy` on the staging
# root cannot take anything irreplaceable with it.
#
# So the PERSISTENT TIER lives here, in its own root with its own state:
#
#   * the container registry and its images — an environment must be rebuildable
#     from the SAME bytes it ran before, and a registry that disappears with the
#     environment makes "rebuild and re-verify" impossible
#   * the account's single GitHub OIDC provider
#   * the evidence bucket, which holds what a staging run PROVED after the
#     estate that proved it is gone
#
# Also persistent, but not created here because they need a domain that does not
# exist yet: the Route 53 hosted zone and the two ACM certificates (ca-central-1
# for the load balancer, us-east-1 for CloudFront). They are passed into the
# environment roots as ARNs precisely so that destroying an environment does not
# destroy them, and so that a rebuild does not have to re-validate DNS.
#
# NOTHING HERE IS SIZED BY A CAPACITY PROFILE. Persistence is not capacity.

terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
  backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project     = "onyx-ledger"
      Environment = "shared"
      ManagedBy   = "terraform"
      Tier        = "persistent"
    }
  }
}

data "aws_caller_identity" "current" {}

locals { tags = { Tier = "persistent" } }

# ------------------------------------------------------------------ image ---
resource "aws_kms_key" "ecr" {
  description             = "Onyx container registry encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = local.tags
}

resource "aws_kms_alias" "ecr" {
  name          = "alias/onyx-ecr"
  target_key_id = aws_kms_key.ecr.key_id
}

resource "aws_ecr_repository" "backend" {
  name                 = "onyx/backend"
  image_tag_mutability = "IMMUTABLE" # a tag must never come to mean other bytes
  force_delete         = false

  image_scanning_configuration { scan_on_push = true }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.ecr.arn
  }

  # The registry is the persistent tier. Losing it would mean no environment
  # could be rebuilt from the artefact it was verified with.
  lifecycle { prevent_destroy = true }

  tags = local.tags
}

resource "aws_ecr_lifecycle_policy" "backend" {
  repository = aws_ecr_repository.backend.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 30 images; older ones are rebuildable from git."
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 30
      }
      action = { type = "expire" }
    }]
  })
}

# --------------------------------------------------------------- identity ---
# ONE provider per account. Both environment roots reuse it, so neither has to
# be applied before the other.
resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1", "1c58a3a8518e8759bf075b76b750d4f2df264fcd"]
  tags            = local.tags
}

# --------------------------------------------------------------- evidence ---
# WHAT A DESTROYED ENVIRONMENT LEAVES BEHIND. A staging run produces migration
# output, security-gate results, hard-erasure proof and the twelve-persona
# regression. Those outlive the estate that produced them — otherwise "staging
# proved it" is a claim with nothing behind it once staging is gone.
# Server access logging here would need a fourth bucket to receive it and would
# log reads of test output. This bucket is private, versioned and account-scoped,
# and CloudTrail records the API calls against it.
# tfsec:ignore:aws-s3-enable-bucket-logging
resource "aws_s3_bucket" "evidence" {
  bucket = "${var.evidence_bucket_prefix}-onyx-evidence"
  lifecycle { prevent_destroy = true }
  tags = local.tags
}

resource "aws_s3_bucket_versioning" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  versioning_configuration { status = "Enabled" }
}

# SSE-S3 rather than a customer key: this bucket holds test evidence from an
# environment that never contained customer data, and a CMK here would add cost
# and a key policy to maintain for no confidentiality the account boundary does
# not already give.
# tfsec:ignore:aws-s3-encryption-customer-key
resource "aws_s3_bucket_server_side_encryption_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_public_access_block" "evidence" {
  bucket                  = aws_s3_bucket.evidence.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_policy" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.evidence.arn, "${aws_s3_bucket.evidence.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_s3_bucket_lifecycle_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}
