variable "region" {
  type    = string
  default = "ca-central-1"
}

variable "evidence_bucket_prefix" {
  description = "Globally-unique prefix for the evidence bucket, e.g. the account id."
  type        = string
}
