# =============================================================================
# STORAGE — customer documents and tax-source legislation
# =============================================================================
# Two buckets, per environment, and they are not interchangeable: one holds
# customer bytes subject to erasure, the other holds published tax sources whose
# whole value is that they are immutable provenance.
#
# VERSIONING IS ON, AND OBJECT LOCK IS NOT. That pairing is deliberate and B2A
# is the reason. `delete_object` on a versioned bucket removes nothing — it
# writes a delete marker and every prior version stays readable by anyone who
# can name one. `ObjectStorage.hard_erase` therefore enumerates every version
# AND every delete marker for one key, removes them by version id, and re-lists
# to confirm nothing remains. Object Lock would make that impossible: a locked
# version cannot be deleted before its retention expires, so a privacy erasure
# would report success having removed nothing, or fail outright. Do not enable
# it here without a legal instruction that outranks the erasure contract, and
# if that day comes, the erasure path needs redesigning first.

locals { tags = merge(var.tags, { Module = "storage" }) }

resource "aws_kms_key" "this" {
  description             = "${var.name} S3 object encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = local.tags
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.name}-s3"
  target_key_id = aws_kms_key.this.key_id
}

# ------------------------------------------------------- customer documents --
resource "aws_s3_bucket" "documents" {
  bucket = "${var.name}-onyx-documents"
  tags   = merge(local.tags, { Contents = "customer-documents" })
}

resource "aws_s3_bucket_public_access_block" "documents" {
  bucket                  = aws_s3_bucket.documents.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "documents" {
  bucket = aws_s3_bucket.documents.id
  rule { object_ownership = "BucketOwnerEnforced" } # ACLs off entirely
}

resource "aws_s3_bucket_versioning" "documents" {
  bucket = aws_s3_bucket.documents.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.this.arn
    }
    bucket_key_enabled = true # cuts KMS request cost on a document-heavy bucket
  }
}

# The ONLY lifecycle rule: clean up multipart uploads that never finished.
# Those are storage nobody can see and everybody pays for.
#
# THERE IS NO EXPIRATION RULE, and its absence is a decision. Customer-record
# retention (PD-3) has not been settled by counsel, and a lifecycle rule is a
# retention policy whatever it is called. Inventing ninety days here would be a
# legal judgement wearing an engineering costume.
resource "aws_s3_bucket_lifecycle_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

resource "aws_s3_bucket_policy" "documents" {
  bucket = aws_s3_bucket.documents.id
  policy = data.aws_iam_policy_document.documents.json
}

data "aws_iam_policy_document" "documents" {
  # Refuse cleartext outright rather than relying on clients to ask for TLS.
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.documents.arn,
      "${aws_s3_bucket.documents.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # Refuse an unencrypted PUT. Without this a caller can write an object with no
  # server-side encryption and the bucket default never applies.
  statement {
    sid       = "DenyUnencryptedObjectUploads"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.documents.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["aws:kms"]
    }
  }
}

# ------------------------------------------------------ legislation sources --
# Published tax sources. Same protections; different reason for versioning —
# here it is provenance, and nothing in the product ever erases from it.
# Access to published sources is recorded where it means something — the
# publication ledger in PostgreSQL names every version and who published it.
# An S3 access log would add bytes, not accountability.
# tfsec:ignore:aws-s3-enable-bucket-logging
resource "aws_s3_bucket" "legislation" {
  bucket = "${var.name}-onyx-legislation"
  tags   = merge(local.tags, { Contents = "tax-sources" })
}

resource "aws_s3_bucket_public_access_block" "legislation" {
  bucket                  = aws_s3_bucket.legislation.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "legislation" {
  bucket = aws_s3_bucket.legislation.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_versioning" "legislation" {
  bucket = aws_s3_bucket.legislation.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "legislation" {
  bucket = aws_s3_bucket.legislation.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.this.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_policy" "legislation" {
  bucket = aws_s3_bucket.legislation.id
  policy = data.aws_iam_policy_document.legislation.json
}

data "aws_iam_policy_document" "legislation" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.legislation.arn,
      "${aws_s3_bucket.legislation.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

# --------------------------------------------------------- access logging ----
# S3 server access logging CANNOT deliver into a KMS-encrypted bucket — AWS
# refuses the delivery, silently. SSE-S3 is the only option that works, and a
# bucket holding access records rather than customer bytes is where that trade
# is acceptable. Neither does this bucket log itself: the recursion has to stop.
# tfsec:ignore:aws-s3-encryption-customer-key tfsec:ignore:aws-s3-enable-bucket-logging
resource "aws_s3_bucket" "access_logs" {
  bucket = "${var.name}-onyx-access-logs"
  tags   = merge(local.tags, { Contents = "s3-access-logs" })
}

resource "aws_s3_bucket_public_access_block" "access_logs" {
  bucket                  = aws_s3_bucket.access_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id
  versioning_configuration { status = "Enabled" }
}

# tfsec:ignore:aws-s3-encryption-customer-key
resource "aws_s3_bucket_server_side_encryption_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_logging" "documents" {
  bucket        = aws_s3_bucket.documents.id
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "documents/"
}
