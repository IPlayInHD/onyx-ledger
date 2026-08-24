# =============================================================================
# WAF — two web ACLs, doing two different jobs
# =============================================================================
# THE WAF IS NOT THE RATE LIMITER. Application admission control stays
# authoritative for anything identity-shaped: it is per identity and per
# operation class, it knows what an analysis costs, and it is tested. The
# rate-based rule here is volumetric only — a coarse outer bound that keeps a
# flood away from the application at all. Setting it low enough to be the real
# limiter would break legitimate bursts it cannot reason about.

# ------------------------------------------------- in front of CloudFront ----
resource "aws_wafv2_web_acl" "cloudfront" {
  provider = aws.us_east_1 # CLOUDFRONT-scoped ACLs live in us-east-1

  name        = "${var.name}-onyx-cloudfront"
  description = "Baseline protections for public Onyx traffic."
  scope       = "CLOUDFRONT"

  default_action {
    allow {}
  }

  rule {
    name     = "common-rule-set"
    priority = 1
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesCommonRuleSet"

        # Customer documents are uploaded through presigned URLs straight to S3,
        # so no large body reaches this distribution — but a tax filing's JSON
        # payload can exceed the default 8 KB inspection limit, and the rule
        # would otherwise reject a legitimate one.
        rule_action_override {
          name = "SizeRestrictions_BODY"
          action_to_use {
            count {}
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "common"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "known-bad-inputs"
    priority = 2
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "known-bad"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "ip-reputation"
    priority = 3
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesAmazonIpReputationList"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "ip-reputation"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "volumetric-bound"
    priority = 4
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit              = var.rate_limit_per_five_minutes
        aggregate_key_type = "IP"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "volumetric"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.name}-onyx-cloudfront"
    sampled_requests_enabled   = true
  }

  tags = local.tags
}

# ------------------------------------------------ in front of the ALB --------
# One rule, and it is the whole reason this ACL exists: a request that did not
# come through our distribution does not carry the secret header, and is
# refused. This is what makes the load balancer's open security group safe.
resource "aws_wafv2_web_acl" "alb" {
  name        = "${var.name}-onyx-alb"
  description = "Admits only requests forwarded by the Onyx distribution."
  scope       = "REGIONAL"

  default_action {
    block {}
  }

  rule {
    name     = "from-our-distribution"
    priority = 1
    action {
      allow {}
    }
    statement {
      byte_match_statement {
        field_to_match {
          single_header {
            name = "x-onyx-origin-verify"
          }
        }
        positional_constraint = "EXACTLY"
        search_string         = random_password.origin_secret.result
        text_transformation {
          priority = 0
          type     = "NONE"
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "from-distribution"
      sampled_requests_enabled   = false # the header is the secret
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.name}-onyx-alb"
    sampled_requests_enabled   = false
  }

  tags = local.tags
}

resource "aws_wafv2_web_acl_association" "alb" {
  resource_arn = var.alb_arn
  web_acl_arn  = aws_wafv2_web_acl.alb.arn
}
