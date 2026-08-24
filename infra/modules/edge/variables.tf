variable "name" { type = string }
variable "aliases" {
  description = "Hostnames served. Empty until a domain exists — the distribution then answers on its cloudfront.net name."
  type        = list(string)
  default     = []
}
variable "certificate_arn" {
  description = "ACM certificate in us-east-1. Null uses the CloudFront default certificate."
  type        = string
  default     = null
}
variable "alb_dns_name" { type = string }

variable "origin_verify_secret" {
  description = <<-DOC
    The shared secret CloudFront sends and the load balancer's listener demands.

    GENERATED IN THE ENVIRONMENT ROOT, not here, because both this module and
    modules/compute need it and compute cannot depend on edge (edge already
    depends on compute for the load balancer's name).
  DOC
  type        = string
  sensitive   = true
}
variable "access_logs_bucket_domain" { type = string }
variable "rate_limit_per_five_minutes" {
  description = "Volumetric ceiling per IP. Coarse by design; admission control is the real limiter."
  type        = number
  default     = 2000
}
variable "tags" {
  type    = map(string)
  default = {}
}
