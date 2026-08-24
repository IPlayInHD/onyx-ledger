# =============================================================================
# BOOTSTRAP — the state bucket, and the chicken-and-egg
# =============================================================================
# Every other stack keeps its state in the bucket this one creates, so this one
# cannot. Its state is local and belongs in a password manager or a private
# archive, not in git. That is the standard shape of the problem and it is
# cheaper to say so than to build a mechanism that hides it.
#
# Run once, from a workstation with administrative credentials:
#
#     cd infra/bootstrap && terraform init && terraform apply
#
# TERRAFORM STATE IS SENSITIVE. It contains generated database passwords, the
# Redis AUTH token, the JWT signing secret and the CloudFront origin secret in
# cleartext. The bucket is therefore encrypted, versioned, public-access-blocked
# and deny-insecure-transport, and nobody should keep a local copy of the
# production state file.

terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = "onyx-ledger"
      ManagedBy = "terraform"
      Stack     = "bootstrap"
    }
  }
}

resource "aws_kms_key" "state" {
  description             = "Terraform state encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "state" {
  name          = "alias/onyx-terraform-state"
  target_key_id = aws_kms_key.state.key_id
}

resource "aws_s3_bucket" "state" {
  bucket = var.state_bucket_name

  # Losing this bucket means losing the record of what exists. Terraform will
  # refuse to destroy it, and so should anybody reading this.
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.state.arn
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# Two people and a pipeline can all run `apply`. The lock is what stops two of
# them writing the same state file at once and producing a record that matches
# neither reality.
# Who read the state, and when. State holds generated database passwords, the
# Redis AUTH token and the JWT signing secret in cleartext, so "somebody
# downloaded this object" is a question worth being able to answer.
#
# The log bucket does not log itself. That recursion has to stop somewhere, and
# stopping it at a bucket containing only access records is the usual place.
# Same two constraints as the document access-log bucket: log delivery refuses a
# KMS-encrypted target, and a log bucket does not log itself.
# tfsec:ignore:aws-s3-encryption-customer-key tfsec:ignore:aws-s3-enable-bucket-logging
resource "aws_s3_bucket" "state_logs" {
  bucket = "${var.state_bucket_name}-logs"

  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_public_access_block" "state_logs" {
  bucket                  = aws_s3_bucket.state_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "state_logs" {
  bucket = aws_s3_bucket.state_logs.id
  versioning_configuration { status = "Enabled" }
}

# tfsec:ignore:aws-s3-encryption-customer-key
resource "aws_s3_bucket_server_side_encryption_configuration" "state_logs" {
  bucket = aws_s3_bucket.state_logs.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_logging" "state" {
  bucket        = aws_s3_bucket.state.id
  target_bucket = aws_s3_bucket.state_logs.id
  target_prefix = "state-access/"
}

resource "aws_dynamodb_table" "locks" {
  name         = var.lock_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.state.arn
  }

  point_in_time_recovery { enabled = true }

  lifecycle { prevent_destroy = true }
}
