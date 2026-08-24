variable "region" {
  type    = string
  default = "ca-central-1"
}

variable "github_repository" {
  description = "owner/repo, for the OIDC trust condition."
  type        = string
}

variable "image_uri" {
  description = <<-EOT
    Backend image BY DIGEST. The pipeline supplies the real one on every deploy;
    this default exists only so `terraform plan` can run before the first build.
  EOT
  type        = string
  default     = "public.ecr.aws/docker/library/busybox@sha256:0000000000000000000000000000000000000000000000000000000000000000"
}

variable "aliases" {
  description = "Hostnames. Empty until a domain exists; the distribution then answers on its cloudfront.net name."
  type        = list(string)
  default     = []
}

variable "cloudfront_certificate_arn" {
  description = "ACM certificate in us-east-1. Null uses the CloudFront default certificate, which only covers *.cloudfront.net."
  type        = string
  default     = null
}

variable "alb_certificate_arn" {
  description = "ACM certificate in the primary region for the load balancer listener."
  type        = string
}

variable "app_public_url" {
  description = "Origin recovery links are built from. Must be https."
  type        = string
}

variable "email_sender_address" { type = string }
variable "ses_identity_arn" { type = string }

variable "alert_emails" {
  description = "Operational and budget notices. At least one address is required — see modules/cost_guardrails."
  type        = list(string)
}
variable "capacity_profile" {
  description = <<-DOC
    `lean_launch` or `high_availability`. See modules/capacity — that module is
    the single place every size is decided, and this is the single line that
    decides which set applies. Moving between them is a reviewed configuration
    change, not a redesign.

    The default is the LAUNCH POSTURE, written here rather than in a tfvars file
    that is not in the repository, so that what this environment runs at is
    visible to a reader and to a test.
  DOC
  type        = string
  default     = "lean_launch"
}

variable "ecr_repository_url" {
  description = "From envs/shared. The registry outlives this environment."
  type        = string
}

variable "ecr_repository_arn" {
  description = "From envs/shared."
  type        = string
}

