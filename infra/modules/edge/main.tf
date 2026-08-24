# =============================================================================
# EDGE — CloudFront, WAF, and the frontend origin
# =============================================================================
# One distribution serves both halves of a same-origin application: the static
# bundle from a private S3 bucket, and `/api/*` from the load balancer. Same
# origin means the browser sends no preflight and the API needs no CORS trust,
# which is the security property `netlify.toml` bought and this must not lose.
#
# THE ALB IS OPEN TO THE INTERNET AND MUST NOT BE USABLE FROM IT. A security
# group cannot express "only from our distribution" — CloudFront's egress ranges
# are shared by every customer. So the distribution adds a secret header that
# Terraform generates and nobody reads, and the load balancer's listener refuses
# anything that does not carry it. Anything that finds the ALB's DNS name and
# connects directly is answered with a 403 before a target group is chosen.

terraform {
  required_providers {
    aws = {
      source                = "hashicorp/aws"
      configuration_aliases = [aws.us_east_1]
    }
  }
}

locals { tags = merge(var.tags, { Module = "edge" }) }

# ------------------------------------------------------- frontend bucket -----
# The frontend bucket holds the compiled public bundle — the same bytes any
# visitor downloads. SSE-S3 is sufficient for content that is public by design,
# and a customer-managed key here would add KMS cost per object request for no
# confidentiality gain. Access is logged at CloudFront, which is where the
# requests actually arrive.
# tfsec:ignore:aws-s3-encryption-customer-key tfsec:ignore:aws-s3-enable-bucket-logging
resource "aws_s3_bucket" "frontend" {
  bucket = "${var.name}-onyx-frontend"
  tags   = merge(local.tags, { Contents = "static-frontend" })
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket                  = aws_s3_bucket.frontend.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_versioning" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  versioning_configuration { status = "Enabled" }
}

# tfsec:ignore:aws-s3-encryption-customer-key
resource "aws_s3_bucket_server_side_encryption_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

# Origin Access Control: the bucket is private and only this distribution can
# read it. No public listing, no website endpoint, no object URL that works.
resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "${var.name}-onyx-frontend"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.frontend.arn}/*"
      Condition = {
        StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.this.arn }
      }
    }]
  })
}

# ------------------------------------------------------------ distribution ---
resource "aws_cloudfront_distribution" "this" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = "Onyx ${var.name}"
  default_root_object = "index.html"
  aliases             = var.aliases
  price_class         = "PriceClass_100" # NA + EU. The customers are Canadian.
  web_acl_id          = aws_wafv2_web_acl.cloudfront.arn

  origin {
    origin_id                = "frontend"
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  origin {
    origin_id   = "api"
    domain_name = var.alb_dns_name

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
      origin_read_timeout    = 60
    }

    # The shared secret described in the header. Terraform holds it in state;
    # no human needs to see it, and rotating it is a two-apply operation
    # documented in runbooks.md.
    custom_header {
      name  = "X-Onyx-Origin-Verify"
      value = var.origin_verify_secret
    }
  }

  # ---- the application shell -----------------------------------------------
  default_cache_behavior {
    target_origin_id       = "frontend"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
  }

  # ---- the API -------------------------------------------------------------
  # CACHING DISABLED, and this is the single most important line in the file.
  # Every authenticated response here is one customer's tax position. A cache
  # policy that keyed on the path alone would serve one person's figures to the
  # next request for the same URL.
  ordered_cache_behavior {
    path_pattern           = "/api/*"
    target_origin_id       = "api"
    viewer_protocol_policy = "https-only"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
  }

  # A single-page application: an unknown path is a client route, not a missing
  # file. 404 would break a deep link into the product.
  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = var.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }

  logging_config {
    bucket          = var.access_logs_bucket_domain
    prefix          = "cloudfront"
    include_cookies = false # cookies here would mean session material in a log
  }

  tags = local.tags
}

data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

# Forwards Authorization, which the API needs and which the managed
# `AllViewerExceptHostHeader` policy would drop.
data "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "Managed-AllViewerExceptHostHeader"
}
